"""The model broker: a per-session loopback proxy that holds the model API key so the session never does.

A mission session must hold no secret (`runtime.env_allow` refuses credential-looking names and the environment builder
strips them), yet an LLM agent cannot work without a model API key. This module closes that gap. Core owns the real key; each
session gets its own loopback listener, a random per-session access code (worth nothing anywhere else) and a base URL. The
agent talks to the listener as if it were the vendor's API; the broker checks the access code, the path, the model and the
limits, swaps in the real key and relays the request.

What it enforces, per session:
  * only `POST /v1/messages` and `POST /v1/messages/count_tokens`; nothing else is relayed;
  * the access code (constant-time compare); a revoked session, or a mission that is not running, is refused;
  * the model must be on the operator's list; the request body is size-capped and must be a JSON object;
  * a request count and a token budget (counted from the vendor's own `usage` fields, streamed or not); past either, every
    further request is refused with a non-retryable error;
  * a response size cap; only a fixed set of headers goes each way; the client's own auth headers never go upstream.
What it never does: log or store a request or response body, write the key anywhere, or relay to any host but the configured
upstream. Each request leaves one `model.request` event (path, model, status, token counts, duration) on the mission.

THE KEY IS NOT PROTECTED FROM THE BROKER'S OWN PROCESS: it lives in Core's memory, as every Core secret does. What the broker
removes is the key from the SESSION: its environment, its files and its process list.
"""

import hmac
import http.client
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from planet_express.missions.events import redact_value
from planet_express.missions.store import MissionStore

log = logging.getLogger(__name__)
ACTOR = "model-broker"
LOOPBACK = "127.0.0.1"
ALLOWED_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent")
FORWARD_RESPONSE_HEADERS = ("content-type", "request-id", "retry-after", "x-should-retry")
FORWARD_RESPONSE_PREFIXES = ("anthropic-ratelimit-",)          # the CLI's own back-off reads these
RUNNING = frozenset({"executing", "investigating"})
# Everything the CLI would otherwise send straight to the vendor, around the broker (per its documented env settings). The
# one it cannot switch off by environment is the WebFetch domain preflight: the grant should not give a session WebFetch.
HARDENING_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                 "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1", "DISABLE_GROWTHBOOK": "1",
                 "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
                 "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
_CHUNK = 8192
_MAX_LINE = 1 << 20


class BrokerUnavailable(RuntimeError):
    """The broker cannot serve this session (no key, no such session). The session must not start without it."""


@dataclass(frozen=True)
class BrokerLimits:
    max_requests: int = 300
    max_tokens: int = 10_000_000
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 32 * 1024 * 1024
    timeout_s: float = 300.0
    max_connections: int = 8            # at once, per session: a session cannot hold more of Core's threads than this
    client_timeout_s: float = 30.0      # to send its headers and its body: a stalled client is cut off, never waited for
    max_refusals_logged: int = 20       # refusals are audited one by one up to this many; the rest are only counted
    recheck_s: float = 1.0              # how often a stream already running is checked for a pause, a revoke or an expiry


class _BoundedServer(ThreadingHTTPServer):
    """A threaded server that never runs more than `max_connections` handlers at once (the rest are closed at once) and can
    cut every active connection when its session ends."""
    daemon_threads = True

    def __init__(self, address, handler, *, max_connections: int):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active: set[socket.socket] = set()
        self._active_lock = threading.Lock()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request) -> None:
        with self._active_lock:
            self._active.discard(request)
        self._slots.release()

    def handle_error(self, request, client_address) -> None:
        log.debug("a model broker connection ended with an error", exc_info=True)      # a cut-off client is routine, not a traceback

    def wait_idle(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._active_lock:
                if not self._active:
                    return
            time.sleep(0.02)

    def close_active(self) -> None:
        with self._active_lock:
            live = list(self._active)
        for conn in live:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Session:
    def __init__(self, session_id: str, mission_id: str):
        self.session_id, self.mission_id = session_id, mission_id
        self.access = "sk-ant-pe-" + secrets.token_urlsafe(32)      # shaped like a key so a client does not reject it
        self.lock = threading.Lock()
        self.requests = 0
        self.tokens = 0
        self.reserved = 0                # tokens promised to requests still in flight
        self.refused = 0
        self.refused_logged = 0
        self.closed = False              # set when the session ends: nothing may connect or send after it
        self.cap: int | None = None             # a lower token ceiling than the broker's own for THIS session (a conversation's remainder)
        self.upstreams: set[http.client.HTTPConnection] = set()      # the vendor connections of requests still in flight
        self.server: _BoundedServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0


class ModelBroker:
    def __init__(self, *, upstream_host: str, key_env: str, models: list[str], limits: BrokerLimits, store: MissionStore,
                 environ: Mapping[str, str] | None = None,
                 connect: Callable[[str, float], http.client.HTTPConnection] | None = None,
                 path_prefix: str = "", auth_scheme: str = "x-api-key", session_env: Mapping[str, str] | None = None,
                 allow_body_keys: Sequence[str] = ()):
        if auth_scheme not in ("x-api-key", "bearer"):
            raise ValueError(f"unknown auth_scheme {auth_scheme!r}")
        self.extra_body_keys = frozenset(allow_body_keys)
        self.upstream_host, self.key_env, self.models = upstream_host, key_env, frozenset(models)
        self.path_prefix, self.auth_scheme = path_prefix, auth_scheme       # where the vendor's API is mounted; how the key is sent
        self.session_env = dict(session_env or {})
        self.limits, self.ms = limits, store
        self.environ = environ
        self._connect = connect or (lambda host, timeout: http.client.HTTPSConnection(host, timeout=timeout))
        self._sessions: dict[str, _Session] = {}
        self._paused: set[str] = set()                  # mission ids whose model access is closed until an operator resumes
        self._lock = threading.Lock()

    # ── the SocketProvider-like surface the Supervisor already drives ───────────────────────────────
    def _key(self) -> str:
        import os
        return (self.environ if self.environ is not None else os.environ).get(self.key_env, "")

    def secret_values(self) -> list[str]:
        """The real key, for the environment builder's by-value scan: if it ever shows up in a session, that is a leak."""
        key = self._key()
        return [key] if key else []

    def start(self, cs) -> Callable[[], None]:
        if not self._key():
            raise BrokerUnavailable(f"the model API key ({self.key_env}) is not set in Core's environment")
        row = self.ms.get_session(cs.session_id)
        if row is None:
            raise BrokerUnavailable("no such session")
        session = _Session(cs.session_id, row["mission_id"])
        handler = self._handler(session)
        server = _BoundedServer((LOOPBACK, 0), handler, max_connections=self.limits.max_connections)
        session.server, session.port = server, server.server_address[1]
        session.thread = threading.Thread(target=server.serve_forever, name=f"model-broker-{cs.session_id}", daemon=True,
                                          kwargs={"poll_interval": 0.2})
        session.thread.start()
        with self._lock:
            self._sessions[cs.session_id] = session
        return lambda: self._stop(cs.session_id)

    def env(self, cs) -> dict[str, str]:
        with self._lock:
            session = self._sessions.get(cs.session_id)
        if session is None:
            return {}
        return {**self.session_env, "ANTHROPIC_BASE_URL": f"http://{LOOPBACK}:{session.port}", "ANTHROPIC_API_KEY": session.access,
                **HARDENING_ENV}

    def shutdown(self) -> None:
        for session_id in list(self._sessions):
            self._stop(session_id)

    @staticmethod
    def _cut_upstreams(session: _Session) -> None:
        with session.lock:
            upstreams = list(session.upstreams)
        for conn in upstreams:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def pause(self, mission_id: str) -> None:
        """The tripwire (or the tool-abuse guard) closes the session's endpoints: no request starts, a request already with the
        vendor is cut at once, and a stream still reaching the agent stops, until `resume`."""
        with self._lock:
            self._paused.add(mission_id)
            held = [s for s in self._sessions.values() if s.mission_id == mission_id]
        for session in held:
            self._cut_upstreams(session)

    def resume(self, mission_id: str, grant_hash: str) -> None:
        with self._lock:
            self._paused.discard(mission_id)

    def set_session_cap(self, session_id: str, tokens: int) -> bool:
        """Lower this session's token ceiling to `tokens` (never raise it above the broker's own, and never raise one already lowered).
        A Console turn is given what is left of its conversation's budget, so the budget is enforced inside the turn and not only
        between turns. False if the session is not known."""
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        with session.lock:
            session.cap = max(int(tokens), 0) if session.cap is None else min(session.cap, max(int(tokens), 0))
        return True

    def usage(self, session_id: str) -> dict[str, int] | None:
        with self._lock:
            session = self._sessions.get(session_id)
        return None if session is None else {"requests": session.requests, "tokens": session.tokens, "refused": session.refused}

    def _stop(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None and not any(s.mission_id == session.mission_id for s in self._sessions.values()):
                self._paused.discard(session.mission_id)
        if session is None or session.server is None:
            return
        with session.lock:
            session.closed = True
        try:
            session.server.shutdown()
            session.server.close_active()                 # a relay still running when the session ends is cut, not finished
            self._cut_upstreams(session)                  # ... and so is its request to the vendor: no more tokens are spent
            session.server.wait_idle(2.0)
            session.server.server_close()
        finally:
            if session.thread is not None:
                session.thread.join(timeout=5)
            self._record(session, "model.summary", {"requests": session.requests, "tokens": session.tokens,
                                                    "refused": session.refused})

    def _record(self, session: _Session, event: str, body: dict[str, Any]) -> None:
        try:
            self.ms.append_event(session.mission_id, "tool", ACTOR, redact_value({**body, "event": event}))
        except Exception:                                        # noqa: BLE001 -- the record is best effort here
            log.warning("could not record %s for %s", event, session.mission_id)

    def _permitted(self, session: _Session) -> str | None:
        """Why this session may not use the model right now, or None."""
        try:
            row = self.ms.get_session(session.session_id)
            if row is None or row.get("revoked_at") is not None:
                return "the session has ended"
            if int(row["expires_at"]) <= self.ms._now():
                return "the session has expired"
            status = self.ms.get_mission(session.mission_id).status
        except Exception:                                        # noqa: BLE001 -- unreadable state is a refusal
            return "the mission state cannot be read"
        with self._lock:
            if session.mission_id in self._paused:
                return "the mission is paused"
        return None if status in RUNNING else f"the mission is {status}"

    # ── the HTTP side ───────────────────────────────────────────────────────────────────────────────
    def _handler(self, session: _Session):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "pe-model-broker"
            sys_version = ""
            timeout = broker.limits.client_timeout_s          # applies to reading the headers and the body

            def log_message(self, *args):                        # never log a request line: no bodies, no headers
                pass

            def _error(self, status: int, kind: str, message: str) -> None:
                body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def _refuse(self, status: int, kind: str, message: str) -> None:
                with session.lock:
                    session.refused += 1
                    audit = session.refused_logged < broker.limits.max_refusals_logged
                    session.refused_logged += 1 if audit else 0
                if audit:                                          # no headers, no body: the status, the reason, the bare path
                    broker._record(session, "model.refused", {"status": status, "reason": message[:80],
                                                              "path": self.path.split("?", 1)[0][:80]})
                self._error(status, kind, message)

            def do_GET(self):
                self._refuse(404, "not_found_error", "not served here")
            do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path not in ALLOWED_PATHS:
                    return self._refuse(404, "not_found_error", "not served here")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._refuse(411, "invalid_request_error", "Content-Length is required")
                if length < 0 or length > broker.limits.max_request_bytes:
                    return self._refuse(413, "request_too_large", "the request is larger than the broker allows")
                # The body is read BEFORE any refusal is sent: answering with unread data in the socket makes the kernel reset the
                # connection, and a client that sees a reset retries, where the refusal it should have read says "do not".
                body = self.rfile.read(length)
                presented = self.headers.get("x-api-key") or (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if not hmac.compare_digest(presented.encode(), session.access.encode()):
                    return self._refuse(401, "authentication_error", "invalid credentials")
                key = broker._key()
                if not key:
                    return self._refuse(503, "api_error", "the broker has no upstream credential")
                try:
                    parsed = json.loads(body)
                except ValueError:
                    return self._refuse(400, "invalid_request_error", "the body is not JSON")
                model = parsed.get("model") if isinstance(parsed, dict) else None
                if not isinstance(model, str) or model not in broker.models:
                    return self._refuse(400, "invalid_request_error", "that model is not allowed for missions")
                forbidden = _vendor_side_reach(parsed, broker.extra_body_keys)
                if forbidden:
                    return self._refuse(400, "invalid_request_error", f"{forbidden} is not allowed for missions")
                why = broker._permitted(session)           # the last look before this request counts against the budget
                if why:
                    return self._refuse(403, "permission_error", f"model access is closed: {why}")
                # What this request may cost, promised before it is relayed: the output cap it declares plus a generous guess at
                # its input. Without a reservation, parallel requests could each pass the check while none had finished.
                declared = parsed.get("max_tokens")
                promise = (declared if isinstance(declared, int) and declared > 0 else 0) + len(body)   # a token is never under a byte
                with session.lock:
                    ceiling = broker.limits.max_tokens if session.cap is None else min(broker.limits.max_tokens, session.cap)
                    over = (session.requests >= broker.limits.max_requests
                            or session.tokens + session.reserved + promise > ceiling)
                    if not over:
                        session.requests += 1
                        session.reserved += promise
                if over:
                    return self._refuse(403, "permission_error", "the mission's model budget is used up")
                state = {"released": False, "contacted": False}
                try:
                    self._relay(path, body, model, key, promise, state)
                finally:
                    with session.lock:
                        if not state["released"]:
                            session.reserved -= promise
                            if state["contacted"]:
                                session.tokens += promise      # the vendor was reached and no usage came back: assume the worst

            def _relay(self, path: str, body: bytes, model: str, key: str, promise: int, state: dict) -> None:
                started = time.monotonic()
                headers = {name: value for name in FORWARD_REQUEST_HEADERS if (value := self.headers.get(name))}
                if broker.auth_scheme == "bearer":
                    headers["authorization"] = f"Bearer {key}"
                else:
                    headers["x-api-key"] = key
                headers["content-length"] = str(len(body))
                query = "?beta=true" if "beta=true" in self.path.partition("?")[2].split("&") else ""
                counter = _UsageCounter()
                status, sent, committed = 502, 0, False
                try:
                    conn = broker._connect(broker.upstream_host, broker.limits.timeout_s)
                    try:
                        conn.connect()                         # the connection is made BEFORE it is registered: a stop in the
                        with session.lock:                     # middle of DNS or TLS cannot be interrupted, so it is caught here
                            if session.closed:
                                raise OSError("the session ended")
                            session.upstreams.add(conn)
                        again = broker._permitted(session)     # a pause during DNS or TLS had no connection to cut
                        if again:
                            raise OSError(f"model access closed: {again}")
                        conn.request("POST", broker.path_prefix + path + query, body=body, headers=headers)
                        state["contacted"] = True
                        response = conn.getresponse()
                        status = response.status
                        self.send_response(status)
                        for name, value in response.getheaders():
                            lowered = name.lower()
                            if (value and key not in value
                                    and (lowered in FORWARD_RESPONSE_HEADERS or lowered.startswith(FORWARD_RESPONSE_PREFIXES))):
                                self.send_header(name, value)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        committed = True
                        checked = time.monotonic()
                        key_bytes, tail = key.encode(), b""
                        while chunk := response.read1(_CHUNK):          # read1: hand on what has arrived, never wait to fill a buffer
                            sent += len(chunk)
                            if sent > broker.limits.max_response_bytes:
                                break                                    # cut it off: the client sees a truncated stream
                            if key_bytes in tail + chunk:                # an upstream that echoes the key: the stream ends here
                                broker._record(session, "model.refused", {"status": 502, "reason": "the upstream echoed the key",
                                                                          "path": path})
                                break
                            tail = (tail + chunk)[-(len(key_bytes) - 1):] if len(key_bytes) > 1 else b""
                            if time.monotonic() - checked >= broker.limits.recheck_s:        # a pause or a revoke ends a stream already running
                                checked = time.monotonic()
                                if broker._permitted(session):
                                    break
                            counter.feed(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    finally:
                        with session.lock:
                            session.upstreams.discard(conn)
                        conn.close()
                except (OSError, http.client.HTTPException, ValueError, OverflowError) as exc:
                    if not committed:                                    # headers already out: the stream just ends, no second status line
                        status = 504 if isinstance(exc, TimeoutError) else 502
                        self._error(status, "api_error", "the model service did not answer")
                counter.finish()
                charge = counter.total
                completed = counter.complete()                  # the vendor's own end marker arrived, not just a closed socket
                if status == 200 and not completed:
                    charge = max(charge, promise)           # cut short or cut off: the vendor may still have billed all of it
                with session.lock:
                    session.tokens += charge
                    session.reserved -= promise                 # in one step: no request ever sees both counted
                    state["released"] = True
                broker._record(session, "model.request", {
                    "path": path, "model": model, "status": status, "input_tokens": counter.input, "output_tokens": counter.output,
                    "charged": charge, "bytes": sent, "ms": int((time.monotonic() - started) * 1000)})
                self.close_connection = True

        return Handler


# The top-level fields of a Messages request that only shape the model's answer. Anything else is refused: a gateway in front of the
# vendor has fields of its own (OpenRouter's `plugins`, `models`, `provider`, `route`, ...) that make IT search the web or fall back
# to a model off the operator's list, with Core's key. `missions.model_broker.allow_body_keys` adds a field an operator has vetted.
ALLOWED_BODY_KEYS = frozenset({"model", "messages", "max_tokens", "system", "stream", "temperature", "top_p", "top_k", "stop_sequences",
                               "tools", "tool_choice", "thinking", "metadata", "service_tier", "output_config", "context_management"})


def _vendor_side_reach(body: dict, extra_keys: frozenset[str] = frozenset()) -> str | None:
    """What in a request would make the VENDOR (or a gateway) reach out for the agent (fetch a URL, call an MCP server, run code,
    pick another model), around the session's own network containment, using Core's key. The agent's own tools are plain `custom`
    tools: they run in the session."""
    unknown = sorted(set(body) - ALLOWED_BODY_KEYS - extra_keys)
    if unknown:
        return f"the field {unknown[0][:40]!r}"
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") not in (None, "custom"):
                return f"the {str(tool.get('type'))[:40]} tool"
    return None


class _UsageCounter:
    """Token usage from the vendor's own response: the `usage` object of a JSON reply, or the `message_start` and
    `message_delta` events of a stream. It reads a copy of what is relayed and never changes it."""

    def __init__(self):
        self.input = self.output = 0
        self._buffer = b""
        self._whole = bytearray()
        self._json = True
        self._skipping = False
        self.stopped = False             # a stream's `message_stop` was seen
        self.parsed = False              # a non-stream reply was read whole

    @property
    def total(self) -> int:
        return self.input + self.output

    def feed(self, chunk: bytes) -> None:
        if self._json and len(self._whole) < _MAX_LINE * 8:
            self._whole += chunk
        data = self._buffer + chunk
        self._buffer = b""
        while True:
            line, newline, rest = data.partition(b"\n")
            if not newline:
                break
            self._line(line)
            data = rest
        if len(data) > _MAX_LINE:
            data = b""                                           # a single enormous line is not a usage event
        self._buffer = data

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        self._json = False                                       # it is a stream
        try:
            event = json.loads(line[5:])
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_stop":
            self.stopped = True
        usage = (event.get("message") or {}).get("usage") if event.get("type") == "message_start" else event.get("usage")
        self._take(usage, stream=True)

    def _take(self, usage: Any, *, stream: bool) -> None:
        if not isinstance(usage, dict):
            return
        # Cache reads cost a tenth of a fresh input token, so they count a tenth against the budget (an agent that re-sends a large
        # cached context every turn would otherwise spend the budget on a price it does not pay).
        counted_in = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens")
                         if isinstance(usage.get(k), int))
        if isinstance(usage.get("cache_read_input_tokens"), int):
            counted_in += int(usage["cache_read_input_tokens"]) // 10
        counted_out = int(usage["output_tokens"]) if isinstance(usage.get("output_tokens"), int) else 0
        if stream:
            self.input = max(self.input, counted_in)
            self.output = max(self.output, counted_out)          # message_delta usage is cumulative
        else:
            self.input, self.output = counted_in, counted_out

    def complete(self) -> bool:
        return self.stopped or self.parsed

    def finish(self) -> None:
        if self._json and self._whole:
            try:
                reply = json.loads(bytes(self._whole))
            except ValueError:
                return
            if isinstance(reply, dict):
                self.parsed = True
                self._take(reply.get("usage"), stream=False)
                if "input_tokens" in reply and "usage" not in reply:        # count_tokens answers {"input_tokens": n}
                    self.input = int(reply["input_tokens"]) if isinstance(reply["input_tokens"], int) else 0


__all__ = ["ACTOR", "ALLOWED_PATHS", "BrokerLimits", "BrokerUnavailable", "HARDENING_ENV", "ModelBroker"]
