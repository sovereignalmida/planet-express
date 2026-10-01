"""The collector seam: what PE asks the hub, and what it does when the answer is not there.

The hub fake in here is not a stub that agrees with the code. It parses the query string the
provider actually sent -- filter, sort, perPage -- and answers accordingly, because the bug
this slice exists to prevent does not look like a failure: an unfiltered read of
`system_stats` returns HTTP 200 with the newest row in the FLEET, and one host's CPU, memory
and disk get painted onto another host's card with no error anywhere. A fake that ignored the
query could not tell a filtered read from an unfiltered one, so every test about filtering
would pass against code that had none.

Three mutations are therefore run against the real provider with its query builders swapped
for ones missing the filter, the `type` clause and the sort. Each has to change the readings,
and the way it changes them is named: the measured global-newest row belongs to CASA MAC MINI,
so that is what a dropped filter hands back.

The stats rows the fake serves add two rows per system to the capture -- an older `1m` row
placed FIRST in insertion order, and a `10m` rollup stamped newer than the real `1m` row --
because the capture has exactly one `1m` row per system, against which a dropped `sort` and a
dropped `type='1m'` would both be invisible.
"""
import json
import logging
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.hosts import (
    CURRENT,
    STALE,
    STALE_AFTER,
    UNKNOWN,
    Host,
    HostDetails,
)
from planet_express.integrations import beszel
from planet_express.integrations.beszel import (
    AUTH_REFUSED,
    BAD_ID,
    NO_SYSTEM_ROW,
    NOT_CONFIGURED,
    OUT_OF_TIME,
    SCOPED_TO_NOTHING,
    TIMED_OUT,
    BeszelHubProvider,
    ContainerReading,
    FixtureHostProvider,
    FleetReading,
    HostProvider,
    HostReading,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "beszel"

LOCAL_ID = "n7n7ppta55karj9"
UNRAID_ID = "ptf3tn2gzpg913i"
MACMINI_ID = "vw53pk01zei80wt"
SOLAR_ID = "7y4fosy0ebhtk9x"

IDENTITY = "pe-readonly@casalan.com"
PASSWORD = "the-service-account-password-nobody-may-log"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.a-session-token-nobody-may-log"

# The fixture capture's own newest `1m` row per system, in GiB. The point of comparison for
# every "did this host get its OWN numbers?" assertion below.
MEM_TOTAL_GIB = {LOCAL_ID: 15.54, UNRAID_ID: 7.65, MACMINI_ID: 3.57, SOLAR_ID: 0.89}
# What a dropped filter hands back, measured: the fleet's newest 1m row is the Mac Mini's.
FLEET_NEWEST = MACMINI_ID


def _capture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["items"]


# ── a hub that honours the query string ──────────────────────────────────────────

def _stats_rows():
    """The captured newest `1m` row per system, plus the two decoys explained in the module
    docstring. Insertion order is: old 1m, newer 10m, the real 1m."""
    rows = []
    for real in _capture("system_stats_1m_newest"):
        system = real["system"]
        stale = dict(real, id=f"old-{system}", created="2026-10-01 09:00:00.000Z",
                     stats=dict(real["stats"], m=999.0, cpu=99.0))
        rollup = dict(real, id=f"roll-{system}", type="10m",
                      created="2026-10-01 10:59:00.000Z",
                      stats=dict(real["stats"], m=888.0, cpu=88.0))
        rows.extend([stale, rollup, real])
    return rows


class _Sock:
    def __init__(self):
        self.timeouts = []
        self.shut = threading.Event()

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shut.set()


class _Reply:
    def __init__(self, status, body: bytes, headers=None):
        self.status = status
        self._chunks = [body] if body else []
        self._headers = headers or {}

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, amt=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _Hub:
    """An in-memory PocketBase over the capture, answering what the query string asked for."""

    def __init__(self, *, auth_status=200, get_status=200, token=TOKEN, reject_tokens=False,
                 body=None, headers=None, block=False):
        self.rows = {
            "systems": _capture("systems"),
            "system_details": _capture("system_details"),
            "system_stats": _stats_rows(),
            "containers": _capture("containers"),
        }
        self.auth_status = auth_status
        self.get_status = get_status
        self.token = token
        self.reject_tokens = reject_tokens      # every authenticated GET answers 401
        self.body = body                        # a raw body, bypassing the collections
        self.headers = headers or {}
        self.block = block                      # getresponse() blocks until the socket is shut
        self.paths = []                         # every path asked for, in order
        self.auths = 0                          # how many auth posts were made
        self.bodies = []                        # every request body, to prove what was sent
        self.sockets = []

    # -- the connection factory the provider is constructed with -----------------

    def __call__(self, host, port, timeout):
        return _Conn(self, host, port, timeout)

    @property
    def gets(self):
        return [p for p in self.paths if "auth-with-password" not in p]

    def paths_for(self, collection):
        return [p for p in self.paths if f"/collections/{collection}/records" in p]

    def answer(self, method, path, body, headers):
        self.paths.append(path)
        if "auth-with-password" in path:
            self.auths += 1
            self.bodies.append(body)
            if self.auth_status != 200:
                return _Reply(self.auth_status, b'{"message":"Failed to authenticate."}')
            return _Reply(200, json.dumps({"token": self.token, "record": {"id": "u1"}}).encode())
        if self.reject_tokens or headers.get("Authorization") != self.token:
            return _Reply(401, b'{"message":"The request requires valid record authorization."}')
        if self.get_status != 200:
            return _Reply(self.get_status, b'{"message":"nope"}')
        if self.body is not None:
            return _Reply(200, self.body, self.headers)
        return _Reply(200, json.dumps(self._select(path)).encode(), self.headers)

    def _select(self, path):
        base, _, query = path.partition("?")
        collection = base.split("/")[3]
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        rows = list(self.rows.get(collection, []))
        # unquote first: the provider percent-encodes `&&` so the filter survives the query
        # string, and the server is the thing that undoes that.
        expression = urllib.parse.unquote(params.get("filter", [""])[0])
        system = re.search(r"system='([^']*)'", expression)
        if system:
            rows = [r for r in rows if r.get("system") == system.group(1)]
        bucket = re.search(r"type='([^']*)'", expression)
        if bucket:
            rows = [r for r in rows if r.get("type") == bucket.group(1)]
        if params.get("sort") == ["-created"]:
            rows.sort(key=lambda r: r.get("created") or "", reverse=True)
        per_page = int(params.get("perPage", ["30"])[0])
        return {"items": rows[:per_page], "page": 1, "perPage": per_page,
                "totalItems": len(rows), "totalPages": 1}


class _Conn:
    def __init__(self, hub, host, port, timeout):
        self._hub = hub
        self.host = host
        self.port = port
        self.connect_timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = _Sock()
        self._hub.sockets.append(self.sock)

    def request(self, method, path, body=None, headers=None):
        self._method, self._path, self._body = method, path, body
        self._headers = headers or {}

    def getresponse(self):
        if self._hub.block:
            # Blocks exactly the way a peer trickling its answer does. The watchdog's
            # shutdown() is what ends it; without one this would hang the test.
            if not self.sock.shut.wait(10):
                raise AssertionError("nothing shut the socket down: the call was unbounded")
            raise OSError("socket shut down")
        return self._hub.answer(self._method, self._path, self._body, self._headers)

    def close(self):
        pass


class _Refusing:
    """A connection factory for a host that is not listening."""

    def __init__(self):
        self.connects = 0

    def __call__(self, host, port, timeout):
        return self

    def connect(self):
        self.connects += 1
        raise ConnectionRefusedError(111, "Connection refused")

    def close(self):
        pass


def _provider(hub, **over):
    kwargs = {"credentials": lambda: (IDENTITY, PASSWORD), "connection_factory": hub,
              # Frozen a little after the capture, so the fixture's readings are current and a
              # staleness assertion is about the code rather than about today's date.
              "wall": lambda: beszel.parse_timestamp("2026-10-01 10:57:00.000Z")}
    kwargs.update(over)
    return BeszelHubProvider(**kwargs)


def _by_id(reading: FleetReading):
    return {r.host.id: r for r in reading.hosts}


# ── the fake is only worth trusting if it reproduces the hazard ──────────────────

def test_the_fake_hub_reproduces_the_measured_global_newest_row():
    hub = _Hub()
    unfiltered = hub._select("/api/collections/system_stats/records"
                             "?filter=(type='1m')&sort=-created&perPage=1")
    assert unfiltered["items"][0]["system"] == FLEET_NEWEST
    assert unfiltered["items"][0]["stats"]["m"] == MEM_TOTAL_GIB[FLEET_NEWEST]


def test_the_fake_hub_needs_the_sort_to_return_the_newest_row():
    hub = _Hub()
    unsorted_ = hub._select(f"/api/collections/system_stats/records"
                            f"?filter=(system='{LOCAL_ID}'&&type='1m')&perPage=1")
    assert unsorted_["items"][0]["stats"]["m"] == 999.0       # the older decoy, not the reading


def test_the_fake_hub_needs_the_type_clause_to_stay_in_the_1m_bucket():
    hub = _Hub()
    any_bucket = hub._select(f"/api/collections/system_stats/records"
                             f"?filter=(system='{LOCAL_ID}')&sort=-created&perPage=1")
    assert any_bucket["items"][0]["type"] == "10m"


# ── the queries ──────────────────────────────────────────────────────────────────

def test_every_per_system_read_carries_its_filter_and_the_stats_read_its_sort():
    """Asserted on the URLs actually issued. A read that loses its filter does not fail, it
    succeeds with another host's numbers, so the guard has to be on the request itself."""
    hub = _Hub()
    provider = _provider(hub)
    provider.hosts()
    provider.containers(UNRAID_ID)

    details = hub.paths_for("system_details")
    stats = hub.paths_for("system_stats")
    containers = hub.paths_for("containers")
    assert len(details) == 4 and len(stats) == 4 and len(containers) == 1

    for path in details:
        system = re.search(r"filter=\(system='([^']+)'\)", path)
        assert system, f"system_details read without a filter: {path}"
        assert "perPage=1" in path
    for path in stats:
        assert re.search(r"filter=\(system='[^']+'%26%26type='1m'\)", path), \
            f"system_stats read without both filter clauses: {path}"
        assert "sort=-created" in path, f"system_stats read without its sort: {path}"
        assert "perPage=1" in path
    for path in containers:
        assert re.search(r"filter=\(system='([^']+)'\)", path), \
            f"containers read without a filter: {path}"
        assert "perPage=500" in path
    # Every id asked about is one of the four, and each host was asked about exactly once.
    assert sorted(re.search(r"system='([^']+)'", p).group(1) for p in details) == \
        sorted([LOCAL_ID, UNRAID_ID, MACMINI_ID, SOLAR_ID])


def test_the_filter_percent_encodes_only_the_ampersands():
    # A raw `&&` would split the query string, leaving the filter as `(system='x'` -- a read
    # that is no longer scoped to one host. The parens and quotes stay literal, which is what
    # was measured against the live hub.
    assert beszel.stats_query("abc") == (
        "/api/collections/system_stats/records"
        "?filter=(system='abc'%26%26type='1m')&sort=-created&perPage=1")
    assert beszel.details_query("abc") == (
        "/api/collections/system_details/records?filter=(system='abc')&perPage=1")
    assert beszel.containers_query("abc") == (
        "/api/collections/containers/records?filter=(system='abc')&perPage=500")


def test_each_host_gets_its_own_numbers():
    hub = _Hub()
    readings = _by_id(_provider(hub).hosts())
    assert set(readings) == set(MEM_TOTAL_GIB)
    for system_id, expected in MEM_TOTAL_GIB.items():
        assert readings[system_id].metrics.mem_total_gib == expected
        assert readings[system_id].liveness.state == CURRENT


@pytest.mark.parametrize("name,builder", [
    # Each mutation is a plausible edit: the filter dropped, the bucket clause dropped, the
    # sort dropped. None of them makes the hub fail, which is the whole problem.
    ("stats filter dropped",
     lambda sid: "/api/collections/system_stats/records?filter=(type='1m')&sort=-created&perPage=1"),
    ("stats type clause dropped",
     lambda sid: f"/api/collections/system_stats/records?filter=(system='{sid}')&sort=-created&perPage=1"),
    ("stats sort dropped",
     lambda sid: f"/api/collections/system_stats/records?filter=(system='{sid}'%26%26type='1m')&perPage=1"),
])
def test_a_dropped_filter_or_sort_stops_the_host_from_getting_its_own_numbers(monkeypatch, name, builder):
    monkeypatch.setattr(beszel, "stats_query", builder)
    hub = _Hub()
    readings = _by_id(_provider(hub).hosts())
    wrong = {sid: r.metrics.mem_total_gib if r.metrics else None for sid, r in readings.items()
             if (r.metrics.mem_total_gib if r.metrics else None) != MEM_TOTAL_GIB[sid]}
    assert wrong, f"{name} changed nothing: this test no longer guards the query"
    # And the failure is never silently another host's reading: the client-side re-check
    # refuses a row whose `system` is not the one asked for, so a dropped filter degrades to
    # "no reading" rather than to the Mac Mini's CPU on somebody else's card.
    for system_id, value in wrong.items():
        assert value != MEM_TOTAL_GIB[FLEET_NEWEST] or system_id == FLEET_NEWEST
        if readings[system_id].metrics is None:
            assert readings[system_id].liveness.state == UNKNOWN
            assert readings[system_id].liveness.reason == NO_SYSTEM_ROW


def test_a_row_for_another_system_is_refused_rather_than_decoded():
    """Defence behind the filter: whatever the server returns, a row has to be this host's."""
    hub = _Hub()
    mac_row = next(r for r in hub.rows["system_stats"]
                   if r["system"] == MACMINI_ID and r["type"] == "1m"
                   and r["stats"]["m"] == MEM_TOTAL_GIB[MACMINI_ID])
    # A hub that ignores the filter entirely and answers every stats read with one host's row.
    hub.rows["system_stats"] = [mac_row]
    readings = _by_id(_provider(hub).hosts())
    assert readings[MACMINI_ID].metrics.mem_total_gib == MEM_TOTAL_GIB[MACMINI_ID]
    for system_id in (LOCAL_ID, UNRAID_ID, SOLAR_ID):
        assert readings[system_id].metrics is None
        assert readings[system_id].liveness.reason == NO_SYSTEM_ROW


def test_a_system_id_that_is_not_one_never_reaches_a_url():
    hub = _Hub()
    provider = _provider(hub)
    for hostile in ("x'||1", "a' && system='b", "a b", "", "x" * 65, None, 7):
        reading = provider.containers(hostile)
        assert reading.liveness.state == UNKNOWN
        assert reading.liveness.reason == BAD_ID
        assert reading.containers is None
    assert hub.paths == []      # not even an auth post: there was nothing to ask about


# ── the summary blob PE must never read ──────────────────────────────────────────

def test_the_source_never_names_the_systems_summary_blob():
    source = Path(beszel.__file__).read_text()
    for literal in ('"info"', "'info'"):
        assert literal not in source, (
            f"{literal} appears in beszel.py: `t` is an integer thread count in that blob and a "
            "sensor->temperature map in a stats blob, which is why system_details is read instead")


def test_values_come_from_the_stats_blob_and_never_from_the_summary_one():
    hub = _Hub()
    # Poison every summary blob with values that would be obvious if they were ever read:
    # `t` as the integer it is there, and a cpu/mp that no stats row carries.
    for row in hub.rows["systems"]:
        row["info"] = {"cpu": 99.9, "mp": 99.9, "t": 4, "m": 999.0, "v": "0.20.0"}
    readings = _by_id(_provider(hub).hosts())
    for system_id, reading in readings.items():
        assert reading.metrics.cpu_pct != 99.9
        assert reading.metrics.mem_pct != 99.9
        assert reading.metrics.mem_total_gib == MEM_TOTAL_GIB[system_id]
        # `t` decoded as a map, not as a count. An integer there would be a thread count.
        assert reading.metrics.temps is None or all(
            isinstance(k, str) for k in reading.metrics.temps)


# ── units, and absent fields ─────────────────────────────────────────────────────

def test_memory_is_bytes_in_details_and_gibibytes_in_stats():
    readings = _by_id(_provider(_Hub()).hosts())
    local = readings[LOCAL_ID]
    assert local.host.details.memory_bytes == 16688291840      # bytes, from system_details
    assert local.metrics.mem_total_gib == 15.54                # GiB, from the stats blob
    # The same stick of RAM. Guarded because the two collections differ by 1e9 and a mapper
    # that confused them would render 16 GiB as 16 billion.
    assert abs(local.host.details.memory_bytes / 1024 ** 3 - local.metrics.mem_total_gib) < 0.01


def test_an_empty_os_name_is_unknown_and_not_a_measurement():
    # Unraid's 0.17.0 agent reports "". An empty string on the page looks like a reading of
    # nothing; None is what a renderer can show as unknown.
    readings = _by_id(_provider(_Hub()).hosts())
    assert readings[UNRAID_ID].host.details.os_name is None
    assert readings[LOCAL_ID].host.details.os_name == "Ubuntu 24.04.4 LTS"


def test_a_missing_reading_is_none_but_a_zero_reading_is_zero():
    hub = _Hub()
    for row in hub.rows["system_stats"]:
        if row["system"] == SOLAR_ID and row["type"] == "1m":
            # An idle host really does report 0% CPU, and an agent too old to report disk
            # percent sends nothing at all. Those are different answers.
            row["stats"] = {"cpu": 0, "m": 0.89, "la": [0, 0, 0]}
    reading = _by_id(_provider(hub).hosts())[SOLAR_ID]
    assert reading.metrics.cpu_pct == 0.0            # a measurement of zero survives
    assert reading.metrics.disk_pct is None          # an absent field does not become one
    assert reading.metrics.mem_pct is None
    assert reading.metrics.temps is None
    assert reading.metrics.load == (0.0, 0.0, 0.0)


def test_unusable_numbers_and_strings_decode_to_none():
    hub = _Hub()
    for row in hub.rows["system_stats"]:
        if row["type"] == "1m":
            row["stats"] = {"cpu": "lots", "mp": True, "m": None, "d": [1],
                            "la": [1.0, "x", 3.0], "t": {"acpitz": "warm", "k10": 41.0}}
    reading = _by_id(_provider(hub).hosts())[LOCAL_ID]
    assert reading.metrics.cpu_pct is None
    assert reading.metrics.mem_pct is None           # True is an int; it is not 100%
    assert reading.metrics.mem_total_gib is None
    assert reading.metrics.disk_total_gib is None
    assert reading.metrics.load is None              # a partly-decoded triple is not a triple
    assert reading.metrics.temps == {"k10": 41.0}    # the unreadable sensor drops, not the lot


def test_a_status_the_collector_invented_is_not_echoed_onto_the_page():
    hub = _Hub()
    hub.rows["systems"][0]["status"] = "<b>up</b>"
    assert _by_id(_provider(hub).hosts())[MACMINI_ID].host.status is None


def test_a_collector_name_is_bounded_and_the_provider_never_supplies_a_link():
    hub = _Hub()
    hub.rows["systems"][0]["name"] = "n" * 5000
    reading = _by_id(_provider(hub).hosts())[MACMINI_ID]
    assert len(reading.host.name) == beszel.MAX_TEXT
    # The link comes from the inventory alone -- the collector stores none, and a link is
    # exactly the field PE would not let another host supply.
    assert all(r.host.link is None for r in _provider(hub).hosts().hosts)


# ── containers ───────────────────────────────────────────────────────────────────

def test_a_host_with_no_containers_is_empty_and_a_host_that_could_not_be_read_is_not():
    hub = _Hub()
    provider = _provider(hub)
    # CASA SOLAR ASSISTANT is an appliance, not a docker host: it really reports none.
    empty = provider.containers(SOLAR_ID)
    assert empty.containers == ()
    assert empty.liveness.state == CURRENT

    unreadable = _provider(_Refusing()).containers(SOLAR_ID)
    assert unreadable.containers is None           # never (), which would read as "has none"
    assert unreadable.liveness.state == UNKNOWN


def test_containers_decode_to_pe_types_with_the_measured_counts():
    provider = _provider(_Hub())
    assert len(provider.containers(LOCAL_ID).containers) == 85
    assert len(provider.containers(UNRAID_ID).containers) == 6
    unraid = {c.name: c for c in provider.containers(UNRAID_ID).containers}
    assert "beszel-agent" in unraid                # the one name every host in the fleet has
    adguard = unraid["CASA_ADGUARD_SECONDARY"]
    assert adguard.image == "adguard/adguardhome:latest"
    assert adguard.updatable is True               # stored as 1; a tri-state, not a truthiness
    # No labels anywhere: the collector's table has none, so a label authored on another host
    # cannot reach the icon path on this one.
    assert not any(hasattr(c, "labels") for c in provider.containers(LOCAL_ID).containers)


def test_updatable_is_a_tri_state_and_an_undecodable_health_code_is_unknown():
    hub = _Hub()
    rows = [r for r in hub.rows["containers"] if r["system"] == UNRAID_ID]
    rows[0].update(updatable=None, health=7)       # a code never seen on the host
    rows[1].update(updatable=True, health="healthy")
    rows[2].update(updatable="yes", health=None)
    decoded = {c.name: c for c in _provider(hub).containers(UNRAID_ID).containers}
    by_name = [rows[0]["name"], rows[1]["name"], rows[2]["name"]]
    assert decoded[by_name[0]].updatable is None
    # Codes 0 and 2 were measured against the docker socket and now decode; see HEALTH_CODES.
    # Any other code is still unknown rather than inferred from the ordering, which is the
    # `t`-means-two-things mistake with a different letter.
    assert decoded[by_name[0]].health is None
    assert decoded[by_name[1]].updatable is True and decoded[by_name[1]].health == "healthy"
    assert decoded[by_name[2]].updatable is None   # "yes" is not an answer the collector gives


def test_a_container_row_with_no_name_is_dropped_rather_than_rendered_nameless():
    hub = _Hub()
    hub.rows["containers"] = [{"system": UNRAID_ID, "name": "", "image": "x"},
                              {"system": UNRAID_ID, "image": "y"},
                              {"system": UNRAID_ID, "name": "real", "image": "z"}]
    assert [c.name for c in _provider(hub).containers(UNRAID_ID).containers] == ["real"]


# ── liveness ─────────────────────────────────────────────────────────────────────

def test_a_reading_is_aged_on_the_stats_row_not_on_the_system_record():
    hub = _Hub()
    # A host whose agent stopped reporting an hour ago, while the hub keeps touching the
    # system record. Ageing on `systems.updated` would call this current.
    for row in hub.rows["systems"]:
        row["updated"] = "2026-10-01 10:56:59.000Z"
    for row in hub.rows["system_stats"]:
        if row["system"] == UNRAID_ID and row["type"] == "1m":
            row["created"] = "2026-10-01 09:50:00.000Z"
    readings = _by_id(_provider(hub).hosts())
    assert readings[UNRAID_ID].liveness.state == STALE
    assert readings[UNRAID_ID].liveness.age > STALE_AFTER
    assert readings[LOCAL_ID].liveness.state == CURRENT


def test_a_stats_row_without_a_blob_is_unknown_rather_than_an_empty_reading():
    hub = _Hub()
    for row in hub.rows["system_stats"]:
        if row["system"] == SOLAR_ID and row["type"] == "1m":
            row["stats"] = "not a blob"
    reading = _by_id(_provider(hub).hosts())[SOLAR_ID]
    assert reading.metrics is None
    assert reading.liveness.state == UNKNOWN and reading.liveness.reason


def test_a_host_whose_details_are_missing_still_renders_with_its_identity():
    hub = _Hub()
    hub.rows["system_details"] = [r for r in hub.rows["system_details"]
                                 if r["system"] != UNRAID_ID]
    reading = _by_id(_provider(hub).hosts())[UNRAID_ID]
    assert reading.host.details is None
    assert reading.host.name == "CASA UNRAID"      # identity does not depend on the hardware row
    assert reading.metrics.mem_total_gib == MEM_TOTAL_GIB[UNRAID_ID]


# ── authentication ───────────────────────────────────────────────────────────────

def test_the_token_is_fetched_once_and_reused_across_every_read():
    hub = _Hub()
    provider = _provider(hub)
    provider.hosts()
    provider.containers(LOCAL_ID)
    provider.containers(UNRAID_ID)
    assert hub.auths == 1, "the token is re-fetched per request"
    assert len(hub.gets) == 1 + 4 * 2 + 2          # systems + details/stats per host + two reads


def test_the_auth_post_sends_identity_and_password_in_the_body():
    hub = _Hub()
    _provider(hub).hosts()
    assert json.loads(hub.bodies[0]) == {"identity": IDENTITY, "password": PASSWORD}


def test_a_401_buys_exactly_one_reauth_and_not_a_loop():
    hub = _Hub()
    provider = _provider(hub)
    provider.hosts()
    assert hub.auths == 1
    # The hub revokes the session: every authenticated GET now answers 401.
    hub.token = "a-rotated-token"
    reading = provider.hosts()
    assert hub.auths == 2, "a 401 did not trigger a re-auth"
    # And it worked, because the new token is kept rather than re-fetched per request.
    assert len(reading.hosts) == 4
    assert hub.auths == 2


def test_a_hub_that_rejects_every_token_costs_one_extra_auth_for_the_whole_call():
    hub = _Hub(reject_tokens=True)
    reading = _provider(hub).hosts()
    # One initial auth plus the one re-auth the attempt is allowed. Not one per request, and
    # not a loop: a fleet read issues nine GETs and must not become nine auth posts.
    assert hub.auths == 2
    assert reading.liveness.state == UNKNOWN
    assert reading.liveness.reason == AUTH_REFUSED


def test_credentials_that_are_not_configured_are_a_reason_and_not_a_request():
    hub = _Hub()
    reading = _provider(hub, credentials=lambda: ("", "")).hosts()
    assert reading.liveness.state == UNKNOWN
    assert reading.liveness.reason == NOT_CONFIGURED
    assert reading.hosts == ()
    # An install without the account yet does not talk to the collector at all.
    assert hub.paths == []


@pytest.mark.parametrize("password", ["pässwörd-with-an-accent", "pass\r\nX-Evil: 1",
                                      "pass word", 'quote"and\\backslash'])
def test_a_password_the_operator_chose_is_sent_as_json_and_not_as_a_header(password):
    """The identity and password go into a UTF-8 JSON body, never into a header, so the widget
    fetcher's latin-1 rule does not apply to them. Applying it anyway rendered a valid password
    with an accent in it as permanently unauthenticated (Codex review, S2). json.dumps() escapes
    every control character and emits ASCII, so a CR or LF cannot forge a request line either.
    """
    hub = _Hub()
    reading = _provider(hub, credentials=lambda: (IDENTITY, password)).hosts()
    assert reading.liveness.state == CURRENT
    assert json.loads(hub.bodies[0]) == {"identity": IDENTITY, "password": password}
    assert hub.bodies[0].decode("ascii")        # escaped on the wire, whatever was configured


def test_a_token_that_cannot_go_in_a_header_is_a_reason_and_not_an_encoder_crash():
    # The token IS a header value, so this one is checked -- and the reason names no part of it.
    hub = _Hub(token="tok\r\nX-Evil: 1")
    reading = _provider(hub).hosts()
    assert reading.liveness.reason == AUTH_REFUSED
    assert "Evil" not in reading.liveness.reason


def test_an_auth_refusal_is_a_reason_naming_no_credential():
    reading = _provider(_Hub(auth_status=400)).hosts()
    assert reading.liveness.reason == AUTH_REFUSED
    assert PASSWORD not in reading.liveness.reason and IDENTITY not in reading.liveness.reason


def test_no_secret_reaches_the_logs_or_a_rendered_reason(caplog):
    caplog.set_level(logging.DEBUG)
    hub = _Hub()
    provider = _provider(hub)
    provider.hosts()
    hub.token = "rotated"                  # forces the 401 path, which logs
    provider.hosts()
    provider.containers(LOCAL_ID)
    _provider(_Hub(auth_status=400)).hosts()
    _provider(_Refusing()).hosts()
    written = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (PASSWORD, TOKEN, "rotated"):
        assert secret not in written, "a secret reached the logs"
    reasons = [r.liveness.reason or "" for r in [_provider(_Hub(auth_status=400)).hosts(),
                                                 _provider(_Refusing()).hosts()]]
    for reason in reasons:
        assert PASSWORD not in reason and TOKEN not in reason


# ── failure, every kind of it ────────────────────────────────────────────────────

def test_an_unreachable_collector_is_the_whole_fleet_unknown_and_never_an_exception():
    factory = _Refusing()
    reading = _provider(factory).hosts()
    assert isinstance(reading, FleetReading)
    assert reading.liveness.state == UNKNOWN
    assert "unreachable" in reading.liveness.reason
    assert reading.hosts == ()
    # No retry storm: one connect for the auth post, and the failure is reported.
    assert factory.connects == 1


def test_authenticated_but_scoped_to_nothing_is_its_own_reason():
    hub = _Hub()
    hub.rows["systems"] = []               # HTTP 200, items: [] -- the listRule hiding everything
    reading = _provider(hub).hosts()
    assert reading.liveness.state == UNKNOWN
    reason = reading.liveness.reason
    assert reason == SCOPED_TO_NOTHING
    # The distinction this exists for. It happened during setup and it is invisible unless the
    # reason says it: the account authenticated, so this is neither an empty fleet nor a dead
    # collector.
    assert "listed on any system" in reason
    assert "no hosts" not in reason.lower()
    assert "down" not in reason.lower() and "unreachable" not in reason.lower()
    assert reason != _provider(_Refusing()).hosts().liveness.reason


@pytest.mark.parametrize("hub,expected", [
    (_Hub(get_status=500), "answered 500"),
    (_Hub(get_status=403), "answered 403"),
    (_Hub(body=b"<html>not json</html>"), "not JSON"),
    (_Hub(body=b'{"items": "not a list"}'), "not a record list"),
    (_Hub(body=b"[]"), "not a record list"),
    (_Hub(body=b'{"items":[]}', headers={"Content-Encoding": "gzip"}), "compressed"),
    (_Hub(body=b'{"items":[{"id":"abc","stats":{"cpu":Infinity}}]}'), "not JSON"),
    (_Hub(body=b'{"items":[]}' + b" " * (beszel.MAX_BYTES_PER_ANSWER + 1)), "too large"),
])
def test_every_bad_answer_is_an_unknown_with_a_reason(hub, expected):
    reading = _provider(hub).hosts()
    assert reading.liveness.state == UNKNOWN
    assert expected in reading.liveness.reason
    assert reading.hosts == ()


def test_a_number_json_allows_but_arithmetic_does_not_decodes_to_none():
    """Two layers, because they catch different things.

    A literal `Infinity` on the wire is refused outright (the parametrized case above), but
    `1e999` is ordinary JSON that decodes to inf in four bytes, and `10**400` decodes to an int
    that `math.isfinite` raises OverflowError on -- so the guard cannot be the wire cap, it has
    to be the decoder. Either would otherwise reach the page as a measurement, and the renderer
    would raise rounding it.
    """
    metrics = beszel._metrics_from({"cpu": 1e999, "m": 10 ** 400, "mp": 54.26,
                                    "la": [1e999, 1.0, 1.0], "t": {"acpitz": 1e999}})
    assert metrics.cpu_pct is None
    assert metrics.mem_total_gib is None
    assert metrics.load is None
    assert metrics.temps is None
    assert metrics.mem_pct == 54.26                  # the readable field beside it survives


def test_a_collector_that_never_answers_is_bounded_by_the_watchdog():
    hub = _Hub(block=True)
    started = time.monotonic()
    # The budget, not the per-call timeout, is what this exercises: the call has to come back.
    reading = _provider(hub, budget=0.3).hosts()
    assert time.monotonic() - started < 8, "the call was not bounded"
    assert reading.liveness.state == UNKNOWN
    assert reading.liveness.reason in (TIMED_OUT, OUT_OF_TIME)
    # The socket was shut down, not merely closed: http.client hands it to the response and
    # drops its own reference, so close() can leave the read running.
    assert hub.sockets and all(s.shut.is_set() for s in hub.sockets)
    # And the socket carried a timeout as well, so an idle peer cannot hold it either.
    assert all(s.timeouts for s in hub.sockets)


def test_the_call_budget_stops_a_slow_collector_from_holding_the_whole_fleet():
    hub = _Hub()
    # Two seconds per request against a 15-second budget: the listing and the first hosts are
    # read, and the fleet does not cost four hosts times the per-request timeout.
    reading = _provider(hub, clock=lambda: 2.0 * len(hub.paths), budget=15).hosts()
    assert reading.liveness.state == CURRENT       # the listing was read
    out_of_time = [r for r in reading.hosts if r.liveness.reason == OUT_OF_TIME]
    assert out_of_time, "the per-call budget never applies"
    assert all(r.metrics is None for r in out_of_time)
    # Identity survives: a host PE ran out of time on still renders, with the reason.
    assert all(r.host.id for r in out_of_time)


def test_one_unreadable_host_does_not_fail_the_other_three():
    hub = _Hub()
    real_select = hub._select

    def flaky(path):
        if f"system='{UNRAID_ID}'" in urllib.parse.unquote(path):
            raise OSError("that one host's read fell over")
        return real_select(path)

    hub._select = flaky
    reading = _provider(hub).hosts()
    assert reading.liveness.state == CURRENT
    readings = _by_id(reading)
    assert readings[UNRAID_ID].liveness.state == UNKNOWN
    for system_id in (LOCAL_ID, MACMINI_ID, SOLAR_ID):
        assert readings[system_id].metrics.mem_total_gib == MEM_TOTAL_GIB[system_id]


def test_a_system_row_without_a_usable_id_is_skipped_not_guessed_at():
    hub = _Hub()
    hub.rows["systems"] = hub.rows["systems"] + [
        {"id": "", "name": "no id"}, {"id": "has a space", "name": "bad id"},
        {"name": "no id key"}, {"id": 7, "name": "not a string"}]
    reading = _provider(hub).hosts()
    assert sorted(r.host.id for r in reading.hosts) == sorted(MEM_TOTAL_GIB)


# ── the fixture provider ─────────────────────────────────────────────────────────

def test_the_fixture_provider_is_the_same_seam():
    assert isinstance(FixtureHostProvider(), HostProvider)
    assert isinstance(BeszelHubProvider(credentials=lambda: ("a", "b")), HostProvider)


def test_the_fixture_provider_reads_the_capture_into_pe_types():
    provider = FixtureHostProvider(wall=lambda: beszel.parse_timestamp("2026-10-01 10:57:00.000Z"))
    reading = provider.hosts()
    assert reading.liveness.state == CURRENT
    readings = _by_id(reading)
    assert set(readings) == set(MEM_TOTAL_GIB)
    for system_id, expected in MEM_TOTAL_GIB.items():
        assert readings[system_id].metrics.mem_total_gib == expected
        assert readings[system_id].liveness.state == CURRENT
    assert readings[UNRAID_ID].host.details.os_name is None
    assert readings[LOCAL_ID].host.details.memory_bytes == 16688291840
    assert all(isinstance(r.host, Host) for r in reading.hosts)
    assert all(r.host.details is None or isinstance(r.host.details, HostDetails)
               for r in reading.hosts)
    assert len(provider.containers(LOCAL_ID).containers) == 85
    assert provider.containers(SOLAR_ID).containers == ()


def test_the_fixture_provider_picks_each_system_s_own_newest_1m_row(tmp_path):
    """The test double has to be as strict as the hub reader. A fixture provider that handed
    back the fleet's newest row would make every "did this host get its own numbers?" test
    pass for the wrong reason."""
    for name in ("systems", "system_details", "containers"):
        (tmp_path / f"{name}.json").write_text(json.dumps({"items": _capture(name)}))
    (tmp_path / "system_stats_1m_newest.json").write_text(json.dumps({"items": _stats_rows()}))
    readings = _by_id(FixtureHostProvider(
        tmp_path, wall=lambda: beszel.parse_timestamp("2026-10-01 10:57:00.000Z")).hosts())
    for system_id, expected in MEM_TOTAL_GIB.items():
        # Not 999.0 (the older 1m row, first in file order) and not 888.0 (the newer 10m
        # rollup), which is the sort and the bucket clause doing their work client-side.
        assert readings[system_id].metrics.mem_total_gib == expected


def test_the_fixture_provider_reports_a_missing_capture_as_unknown(tmp_path):
    reading = FixtureHostProvider(tmp_path / "nothing-here").hosts()
    assert reading.liveness.state == UNKNOWN
    assert reading.liveness.reason and reading.hosts == ()
    containers = FixtureHostProvider(tmp_path / "nothing-here").containers(LOCAL_ID)
    assert containers.containers is None           # never (), which would read as "has none"


def test_the_fixture_provider_refuses_a_hostile_system_id():
    reading = FixtureHostProvider().containers("a' && system='b")
    assert reading.liveness.reason == BAD_ID
    assert reading.containers is None


# ── the boundary ─────────────────────────────────────────────────────────────────

def test_nothing_here_reaches_the_action_layer():
    source = Path(beszel.__file__).read_text()
    # Phase one is observe-only. No remote value may reach the action layer, and the cheapest
    # way to keep that true is for this module not to be able to name it.
    for forbidden in ("planet_express.execution", "REGISTRY", "resolve_stack_target"):
        assert forbidden not in source


def test_the_readings_are_pe_types_and_frozen():
    reading = _provider(_Hub()).hosts()
    assert isinstance(reading, FleetReading)
    assert all(isinstance(r, HostReading) for r in reading.hosts)
    assert isinstance(_provider(_Hub()).containers(LOCAL_ID), ContainerReading)
    with pytest.raises(FrozenInstanceError):
        reading.hosts[0].host.id = "mutated"


def test_no_pocketbase_record_escapes_the_module():
    """Every value handed out is one of PE's own types or a plain scalar. A dict would be a
    collector record, and a record on the far side of the seam is a collector PE cannot swap."""
    reading = _provider(_Hub()).hosts()
    for host_reading in reading.hosts:
        assert not isinstance(host_reading.host.name, dict)
        assert host_reading.metrics is None or all(
            isinstance(v, (float, int, tuple, type(None))) or k == "temps"
            for k, v in vars(host_reading.metrics).items())
        assert host_reading.host.details is None or all(
            isinstance(v, (str, int, type(None)))
            for v in vars(host_reading.host.details).values())
    for container in _provider(_Hub()).containers(LOCAL_ID).containers:
        assert all(isinstance(v, (str, float, int, bool, type(None)))
                   for v in vars(container).values())


# --- health codes, only where they were measured ------------------------------------------
# Measured on the local docker socket 2026-10-01: code 2 was `healthy` 43/43, code 0 was a
# container with no healthcheck 33/33. Nothing was unhealthy or starting, so those codes are
# deliberately absent and must not be inferred from the ordering.

def test_measured_health_codes_decode():
    from planet_express.integrations.beszel import _health
    assert _health(2) == "healthy"
    assert _health(0) is None, "no healthcheck is absent, not a state"


def test_unmeasured_health_codes_are_unknown_not_guessed():
    from planet_express.integrations.beszel import _health
    for code in (1, 3, 4, 99, -1):
        assert _health(code) is None, f"code {code} was never measured and must not be invented"


def test_a_health_word_still_passes_through():
    from planet_express.integrations.beszel import _health
    assert _health("healthy") == "healthy"
    assert _health("unhealthy") == "unhealthy"
    assert _health("starting") == "starting"


def test_health_booleans_are_not_treated_as_codes():
    from planet_express.integrations.beszel import _health
    assert _health(True) is None and _health(False) is None


def test_the_fixture_decodes_the_way_the_host_measured():
    import json
    from pathlib import Path

    from planet_express.integrations.beszel import _health
    rows = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "beszel" / "containers.json").read_text()
    )["items"]
    decoded = [_health(r.get("health")) for r in rows]
    assert decoded.count("healthy") == sum(1 for r in rows if r.get("health") == 2)
    assert all(d in (None, "healthy") for d in decoded)
    assert decoded.count("healthy") > 0, "the capture must still contain healthy containers"
