"""Fetch one container's widget from the app's own API, and hand back only its summary.

This is the one part of v2.2 with a security surface: an outbound request that carries a
credential, to an address a container holds, whose answer is parsed and put on a page. The
rules, each of which exists because the alternative leaks something or falls over:

  * GET only, to paths the widget file declares -- never a path from the request.
  * The address comes from core, fresh, and is on a docker BRIDGE network (core checks the
    driver; a macvlan address is on the physical LAN). Here it must also be RFC 1918. The
    port comes from the widget file. Nothing the browser sends picks where the request goes.
  * Core hands over a KEYED widget only for an image pulled from one of the widget's exact
    (registry, repo) pairs, never by label alone, never across a shared network namespace.
    That is hardening, not the boundary: the boundary is "containers the operator approved",
    since an approved compose also sets entrypoint, volumes and env.
  * Only a failure to CONNECT, before anything has been sent to an address, moves on to the
    next one. Once a request may have been delivered, its key has gone to that address, and
    sending it to another is not a retry.
  * Plain `http.client`, not `requests`. requests reads -- and gunzips -- the whole body of a
    3xx answer inside session.get() even with redirects off, and urllib3 reads chunk-size and
    trailer lines without a length limit; either lets an app answer a few hundred KB and cost
    the dashboard gigabytes before any cap here runs. http.client follows nothing, reads no
    body it was not asked for, and caps every header, chunk and trailer line at 64 KiB.
  * The body is read with read1() against one byte budget for the whole fetch, identity
    encoding only (a compressed answer is refused, never decoded), strict JSON.
  * Time is bounded on the socket itself: a watchdog shuts the socket down when a call's
    time is up, which wakes a read blocked on a server trickling a byte at a time. The fetch
    runs in a background slot so the request thread waits the budget and no longer.
  * A widget reads only env vars named after itself (registry.env_name_problem), and the
    values go into the request and nowhere else -- not the answer, not the logs.
  * The strings in the answer are DATA. Anything the app returned -- a queue item's title --
    is in it verbatim; the page must render every field as text (textContent), never markup.
  * A widget never changes container health. Whatever happens here is the widget's state.

All limits here are per dashboard process; the unit runs two gunicorn workers.
"""

import base64
import functools
import http.client
import ipaddress
import json
import logging
import math
import socket
import threading
import time
from datetime import datetime, timezone

from planet_express.widgets import registry

log = logging.getLogger("planetexpress.widgets.fetcher")

CALL_TIMEOUT_SECONDS = 3          # per call, as the spec says -- wall clock, not per read
CONNECT_TIMEOUT_SECONDS = 1       # an address the host cannot reach should fail fast
FETCH_BUDGET_SECONDS = 6          # every call of one widget together
CACHE_SECONDS = 30
FAILED_CACHE_SECONDS = 5          # a timeout or busy answer, so a stuck app is not re-asked per view
MAX_INFLIGHT = 2                  # background fetches at once, per process
# Per fetch, every path together. Parsed JSON costs ~25x its wire size, so this is a memory
# ceiling: the shipped widgets' paged answers are tens of KB.
DEFAULT_MAX_BYTES = 256 * 1024
_LEVELS = {"ok", "warn", "crit"}
_MAX_TEXT = 120
_MAX_NUMBER = 10 ** 15
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class _Failure(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _usable_addresses(addresses) -> list[str]:
    usable = []
    for value in addresses or []:
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        # Docker's bridge pools are RFC 1918. Anything else -- loopback, link-local (cloud
        # metadata lives there), CGNAT, reserved, public -- is not an address to send a key to.
        if address.version == 4 and any(address in net for net in _RFC1918):
            usable.append(str(address))
    return usable


def _usable_credential(value: str) -> bool:
    # HTTP carries header values as latin-1 with no line breaks. Checked here so an unusable
    # key is a fixed reason, never an encoder exception whose message names a character of
    # the secret and its position.
    if "\r" in value or "\n" in value:
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _credentials(widget: dict, env) -> tuple[dict | None, list[str]]:
    """(auth headers, missing env names). The values go into the request only."""
    auth = widget.get("auth")
    if auth is None:
        return {}, []
    names = registry.auth_env_names(auth)
    for name in names:
        if registry.env_name_problem(widget["name"], name) is not None:
            # The registry already refuses this; checked again because this is the line that
            # sends the value somewhere.
            raise _Failure("widget declaration refused")
    values = {name: (env.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        return None, missing
    if not all(_usable_credential(value) for value in values.values()):
        raise _Failure("the configured key cannot be sent over HTTP")
    if auth["type"] == "header":
        return {auth["header"]: values[auth["env"]]}, []
    pair = f'{values[auth["username_env"]]}:{values[auth["password_env"]]}'.encode("latin-1")
    return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}, []


def _shutdown_sock(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# Bytes one call's status line and headers may take, on top of the body budget (which also
# pays for chunk framing and trailers). http.client alone allows ~100 header lines of 64 KiB.
HEADER_ALLOWANCE_BYTES = 256 * 1024


class _Budget:
    """Bytes left for the whole fetch. Two counters: the BODY limit is shared by every path of
    one widget, and each call's status line and headers get their own allowance, reset per
    call, which body bytes can never spend."""

    def __init__(self, limit: int):
        self.left = limit
        self.header_left = 0
        self.in_headers = False

    def begin_headers(self) -> None:
        self.header_left = HEADER_ALLOWANCE_BYTES
        self.in_headers = True

    def end_headers(self) -> None:
        self.in_headers = False

    def room(self) -> int:
        return self.header_left if self.in_headers else self.left

    def spend(self, count: int) -> None:
        if self.in_headers:
            self.header_left -= count
            if self.header_left < 0:
                raise _Failure("answer too large")
            return
        self.left -= count
        if self.left < 0:
            raise _Failure("answer too large")


class _CappedReader:
    """The response's file object, charging every byte it returns to the fetch's budget.
    http.client caps each LINE; this caps the lot, and never asks for more than is left."""

    def __init__(self, fp, budget: _Budget):
        self._fp = fp
        self._budget = budget

    def _bound(self, n):
        room = max(0, self._budget.room()) + 1
        return room if n is None or n < 0 else min(n, room)

    def readline(self, limit=-1):
        data = self._fp.readline(self._bound(limit))
        self._budget.spend(len(data))
        return data

    def read(self, n=-1):
        data = self._fp.read(self._bound(n))
        self._budget.spend(len(data))
        return data

    def read1(self, n=-1):
        data = self._fp.read1(self._bound(n))
        self._budget.spend(len(data))
        return data

    def readinto(self, buffer):
        view = memoryview(buffer)[:self._bound(len(buffer))]
        count = self._fp.readinto(view)
        self._budget.spend(count or 0)
        return count

    def flush(self):
        pass

    def readable(self):
        return True

    def peek(self, n=0):
        return self._fp.peek(n)

    def close(self):
        self._fp.close()

    @property
    def closed(self):
        return self._fp.closed


class _CappedResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, budget: _Budget, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp = _CappedReader(self.fp, budget)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def http_get_json(address: str, port: int, path: str, *, headers: dict, timeout: float,
                  budget: _Budget, connection_factory=http.client.HTTPConnection):
    """One GET. Raises _Failure; `reason == "unreachable"` means nothing was sent."""
    conn = connection_factory(address, port, timeout=min(CONNECT_TIMEOUT_SECONDS, timeout))
    if isinstance(conn, http.client.HTTPConnection):
        conn.response_class = functools.partial(_CappedResponse, budget=budget)
    fired = threading.Event()
    held = {}

    def expire():
        fired.set()
        # The socket held since connect: http.client drops conn.sock the moment it reads a
        # response that will close the connection, and the read goes on regardless.
        _shutdown_sock(held.get("sock"))
    # Started before connect(), so the connect counts against the call's wall clock too.
    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        try:
            conn.connect()
        except OSError:
            raise _Failure("unreachable") from None
        held["sock"] = conn.sock
        if fired.is_set():
            _shutdown_sock(held["sock"])
            raise _Failure("timeout")
        try:
            conn.sock.settimeout(timeout)
            conn.request("GET", path, headers={**headers, "Accept": "application/json",
                                               "Accept-Encoding": "identity"})
        except (OSError, http.client.HTTPException, ValueError):
            raise _Failure("connection dropped") from None
        try:
            budget.begin_headers()
            try:
                resp = conn.getresponse()
            finally:
                budget.end_headers()
            if resp.status != 200:
                raise _Failure(f"answered {resp.status}", resp.status)
            encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                # Identity was asked for; decoding what came anyway is a decompression bomb.
                raise _Failure("answer was compressed")
            body = bytearray()
            # Normally the capped reader under `resp` has already charged each byte; a
            # response without one (another connection class) is charged here instead.
            charged = isinstance(getattr(resp, "fp", None), _CappedReader)
            while True:
                chunk = resp.read1(64 * 1024)
                if not chunk:
                    break
                if not charged:
                    budget.spend(len(chunk))
                body.extend(chunk)
            # After the watchdog's shutdown() a read returns EOF rather than raising, and a
            # server may simply close early: either way the body is not whole.
            if fired.is_set():
                raise _Failure("timeout")
            if resp.length:
                raise _Failure("connection dropped")
        except _Failure:
            raise
        except socket.timeout:
            raise _Failure("timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            # Includes the watchdog's shutdown, an over-long line, too many headers, bad chunks.
            raise _Failure("timeout" if fired.is_set() else "connection dropped") from None
    finally:
        watchdog.cancel()
        conn.close()
    try:
        return json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _Failure("answer was not JSON") from None


def _text(value) -> str:
    return str(value)[:_MAX_TEXT]


def _value(value):
    if isinstance(value, bool) or value is None:
        return "—" if value is None else _text(value)
    if isinstance(value, int):
        return value if abs(value) < _MAX_NUMBER else _text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) and abs(value) < _MAX_NUMBER else "—"
    return _text(value)


def normalise_summary(summary) -> dict:
    """Clamp summarise()'s output to the contract: 1-4 stats, up to 3 rows, short strings,
    finite numbers. Structure, size and number range are bounded here; the strings are still
    the app's own words, and the page renders them as text."""
    if not isinstance(summary, dict):
        raise _Failure("widget summary malformed")
    stats = []
    raw_stats = summary.get("stats") or []
    for stat in raw_stats[:20] if isinstance(raw_stats, list) else []:
        if len(stats) == 4:
            break
        if not isinstance(stat, dict) or "k" not in stat:
            continue
        item = {"k": _text(stat["k"])[:24], "v": _value(stat.get("v"))}
        if stat.get("level") in _LEVELS:
            item["level"] = stat["level"]
        stats.append(item)
    if not stats:
        raise _Failure("widget summary malformed")
    rows = []
    raw_rows = summary.get("rows") or []
    for row in raw_rows[:20] if isinstance(raw_rows, list) else []:
        if len(rows) == 3:
            break
        if not isinstance(row, dict):
            continue
        item = {"title": _text(row.get("title", ""))}
        pct = row.get("pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and math.isfinite(pct):
            item["pct"] = max(0, min(100, round(pct)))
        if row.get("meta") is not None:
            item["meta"] = _text(row["meta"])
        rows.append(item)
    line = summary.get("line")
    return {"stats": stats, "rows": rows, "rows_label": _text(summary.get("rows_label") or "")[:24],
            "line": _text(line) if line else None}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class _Job:
    def __init__(self, started: float):
        self.done = threading.Event()
        self.answer = None
        self.started = started


class WidgetFetcher:
    """Per-process fetcher. Answers are cached per page key (the stack/service the page asked
    about), so a cache hit costs neither core nor the app anything; the last good values are
    kept per container instance, so a recreated container never shows its predecessor's.
    """

    def __init__(self, *, env, widgets=None, connection_factory=http.client.HTTPConnection,
                 clock=time.monotonic, wall=time.time, start=None):
        self._env = env
        self._widgets = widgets
        self._connection_factory = connection_factory
        self._clock = clock
        self._wall = wall
        self._start = start or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._inflight: dict = {}

    def cached(self, key) -> dict | None:
        """The answer for `key` if it is fresh, without asking core or the app."""
        with self._lock:
            entry = self._cache.get(key)
        if entry is not None and self._clock() - entry["at"] < entry["ttl"]:
            return entry["answer"]
        return None

    def now(self) -> float:
        """This fetcher's clock, for the route to stamp when it asked core (see fetch)."""
        return self._clock()

    def fetch(self, target: dict, key, asked: float | None = None) -> dict:
        """`target` is core's query.widget_target answer. Returns one of the four states.

        `asked` is when the caller asked core for `target` (now() before the call). Every
        store this request makes is ordered by it, so a request that resolved a predecessor
        container before its successor was answered can never displace that answer.
        """
        asked = self._clock() if asked is None else asked
        name = target.get("widget") if isinstance(target, dict) else None
        widgets = registry.load_widgets() if self._widgets is None else self._widgets
        widget = widgets.get(name) if isinstance(name, str) else None
        if widget is None:
            answer = {"state": "none"}
            self._store(key, target, answer)
            return answer
        busy = False
        instance = str(target.get("container_id", ""))
        job_key = (key, instance, widget["name"])
        with self._lock:
            entry = self._cache.get(key)
            if (entry is not None and self._clock() - entry["at"] < entry["ttl"]
                    and entry.get("instance") == instance
                    and entry["answer"].get("widget") == widget["name"]):
                # Stored -- good or not -- while this request was asking core: answer it, and
                # do not send the key again for an answer already in hand.
                return entry["answer"]
            # Keyed by instance and widget too: a joiner for a recreated container must not be
            # handed the answer of a job started for its predecessor.
            job = self._inflight.get(job_key)
            starting = False
            if job is None:
                if len(self._inflight) >= MAX_INFLIGHT:
                    busy = True
                else:
                    job = self._inflight[job_key] = _Job(self._clock())
                    starting = True
        if busy:
            return self._fail_fast(key, target, widget, "busy", asked)
        if starting:
            try:
                self._start(lambda: self._run(job, job_key, key, target, widget, asked))
            except Exception:  # noqa: BLE001 -- e.g. no thread to start: release the slot
                log.warning("Could not start a widget fetch", exc_info=True)
                with self._lock:
                    self._inflight.pop(job_key, None)
                job.answer = self._fail_fast(key, target, widget, "busy", asked)
                job.done.set()
        # A joiner waits only what is left of the job's own budget, not a fresh one.
        left = job.started + FETCH_BUDGET_SECONDS - self._clock()
        if job.done.wait(max(0.0, left)):
            return job.answer
        return self._fail_fast(key, target, widget, "timeout", asked)

    def _fail_fast(self, key, target, widget, reason: str, asked: float) -> dict:
        """A busy or timed-out answer: cached briefly, so a stuck app is not re-asked on every
        view, and never over a fresh good answer."""
        answer = self._with_stale(key, target, widget, {"state": "error", "error": reason})
        return self._store(key, target, answer, ttl=FAILED_CACHE_SECONDS, started=asked)

    def _run(self, job, job_key, key, target, widget, asked):
        answer = None
        try:
            answer = self._fetch_now(widget, target)
            if answer["state"] == "error":
                answer = self._with_stale(key, target, widget, answer)
            # What the cache kept: a fresh good answer, if one arrived while this failed.
            answer = self._store(key, target, answer, started=asked)
        except Exception:  # noqa: BLE001 -- one widget's failure is its own panel's
            log.warning("Widget %s fetch failed unexpectedly", widget["name"], exc_info=True)
            try:
                # Cached like any failure, or a broken widget is re-fetched -- key and all --
                # on every view.
                answer = self._fail_fast(key, target, widget, "request failed", asked)
            except Exception:  # noqa: BLE001
                answer = None
        finally:
            # The slot is released whatever happened above, or every later request for this
            # key would join a job that never finishes.
            with self._lock:
                self._inflight.pop(job_key, None)
            job.answer = answer or {"widget": widget["name"], "via": widget["get"][0],
                                    "state": "error", "error": "request failed"}
            job.done.set()

    def _store(self, key, target, answer, ttl=CACHE_SECONDS, started=None):
        instance = str(target.get("container_id", "")) if isinstance(target, dict) else ""
        name = answer.get("widget")
        with self._lock:
            # Merged against the entry current NOW, under the lock: a slow fetch that fails
            # must not drop the last good values a faster one stored meanwhile.
            current = self._cache.get(key)
            same = (current is not None and current.get("instance") == instance
                    and current.get("widget") == name)
            if (current is not None and not same and started is not None
                    and current["at"] > started):
                # A job for a predecessor (or for the widget this container had before an
                # edit) finishing after its successor's answer was stored: it is not news.
                return answer
            if (answer.get("state") == "error" and same
                    and self._clock() - current["at"] < current["ttl"]
                    and current["answer"].get("state") == "ok"):
                # No error -- a waiter's timeout, or a second job's passing failure -- replaces
                # a good answer that is still fresh; the caller gets the good one.
                return current["answer"]
            good = current.get("good") if same else None
            if answer.get("state") == "ok":
                good = answer
            self._cache[key] = {"at": self._clock(), "ttl": ttl, "answer": answer, "good": good,
                                "instance": instance, "widget": name}
            while len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))
            return answer

    def _with_stale(self, key, target, widget, answer):
        answer = {"widget": widget["name"], "via": widget["get"][0], **answer}
        instance = str(target.get("container_id", "")) if isinstance(target, dict) else ""
        with self._lock:
            current = self._cache.get(key)
        good = (current.get("good") if current and current.get("instance") == instance
                and current.get("widget") == widget["name"] else None)
        if good is not None:
            # The last good values, shown dimmed: the spec's "stale values stay visible".
            answer["stale"] = {k: good[k] for k in ("stats", "rows", "rows_label", "line")}
            answer["stale_at"] = good["fetched_at"]
        return answer

    def _fetch_now(self, widget: dict, target: dict) -> dict:
        base = {"widget": widget["name"], "via": widget["get"][0]}
        try:
            headers, missing = _credentials(widget, self._env)
        except _Failure as failure:
            return {**base, "state": "error", "error": failure.reason}
        if missing:
            return {**base, "state": "needs_key", "env": missing}
        addresses = _usable_addresses(target.get("addresses"))
        if not target.get("running") or not addresses:
            return {**base, "state": "error", "error": "not reachable from the dashboard"}
        deadline = self._clock() + FETCH_BUDGET_SECONDS
        responses, failure = None, None
        for address in addresses:
            try:
                responses = self._get_all(widget, address, headers, deadline)
                break
            except _Failure as e:
                failure = e
                if e.reason != "unreachable":
                    break
        if responses is None:
            failure = failure or _Failure("unreachable")
            answer = {**base, "state": "error", "error": failure.reason}
            if failure.status is not None:
                answer["status"] = failure.status
            return answer
        try:
            summary = normalise_summary(widget["summarise"](responses))
        except _Failure as failure:
            return {**base, "state": "error", "error": failure.reason}
        except Exception:  # noqa: BLE001 -- one widget's bug costs its own panel
            log.warning("Widget %s failed to summarise its answer", widget["name"], exc_info=True)
            return {**base, "state": "error", "error": "widget failed to read the answer"}
        return {**base, "state": "ok", "fetched_at": _iso(self._wall()), **summary}

    def _get_all(self, widget, address, headers, deadline) -> list:
        budget = _Budget(widget.get("max_bytes", DEFAULT_MAX_BYTES))
        responses = []
        for index, path in enumerate(widget["get"]):
            left = deadline - self._clock()
            if left <= 0:
                raise _Failure("timeout")
            try:
                responses.append(http_get_json(
                    address, widget["port"], path, headers=headers,
                    timeout=min(CALL_TIMEOUT_SECONDS, left), budget=budget,
                    connection_factory=self._connection_factory))
            except _Failure as failure:
                if failure.reason == "unreachable" and index:
                    # An earlier path already went to this address, key and all: moving to
                    # another address now would send the key a second place.
                    raise _Failure("connection dropped") from None
                raise
        return responses
