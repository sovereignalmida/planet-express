"""Reading the beszel hub, and turning its records into PE's own types.

This is the collector seam. Everything above it -- the dashboard data path, the templates --
sees only the types in `planet_express.core.hosts`. A PocketBase record never leaves this
module, so swapping the collector later (or becoming it) is a change here and nowhere else.

The connection direction is the surprising part, and it is why this file exists at all: the
agents dial the hub, the hub does not dial the agents, and an agent registers with exactly
one hub. PE cannot observe the fleet directly, so it reads the hub over HTTP on localhost.

Four rules, each of which is a measured hazard rather than a preference:

  * **Every per-system read carries its filter, and the stats read carries its sort.** The
    three child collections all have a `system` relation, and an unfiltered read does not
    fail -- it succeeds with the wrong host's row. "The newest 1m row" without a filter is
    the newest row in the FLEET; when this was measured that row belonged to CASA MAC MINI,
    so an unfiltered read would have painted the Mac Mini's CPU, memory and disk onto
    whichever card was being rendered, with no error anywhere. `sort=-created` is required
    for the same reason: without it "newest" is whatever the server happened to return
    first. The URLs are built in one place, `_q()`, and `_rows_for()` then refuses any row
    whose `system` is not the one asked for -- so a dropped filter degrades to "no reading
    for this host" rather than to another host's numbers.
  * **Never read `systems`' single-letter summary blob.** It carries a `t` that is an integer
    thread count, while `t` in a stats blob is a sensor-name -> temperature map. Same letter,
    two meanings, one application, and several of its other keys could not be decoded against
    ground truth at all. `system_details` carries the same facts under real names, so this
    module reads that and treats the blob as if it were not there. A test asserts the source
    never names it.
  * **Every failure is a `Liveness` of `unknown` carrying a reason.** Nothing here raises at
    the caller, and nothing substitutes a zero for a value nobody could read. The reasons are
    module constants because the renderer shows them and the tests pin them.
  * **"Authenticated but scoped to nothing" is its own reason.** Every collection's listRule
    is `@request.auth.id != "" && users.id ?= @request.auth.id`, so an account that exists but
    is not listed on any system gets HTTP 200 with an empty `items` -- byte-identical to an
    empty fleet, and nothing like a collector being down. This actually happened during
    setup, and the only thing that distinguishes it is saying so out loud.

Units, measured and not guessed: `system_details.memory` is BYTES while `stats.m` is GiB --
the same quantity under a different unit in a different collection, which is why the unit is
in the field name on both sides of `HostDetails`/`HostMetrics`.

Absent is `None`. Unraid's 0.17.0 agent reports an empty `os_name`, and an empty string that
renders where a measurement goes is this project's recurring bug; see hosts.py.
"""

import http.client
import json
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from planet_express.core.hosts import (
    CURRENT,
    Host,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    liveness,
    parse_timestamp,
    unknown,
)
from planet_express.core.numbers import MAX_BYTES, finite
from planet_express.core.sockets import shutdown_sock

log = logging.getLogger("planetexpress.integrations.beszel")

# The hub runs in the `services` stack and PE reads it on the loopback address, never through
# traefik: the proxy adds a TLS handshake, a name to resolve and an auth layer to the path of
# a read that does not leave the machine.
HUB_HOST = "127.0.0.1"
HUB_PORT = 8090

CONNECT_TIMEOUT_SECONDS = 1   # the hub is on loopback; a connect that is slow is a connect that failed
CALL_TIMEOUT_SECONDS = 3      # per request, wall clock, not per read
# Every request of one hosts() or containers() call together. A fleet read is 1 auth + 1
# listing + 2 per host, so without a budget over the whole thing a hub that answers each
# request just inside the per-call timeout costs 30s for four hosts. Past the budget the
# remaining hosts are unknown with a reason, which is the honest answer and a bounded one.
FLEET_BUDGET_SECONDS = 15
# Bytes per answer. 97 container rows measured at 33 KB, so a 500-row page is ~170 KB; parsed
# JSON costs ~25x its wire size, which is what this is really a ceiling on.
MAX_BYTES_PER_ANSWER = 1024 * 1024
_CHUNK = 64 * 1024

# The widest page PE asks for. Containers is 500 because the spec measured that the hub
# accepts it and this host alone reports 85; systems is bounded because an answer PE renders
# a row per is an answer PE has to be able to hold.
CONTAINERS_PER_PAGE = 500
SYSTEMS_PER_PAGE = 200

# Collector-supplied text is display-only and gets rendered as text by the template, but it
# still has a size: a host name is a label, not a payload.
MAX_TEXT = 120
# A host with more sensors than this is reporting something other than a sensor list.
MAX_TEMPS = 64

# The states beszel's `systems.status` is documented and measured to take. Anything else is
# not a status PE knows how to render, so it is absent rather than echoed onto the page.
STATUSES = ("up", "down", "paused", "pending")

# The same character class config_schema enforces on an inventory entry's system_id, checked
# again here rather than trusted, because this is the line that interpolates the value into
# `filter=(system='<id>')`. The second check is not redundant: containers() is also called
# with ids that came from the COLLECTOR -- the unconfigured rows the spec requires PE to
# surface -- and those never passed config validation at all. A quote or an `&&` here is an
# injection into the expression that chooses whose numbers get rendered, and it fails silently.
_SYSTEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# --- reasons ------------------------------------------------------------------------------
# The renderer shows these and the tests pin them, so they are constants rather than inline
# strings. None of them may contain a credential, a token or a host's secret.

# The one that exists because it was indistinguishable from the others. HTTP 200 with no
# items, after a successful auth, is not "no hosts" and is not "the collector is down".
SCOPED_TO_NOTHING = ("the collector authenticated this account but listed no systems -- "
                     "the account may not be listed on any system yet")
NOT_CONFIGURED = "no collector credentials are configured"
AUTH_REFUSED = "the collector refused the configured credentials"
OUT_OF_TIME = "ran out of time reading the collector"
NO_SYSTEM_ROW = "the collector returned no row for this host"
TIMED_OUT = "the collector did not answer in time"
BAD_ID = "that is not a system id PE will query the collector with"


@dataclass(frozen=True)
class HostReading:
    """One host as the collector answered for it, with whether its numbers are worth showing.

    `metrics` is None when there was no stats row to decode -- never a `HostMetrics` of
    zeroes, which would render as an idle host rather than an unreadable one. `liveness`
    carries the state, the reason and the age; `host` is present either way, because a host
    PE cannot read still has an identity and still renders.
    """

    host: Host
    metrics: HostMetrics | None = None
    liveness: Liveness = field(default_factory=lambda: unknown(NO_SYSTEM_ROW))


@dataclass(frozen=True)
class FleetReading:
    """Every host the collector listed, plus whether the listing itself could be read.

    `liveness.state` is `current` when the listing was read, and `unknown` with the reason
    when it was not -- the collector being unreachable is the whole-fleet case, not an error
    page: the caller renders every configured host unknown and the local host keeps rendering
    from the docker socket, which does not depend on the collector at all.
    """

    liveness: Liveness
    hosts: tuple[HostReading, ...] = ()


@dataclass(frozen=True)
class ContainerReading:
    """A host's containers, or the fact that nobody could read them.

    `containers` is None when the read failed and `()` when it succeeded and the host has
    none. Those are opposite situations and the distinction is the whole point: CASA SOLAR
    ASSISTANT is an aarch64 appliance that really does run no containers and reports fine,
    while a host whose list could not be read must not render as empty.
    """

    system_id: str
    liveness: Liveness
    containers: tuple[RemoteContainer, ...] | None = None


@runtime_checkable
class HostProvider(Protocol):
    """Where host readings come from. One method lists hosts, one lists a host's containers.

    Both return PE's own types and neither raises: a provider reports failure as a `Liveness`
    of unknown with a reason. Implemented here by the hub reader and by the fixture reader,
    and the seam exists so that replacing beszel later touches no route and no template.
    """

    def hosts(self) -> FleetReading:
        ...

    def containers(self, system_id: str) -> ContainerReading:
        ...


# --- query construction -------------------------------------------------------------------

def _q(collection: str, *, system_id: str | None = None, type_: str | None = None,
       sort: str | None = None, per_page: int) -> str:
    """The one place a collector URL is built, so there is one place a filter can go missing.

    The filter expression keeps its parentheses, quotes and `=` literal and percent-encodes
    only `&`, which reproduces exactly what was measured against the live hub:
    `filter=(system='x'%26%26type='1m')`. A raw `&&` would split the query string and the
    filter would silently become `(system='x'` -- unparseable at best, and at worst a read
    that is no longer scoped to one host.
    """
    params = []
    if system_id is not None:
        expression = f"(system='{system_id}'"
        if type_ is not None:
            expression += f"&&type='{type_}'"
        expression += ")"
        params.append("filter=" + urllib.parse.quote(expression, safe="()='"))
    if sort is not None:
        params.append("sort=" + urllib.parse.quote(sort, safe="-"))
    params.append(f"perPage={per_page}")
    return f"/api/collections/{collection}/records?" + "&".join(params)


def systems_query() -> str:
    return _q("systems", per_page=SYSTEMS_PER_PAGE)


def details_query(system_id: str) -> str:
    return _q("system_details", system_id=system_id, per_page=1)


def stats_query(system_id: str) -> str:
    # Both halves of the filter and the sort are load-bearing together: without `system` this
    # is the fleet's newest row, without `type='1m'` it is whichever bucket wrote last (the
    # 10m and 120m rollups are newer than the 1m row most of the time), and without
    # `sort=-created` "newest" is unspecified.
    return _q("system_stats", system_id=system_id, type_="1m", sort="-created", per_page=1)


def containers_query(system_id: str) -> str:
    return _q("containers", system_id=system_id, per_page=CONTAINERS_PER_PAGE)


# --- decoding -----------------------------------------------------------------------------

def _items(answer) -> list | None:
    """A PocketBase list answer's records, or None when it is not one of those."""
    if not isinstance(answer, Mapping):
        return None
    items = answer.get("items")
    return items if isinstance(items, list) else None


def _rows_for(items, system_id: str) -> list:
    """The rows that are actually this host's, whatever the server returned.

    Second line of defence behind the filter in the URL. If a filter were ever dropped the
    answer would carry another host's row, and dropping it here too would be the silent
    failure the spec is about: one host's numbers on another host's card. Refusing the row
    instead turns that into "no reading for this host", which is visible.
    """
    return [row for row in items or []
            if isinstance(row, Mapping) and row.get("system") == system_id]


def _text(value) -> str | None:
    """A short display string, or None. An empty string is None: Unraid reports `os_name` as
    "" and a blank where a measurement goes looks like a measurement of nothing."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:MAX_TEXT] if text else None


def _number(value, limit: float = 1e12) -> float | None:
    """A float, or None. Guards bools (True is an int), inf, and ints too large to float."""
    return float(value) if finite(value, limit) else None


def _count(value) -> int | None:
    """A whole count, or None. A float core count is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if finite(value) else None


def _bytes(value) -> int | None:
    """A byte count, or None. Its own limit because bytes and counts are not the same size of
    number: a RAM figure in bytes passes 1e12 on a 1 TiB host without being remarkable."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if finite(value, MAX_BYTES) else None


def _flag(value) -> bool | None:
    """Tri-state. True/False when the collector said so, None when nobody could tell us.

    Both spellings are accepted because both occur: the REST API answers a JSON boolean and
    the SQLite the fixtures were captured from stores 0/1. Anything else is not an answer.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _details_from(row: Mapping) -> HostDetails:
    """`system_details` -> HostDetails. Named fields only; nothing reverse-engineered."""
    return HostDetails(
        hostname=_text(row.get("hostname")),
        cores=_count(row.get("cores")),
        threads=_count(row.get("threads")),
        arch=_text(row.get("arch")),
        kernel=_text(row.get("kernel")),
        cpu_model=_text(row.get("cpu")),
        # BYTES here, GiB in the stats blob. 16688291840 vs 15.54 for the same stick of RAM.
        memory_bytes=_bytes(row.get("memory")),
        os_name=_text(row.get("os_name")),
    )


def _load_from(value) -> tuple[float, ...] | None:
    """`la` -> the load averages. All three or none: a partly-decoded triple is not a triple."""
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        return None
    numbers = [_number(v) for v in value]
    return tuple(numbers) if all(n is not None for n in numbers) else None


def _temps_from(value) -> Mapping[str, float] | None:
    """`t` -> sensor name to degrees Celsius. In a stats blob this is a map; the identically
    named key in the `systems` summary blob is an integer thread count, which is exactly why
    this module reads stats and never that."""
    if not isinstance(value, Mapping):
        return None
    temps = {}
    for name, reading in value.items():
        if len(temps) >= MAX_TEMPS:
            break
        label, degrees = _text(name), _number(reading)
        if label is not None and degrees is not None:
            temps[label] = degrees
    return temps or None


def _metrics_from(stats: Mapping) -> HostMetrics:
    """A decoded `1m` stats blob. Keys verified against ground truth on the host, 2026-10-01."""
    return HostMetrics(
        cpu_pct=_number(stats.get("cpu")),
        mem_pct=_number(stats.get("mp")),
        mem_used_gib=_number(stats.get("mu")),
        mem_total_gib=_number(stats.get("m")),
        disk_pct=_number(stats.get("dp")),
        disk_used_gib=_number(stats.get("du")),
        disk_total_gib=_number(stats.get("d")),
        load=_load_from(stats.get("la")),
        temps=_temps_from(stats.get("t")),
    )


def _container_from(row: Mapping) -> RemoteContainer | None:
    """One `containers` row, or None when there is nothing there to identify.

    `health` is passed through only when the collector sent a word. The capture carries it as
    an integer code (0 and 2 both occur) whose mapping could not be checked against ground
    truth, and an undecoded code rendered as a health state is the `t`-means-two-things
    mistake with a different letter. An unreadable code is unknown, which is honest; guessing
    that 2 means healthy is not.
    """
    name = _text(row.get("name"))
    if name is None:
        return None
    return RemoteContainer(
        name=name,
        image=_text(row.get("image")),
        status=_text(row.get("status")),
        health=_text(row.get("health")),
        cpu=_number(row.get("cpu")),
        memory=_number(row.get("memory")),
        net=_number(row.get("net")),
        ports=_text(row.get("ports")),
        updatable=_flag(row.get("updatable")),
    )


def _host_from(row: Mapping) -> Host | None:
    """One `systems` row -> Host, or None when it has no usable id.

    `name` is the collector's, which is display-only text: the inventory's name wins where
    there is one, and this is what an unconfigured host renders with. `link` is always None
    here -- the collector stores no link and a link is exactly the field PE would not let a
    remote host supply, so it comes from the inventory alone.
    """
    system_id = row.get("id")
    if not isinstance(system_id, str) or not _SYSTEM_ID_RE.fullmatch(system_id):
        return None
    status = row.get("status")
    return Host(
        id=system_id,
        name=_text(row.get("name")),
        link=None,
        status=status if status in STATUSES else None,
        updated=parse_timestamp(row.get("updated")),
    )


# --- the hub reader -----------------------------------------------------------------------

class _Failure(Exception):
    """An HTTP exchange that did not produce a usable answer. Carries a renderable reason and
    never a credential: the reason goes on the page."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _header_safe(value: str) -> bool:
    """HTTP carries header values as latin-1 with no line breaks. Applied to the session token,
    which is the one value this module puts in a header, so an unusable one is a fixed reason
    rather than an encoder exception whose message names a character of the secret and its
    position. Same check the widget fetcher makes, for the same reason. NOT applied to the
    identity and password: those go only into a JSON body, see _authenticate()."""
    if "\r" in value or "\n" in value:
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class _Attempt:
    """One hosts() or containers() call: its remaining wall clock and its one re-auth.

    The re-auth budget lives here rather than per request, which is what stops a hub that
    rejects every token from turning a four-host read into ten auth posts. A 401 buys exactly
    one re-authentication for the whole call; after that a 401 is a failure with a reason.
    """

    def __init__(self, *, budget: float, clock):
        self._clock = clock
        self._deadline = clock() + budget
        self.reauth_left = 1

    def left(self) -> float:
        return self._deadline - self._clock()

    def expired(self) -> bool:
        return self.left() <= 0


def _default_credentials() -> tuple[str, str]:
    # Imported here, not at module import: config.py reads config.yaml on import, and the
    # provider seam should be importable (and testable) without a configured host.
    import config
    return config.beszel_credentials()


class BeszelHubProvider:
    """Reads the beszel hub's PocketBase API over HTTP on loopback.

    The token is obtained once and reused. It is held in memory only, never logged and never
    put in a reason: a reason is rendered on a page. A 401 means the token expired or was
    revoked, which buys exactly one re-authentication per call -- a hub that rejects
    everything must cost one extra request, not a request per host forever.
    """

    def __init__(self, *, host: str = HUB_HOST, port: int = HUB_PORT, credentials=None,
                 connection_factory=http.client.HTTPConnection, clock=time.monotonic,
                 wall=time.time, budget: float = FLEET_BUDGET_SECONDS):
        self._host = host
        self._port = port
        self._credentials = credentials or _default_credentials
        self._connection_factory = connection_factory
        self._clock = clock
        # Two clocks on purpose: `clock` is monotonic and measures the call's own budget,
        # `wall` dates a reading against the collector's timestamps. A monotonic clock cannot
        # answer "how old is this row", and a wall clock stepped by NTP cannot bound a budget.
        self._wall = wall
        self._budget = budget
        self._lock = threading.Lock()
        self._token: str | None = None

    # -- the two provider methods ----------------------------------------------------------

    def hosts(self) -> FleetReading:
        attempt = self._attempt()
        try:
            answer = self._get(systems_query(), attempt)
        except _Failure as failure:
            # The whole-fleet case. Not an error page: the caller renders every configured
            # host unknown with this reason and the local host keeps using the docker socket.
            return FleetReading(liveness=unknown(failure.reason))
        items = _items(answer)
        if items is None:
            return FleetReading(liveness=unknown("the collector's answer was not a record list"))
        if not items:
            # Authenticated, and scoped to nothing. The listRule hides systems the account is
            # not listed on, so this is byte-identical to an empty fleet and nothing like a
            # collector that is down. Saying which is the only thing that separates them.
            return FleetReading(liveness=unknown(SCOPED_TO_NOTHING))
        readings = []
        for row in items:
            host = _host_from(row) if isinstance(row, Mapping) else None
            if host is None:
                continue
            readings.append(self._reading_for(host, attempt))
        return FleetReading(liveness=Liveness(CURRENT), hosts=tuple(readings))

    def containers(self, system_id: str) -> ContainerReading:
        if not _usable_id(system_id):
            return ContainerReading(system_id=str(system_id)[:MAX_TEXT],
                                    liveness=unknown(BAD_ID))
        attempt = self._attempt()
        try:
            answer = self._get(containers_query(system_id), attempt)
        except _Failure as failure:
            return ContainerReading(system_id=system_id, liveness=unknown(failure.reason))
        items = _items(answer)
        if items is None:
            return ContainerReading(system_id=system_id,
                                    liveness=unknown("the collector's answer was not a record list"))
        return ContainerReading(system_id=system_id, liveness=Liveness(CURRENT),
                                containers=_containers_from(_rows_for(items, system_id)))

    # -- per host --------------------------------------------------------------------------

    def _reading_for(self, host: Host, attempt: _Attempt) -> HostReading:
        """A host's details and newest 1m stats row. One failed host does not fail the fleet."""
        if attempt.expired():
            return HostReading(host=host, liveness=unknown(OUT_OF_TIME))
        try:
            details_rows = _rows_for(_items(self._get(details_query(host.id), attempt)), host.id)
        except _Failure as failure:
            return HostReading(host=host, liveness=unknown(failure.reason))
        host = _with_details(host, _details_from(details_rows[0]) if details_rows else None)
        if attempt.expired():
            return HostReading(host=host, liveness=unknown(OUT_OF_TIME))
        try:
            stats_rows = _rows_for(_items(self._get(stats_query(host.id), attempt)), host.id)
        except _Failure as failure:
            return HostReading(host=host, liveness=unknown(failure.reason))
        if not stats_rows:
            return HostReading(host=host, liveness=unknown(NO_SYSTEM_ROW))
        row = stats_rows[0]
        stats = row.get("stats")
        if not isinstance(stats, Mapping):
            return HostReading(host=host, liveness=unknown("the collector's reading was unreadable"))
        # Aged on the stats row's own `created`, not on `systems.updated`: the question is how
        # old the NUMBERS are, and a host whose agent stopped reporting keeps a fresh-looking
        # `updated` for as long as the hub keeps touching the record.
        return HostReading(host=host, metrics=_metrics_from(stats),
                           liveness=liveness(row.get("created"), now=self._wall()))

    # -- HTTP ------------------------------------------------------------------------------

    def _attempt(self) -> _Attempt:
        return _Attempt(budget=self._budget, clock=self._clock)

    def _get(self, path: str, attempt: _Attempt):
        """One authenticated GET, with at most one re-auth per attempt. Raises _Failure."""
        if attempt.expired():
            raise _Failure(OUT_OF_TIME)
        token = self._authenticate(attempt)
        try:
            return self._request("GET", path, attempt, token=token)
        except _Failure as failure:
            if failure.status != 401 or attempt.reauth_left <= 0:
                raise
            # The token expired or was revoked. Exactly one re-authentication, and the budget
            # is per call rather than per request so N hosts cannot become N auth posts.
            attempt.reauth_left -= 1
            self._forget(token)
            log.info("The collector rejected PE's session token; re-authenticating once.")
            return self._request("GET", path, attempt, token=self._authenticate(attempt))

    def _authenticate(self, attempt: _Attempt) -> str:
        with self._lock:
            if self._token is not None:
                return self._token
        identity, password = self._credentials()
        if not identity or not password:
            raise _Failure(NOT_CONFIGURED)
        # No header-safety check on these two, deliberately. They go into the UTF-8 JSON body
        # below and never into a header, and json.dumps() is ASCII-only output that escapes
        # every control character -- so a CR or LF in a password cannot forge a request line,
        # and a non-Latin-1 character is not a problem to solve. An earlier draft applied the
        # widget fetcher's header check here, which would have rendered a perfectly valid
        # password with an accent in it as "not configured" forever (Codex review, S2). The
        # token the hub answers with IS a header value, and that one is still checked.
        try:
            answer = self._request(
                "POST", "/api/collections/users/auth-with-password", attempt,
                body=json.dumps({"identity": identity, "password": password}).encode("utf-8"),
            )
        except _Failure as failure:
            # PocketBase answers a bad identity or password with 400 and per-field validation,
            # not 401, so "the collector answered 400" would be a true sentence that tells the
            # operator nothing about what to fix. A refusal of the credentials is reported as
            # one. Anything else -- unreachable, timed out, not JSON -- keeps its own reason,
            # because "check the password" is wrong advice for a collector that is down.
            if failure.status in (400, 401, 403):
                raise _Failure(AUTH_REFUSED, failure.status) from None
            raise
        token = answer.get("token") if isinstance(answer, Mapping) else None
        if not isinstance(token, str) or not token or not _header_safe(token):
            raise _Failure(AUTH_REFUSED)
        with self._lock:
            self._token = token
        return token

    def _forget(self, token: str) -> None:
        # Only if it is still the token that failed: another thread may already have replaced
        # it, and dropping the good one would send the next request unauthenticated.
        with self._lock:
            if self._token == token:
                self._token = None

    def _request(self, method: str, path: str, attempt: _Attempt, *, token: str | None = None,
                 body: bytes | None = None):
        """One bounded exchange, returning parsed JSON. Raises _Failure with a renderable
        reason; a 401 carries its status so the caller can spend its one re-auth.

        Time is bounded on the socket itself by a watchdog that shuts it down, because a
        socket timeout is an INACTIVITY timeout: a peer trickling one byte inside it holds the
        read open forever, and getresponse() parses headers before any deadline check can run.
        shutdown(), not close() -- http.client hands the socket to the response the moment it
        reads one that will close the connection and drops its own reference. Same shape as
        the widget fetcher and the icon cache, and the same one copy of shutdown_sock().
        """
        timeout = min(CALL_TIMEOUT_SECONDS, max(attempt.left(), 0.0))
        if timeout <= 0:
            raise _Failure(OUT_OF_TIME)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if token is not None:
            # PocketBase takes the raw token; the `Bearer` prefix is only accepted by newer
            # releases, and the raw form works on both.
            headers["Authorization"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        conn = self._connection_factory(self._host, self._port,
                                        timeout=min(CONNECT_TIMEOUT_SECONDS, timeout))
        fired = threading.Event()
        held = {}

        def expire():
            fired.set()
            # Both, because neither alone covers the whole exchange: during connect() only the
            # connection holds a socket, and after a response that closes the connection
            # http.client has dropped conn.sock and only the copy is left.
            shutdown_sock(getattr(conn, "sock", None))
            shutdown_sock(held.get("sock"))

        # Started before connect(), so the connect counts against the call's wall clock too.
        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                conn.connect()
            except OSError:
                raise _Failure("the collector is unreachable") from None
            held["sock"] = getattr(conn, "sock", None)
            if fired.is_set():
                # The timer can fire while connect() was still running, when there was no
                # socket to shut down yet. It is not rearmed, so without this the request and
                # response that follow would be unbounded.
                shutdown_sock(held["sock"])
                raise _Failure(TIMED_OUT)
            try:
                if held["sock"] is not None:
                    held["sock"].settimeout(timeout)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException, ValueError):
                raise _Failure(TIMED_OUT if fired.is_set() else "the collector dropped the connection") from None
            if resp.status == 401:
                raise _Failure(AUTH_REFUSED, 401)
            if resp.status != 200:
                raise _Failure(f"the collector answered {resp.status}", resp.status)
            encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                # Identity was asked for; decoding what came anyway is a decompression bomb.
                raise _Failure("the collector's answer was compressed")
            payload = self._read_body(resp, fired, attempt)
        finally:
            watchdog.cancel()
            try:
                conn.close()
            except OSError:
                log.debug("The collector connection would not close cleanly.")
        try:
            return json.loads(payload, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise _Failure("the collector's answer was not JSON") from None

    def _read_body(self, resp, fired: threading.Event, attempt: _Attempt) -> bytes:
        """The body, capped as it arrives rather than after. The cap is a memory ceiling on an
        answer PE does not control the size of."""
        body = bytearray()
        try:
            while len(body) <= MAX_BYTES_PER_ANSWER:
                if fired.is_set() or attempt.expired():
                    raise _Failure(TIMED_OUT)
                chunk = resp.read(min(_CHUNK, MAX_BYTES_PER_ANSWER + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
        except _Failure:
            raise
        except TimeoutError:
            raise _Failure(TIMED_OUT) from None
        except (OSError, http.client.HTTPException, ValueError):
            # Includes the watchdog's shutdown, an over-long header line, bad chunk framing.
            raise _Failure(TIMED_OUT if fired.is_set()
                           else "the collector dropped the connection") from None
        if len(body) > MAX_BYTES_PER_ANSWER:
            raise _Failure("the collector's answer was too large")
        if fired.is_set():
            # After the watchdog's shutdown() a read returns EOF rather than raising, so a
            # truncated body otherwise parses as a short one.
            raise _Failure(TIMED_OUT)
        return bytes(body)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def _usable_id(value) -> bool:
    return isinstance(value, str) and bool(_SYSTEM_ID_RE.fullmatch(value))


def _containers_from(rows: Iterable[Mapping]) -> tuple[RemoteContainer, ...]:
    decoded = (_container_from(row) for row in rows if isinstance(row, Mapping))
    return tuple(c for c in decoded if c is not None)


def _with_details(host: Host, details: HostDetails | None) -> Host:
    """A copy of `host` carrying `details`. Host is frozen, so this is the only way to fill
    them in once they have been read."""
    return Host(id=host.id, name=host.name, link=host.link, status=host.status,
                updated=host.updated, details=details)


# --- the fixture reader -------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "beszel"


class FixtureHostProvider:
    """The same readings, out of the committed capture instead of over the network.

    Two jobs, deliberately one class: it is the test double, and it is what the dashboard
    reads until the read-only collector account exists on the live host. So it applies the
    same per-system selection the hub queries do, through the same functions -- a fixture
    provider that handed back the fleet's newest stats row would make every test that asks
    "did this host get its own numbers?" pass for the wrong reason.
    """

    def __init__(self, directory: Path | str = FIXTURES, *, wall=time.time):
        self._directory = Path(directory)
        self._wall = wall

    def hosts(self) -> FleetReading:
        try:
            systems = self._load("systems")
            details = self._load("system_details")
            stats = self._load("system_stats_1m_newest")
        except (OSError, ValueError) as e:
            log.info("The host fixtures could not be read: %s", e)
            return FleetReading(liveness=unknown("the captured collector data could not be read"))
        if not systems:
            return FleetReading(liveness=unknown(SCOPED_TO_NOTHING))
        readings = []
        for row in systems:
            host = _host_from(row) if isinstance(row, Mapping) else None
            if host is None:
                continue
            rows = _rows_for(details, host.id)
            host = _with_details(host, _details_from(rows[0]) if rows else None)
            row = _newest_1m(stats, host.id)
            if row is None or not isinstance(row.get("stats"), Mapping):
                readings.append(HostReading(host=host, liveness=unknown(NO_SYSTEM_ROW)))
                continue
            readings.append(HostReading(host=host, metrics=_metrics_from(row["stats"]),
                                        liveness=liveness(row.get("created"), now=self._wall())))
        return FleetReading(liveness=Liveness(CURRENT), hosts=tuple(readings))

    def containers(self, system_id: str) -> ContainerReading:
        if not _usable_id(system_id):
            return ContainerReading(system_id=str(system_id)[:MAX_TEXT], liveness=unknown(BAD_ID))
        try:
            rows = self._load("containers")
        except (OSError, ValueError) as e:
            log.info("The container fixtures could not be read: %s", e)
            return ContainerReading(system_id=system_id,
                                    liveness=unknown("the captured collector data could not be read"))
        # `()` and not None: a host the capture lists no containers for really has none, which
        # is CASA SOLAR ASSISTANT's actual situation and not a failed read.
        return ContainerReading(system_id=system_id, liveness=Liveness(CURRENT),
                                containers=_containers_from(_rows_for(rows, system_id)))

    def _load(self, name: str) -> list:
        answer = json.loads((self._directory / f"{name}.json").read_text())
        items = _items(answer)
        if items is None:
            raise ValueError(f"{name}.json is not a record list")
        return items


def _newest_1m(rows, system_id: str) -> Mapping | None:
    """The newest `1m` row for ONE system. The client-side twin of the stats query, and the
    reason the fixture provider cannot accidentally answer with the fleet's newest row: both
    the system and the bucket are matched, then the rows are ordered by `created` descending.
    """
    candidates = [row for row in _rows_for(rows, system_id) if row.get("type") == "1m"]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (parse_timestamp(row.get("created")) or 0.0))
