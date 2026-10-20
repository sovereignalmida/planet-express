"""The proxy's Unix-socket server (contract 6.1). One request per connection. For each: read and validate the head, authenticate
(peer identity AND the session token), refuse everything while paused, match the route, validate query and body, resolve the
objects it names, and for a write check the session's authority NOW, record the event, and only then forward a request rebuilt
from the validated parts. Streams and hijacked connections are bounded in bytes and time and stop the moment the session is
revoked or paused. Any parse doubt, any daemon surprise, any exception: the client gets a refusal and nothing is forwarded."""

import json
import logging
import os
import select
import socket
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from planet_express.integrations.rpc import PeerCred

from .httpwire import (
    BadRequest,
    Head,
    Refusal,
    content_length,
    read_exact_body,
    read_head,
    refuse,
    respond,
    strict_json,
)
from .policy import Plan, Policy, Route, redact_inspect
from .upstream import Upstream, UpstreamError, request_target, status_of

log = logging.getLogger(__name__)
PASS_HEADERS = ("api-version", "docker-experimental", "ostype", "builder-version", "x-docker-container-path-stat")
SESSION_HEADER = "x-pe-session"


@dataclass(frozen=True)
class Limits:
    head_s: float = 10.0
    body_s: float = 60.0
    json_response: int = 8 * 1024 * 1024
    stream_bytes: int = 32 * 1024 * 1024
    stream_s: float = 300.0
    hijack_bytes: int = 64 * 1024 * 1024
    hijack_s: float = 600.0
    connections: int = 8
    refusal_events: int = 100
    execs: int = 512


DEFAULT_LIMITS = Limits()


class Gate(Protocol):
    """What the server asks of whoever owns the session (the service's store-backed gate, or a fake in tests)."""

    def authenticate(self, peer: PeerCred, token: str | None) -> Any | None: ...
    def check_live(self, session) -> None: ...        # Refusal 503 while paused
    def check_write(self, session) -> None: ...       # Refusal unless a write is allowed right now
    def alive(self, session) -> bool: ...             # streams and hijacks poll this
    def record(self, session, body: dict) -> None: ...  # raises if the event could not be written


class ExecTable:
    """The exec ids this session's proxy created, and the container each belongs to. Bounded; oldest forgotten first."""

    def __init__(self, cap: int = 512):
        self._cap, self._items, self._lock = cap, OrderedDict(), threading.Lock()

    def add(self, exec_id: str, container_id: str) -> None:
        with self._lock:
            self._items[exec_id] = container_id
            while len(self._items) > self._cap:
                self._items.popitem(last=False)

    def owner(self, exec_id: str) -> str | None:
        with self._lock:
            return self._items.get(exec_id)


def _peer(conn: socket.socket) -> PeerCred:
    pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return PeerCred(pid, uid, gid)


class ProxyServer:
    """`gate` None is the read-only variant: no session, the peer's uid must be in `allowed_uids`, nothing is ever recorded."""

    def __init__(self, socket_path: str, *, policy: Policy, upstream: Upstream, gate: Gate | None = None,
                 execs: ExecTable | None = None, allowed_uids: set[int] | None = None, limits: Limits = DEFAULT_LIMITS,
                 name: str = "docker-proxy"):
        if gate is None and not allowed_uids:
            raise ValueError("a proxy with no session gate needs allowed_uids")
        self.socket_path, self.policy, self.upstream, self.gate = socket_path, policy, upstream, gate
        self.execs, self.allowed_uids, self.limits, self.name = execs or ExecTable(limits.execs), allowed_uids, limits, name
        self._slots = threading.BoundedSemaphore(limits.connections)
        self._pool = ThreadPoolExecutor(max_workers=limits.connections, thread_name_prefix=name)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._identity: tuple[int, int] | None = None
        self._refusals = 0
        self._lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._sock is not None or self._stop.is_set():
            raise RuntimeError("proxy already started or stopped")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.set_inheritable(False)
            old = os.umask(0o177)                   # 0600, owned by Core: the launcher (root) hands it to the session
            try:
                sock.bind(self.socket_path)
            finally:
                os.umask(old)
            info = os.lstat(self.socket_path)
            self._identity = (info.st_dev, info.st_ino)
            sock.listen(16)
            sock.settimeout(0.1)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._accept, daemon=True, name=f"{self.name}-accept")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            info = os.lstat(self.socket_path)
            if (info.st_dev, info.st_ino) == self._identity:
                os.unlink(self.socket_path)
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                refuse(conn, 503, "busy")
                conn.close()
                continue
            try:
                self._pool.submit(self._serve, conn)
            except RuntimeError:
                self._slots.release()
                conn.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            self._handle(conn)
        except Exception:
            log.exception("docker proxy: unexpected error")
            refuse(conn, 500, "internal error")
        finally:
            try:
                conn.close()
            finally:
                self._slots.release()

    # ── one request ───────────────────────────────────────────────────────────────────────────

    def _authenticate(self, conn: socket.socket, head: Head) -> Any:
        try:
            peer = _peer(conn)
        except OSError:
            raise Refusal("unauthorized", 401) from None
        if self.gate is None:
            if peer.uid not in (self.allowed_uids or ()):
                raise Refusal("unauthorized", 401)
            return None
        session = self.gate.authenticate(peer, head.headers.get(SESSION_HEADER))
        if session is None:
            raise Refusal("unauthorized", 401)
        return session

    def _handle(self, conn: socket.socket) -> None:
        session = head = route = None
        try:
            head = read_head(conn, timeout=self.limits.head_s)
            session = self._authenticate(conn, head)
            if self.gate is not None:
                self.gate.check_live(session)                     # paused: every request is a 503
            route, groups = self.policy.match(head)
            query = self.policy.clean_query(route, head.query)
            upgrade = self.policy.check_upgrade(route, head)
            body, length = self._read_body(conn, head, route)
            plan = self.policy.plan(route, head.method, groups, query, body, length)
            if route.write:
                self.gate.check_write(session)
                self._record(session, plan, head)
            self._forward(conn, head, plan, upgrade, session)
        except Refusal as refusal:
            self._note_refusal(session, head, route, refusal)
            refuse(conn, refusal.status, refusal.reason)
        except UpstreamError:
            refuse(conn, 502, "the Docker daemon could not be asked")

    def _read_body(self, conn: socket.socket, head: Head, route: Route) -> tuple[dict | None, int]:
        if route.body == "json":
            length = content_length(head, cap=self.policy.json_cap, allowed=True)
            ctype = head.headers.get("content-type", "application/json").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise BadRequest("the body must be application/json")
            return strict_json(read_exact_body(conn, head, length, timeout=self.limits.body_s), cap=self.policy.json_cap), length
        if route.body == "raw":
            length = content_length(head, cap=self.policy.archive_cap, allowed=True)
            if len(head.leftover) > length:
                raise BadRequest("bytes after the request body")
            return None, length
        content_length(head, cap=0, allowed=False)
        if head.leftover:
            raise BadRequest("bytes after the request")
        return None, 0

    def _record(self, session, plan: Plan, head: Head) -> None:
        body = {"event": "docker.write", "route": plan.route.name, "method": plan.method, "targets": plan.targets,
                **plan.summary}
        try:
            self.gate.record(session, body)
        except Exception:                                        # noqa: BLE001 - no record, no forward
            raise Refusal("the request could not be recorded", 500) from None

    def _note_refusal(self, session, head, route, refusal: Refusal) -> None:
        if session is None or self.gate is None or refusal.status in (401, 503):
            return
        with self._lock:
            self._refusals += 1
            count = self._refusals
        if count > self.limits.refusal_events + 1:
            return
        body = ({"event": "docker.refused", "reason": refusal.reason, "route": route.name if route else None,
                 "method": head.method if head else None, "path": head.path[:200] if head else None}
                if count <= self.limits.refusal_events else {"event": "docker.refused", "further_refusals_not_recorded": True})
        try:
            self.gate.record(session, body)
        except Exception:                                        # noqa: BLE001 - best effort
            log.warning("docker proxy: refusal event not recorded")

    # ── forwarding ────────────────────────────────────────────────────────────────────────────

    def _forward(self, conn: socket.socket, head: Head, plan: Plan, upgrade: bool, session) -> None:
        route = plan.route
        target = request_target(head.version, plan.upstream_path, plan.query)
        if route.hijack:
            return self._hijack(conn, target, plan, upgrade, session)
        if route.stream and plan.method == "GET":
            return self._stream(conn, target, session, self._event_line if route.name == "events" else None)
        body: Any = plan.body
        length = None
        ctype = "application/json" if plan.body is not None else None
        if route.body == "raw":
            body, length, ctype = self._client_body(conn, head, plan.raw_length), plan.raw_length, "application/x-tar"
        cconn, resp = self.upstream.open(plan.method, target, body=body, length=length, content_type=ctype)
        try:
            data = resp.read(self.limits.json_response + 1)
            if len(data) > self.limits.json_response:
                raise Refusal("the daemon's answer is too large", 502)
            status = resp.status
            ctype_up = resp.getheader("Content-Type") or "application/json"
            passed = {k.title(): resp.getheader(k) for k in PASS_HEADERS if resp.getheader(k)}
            length_up = resp.getheader("Content-Length")
        except Refusal:
            raise
        except Exception as exc:                                 # noqa: BLE001
            raise UpstreamError(type(exc).__name__) from None
        finally:
            cconn.close()
        if status in (200, 201) and route.json_response:
            data = self._postprocess(plan, data, session)
        if plan.method == "HEAD":
            respond(conn, status, b"", content_type=ctype_up, headers=passed,
                    length=int(length_up) if length_up and length_up.isdigit() else 0)
        else:
            respond(conn, status, data, content_type=ctype_up, headers=passed)
        if route.write and self.gate is not None:
            try:
                self.gate.record(session, {"event": "docker.result", "route": route.name, "status": status})
            except Exception:                                    # noqa: BLE001
                log.warning("docker proxy: result event not recorded")

    def _client_body(self, conn: socket.socket, head: Head, length: int) -> Iterator[bytes]:
        sent = len(head.leftover)
        if sent:
            yield head.leftover
        deadline = time.monotonic() + self.limits.body_s
        while sent < length:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OSError("request body timed out")
            conn.settimeout(left)
            chunk = conn.recv(min(65536, length - sent))
            if not chunk:
                raise OSError("request body ended early")
            sent += len(chunk)
            yield chunk

    def _postprocess(self, plan: Plan, data: bytes, session) -> bytes:
        """Redact inspect and list output; remember an exec this session created. Unreadable JSON on a route that must be
        redacted is an error, never passed through."""
        route = plan.route
        try:
            doc = json.loads(data.decode("utf-8")) if data else None
        except ValueError:
            raise Refusal("the daemon's answer could not be read", 502) from None
        if route.name == "exec.create":
            exec_id = doc.get("Id") if isinstance(doc, dict) else None
            if isinstance(exec_id, str) and plan.exec_container:
                self.execs.add(exec_id, plan.exec_container)
            return data
        if route.name in ("container.create", "container.update"):
            return data
        if route.name == "containers.list" and isinstance(doc, list):
            doc = [item for item in doc if not self._protected_listing(item)]
        return json.dumps(redact_inspect(doc), separators=(",", ":")).encode()

    def _protected_listing(self, item: Any) -> bool:
        if self.policy.scope is None or not isinstance(item, dict):
            return False
        from .policy import ContainerInfo
        labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
        names = item.get("Names") if isinstance(item.get("Names"), list) else []
        info = ContainerInfo(str(item.get("Id", "")), str(names[0]).lstrip("/") if names else "", {str(k): str(v) for k, v in labels.items()})
        return self.policy._verdict_container(info) == "protected"

    def _alive(self, session) -> bool:
        return self.gate is None or self.gate.alive(session)

    def _send_head(self, conn: socket.socket, status: int, ctype: str) -> None:
        conn.settimeout(10)
        if status == 101:
            head = f"HTTP/1.1 101 UPGRADED\r\nContent-Type: {ctype}\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
        else:
            head = f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n"
        conn.sendall(head.encode("latin-1"))

    def _event_line(self, line: bytes) -> bytes:
        """One line of the events stream: redacted, and dropped if it concerns a protected container. Anything that is not a
        JSON object is dropped (fail closed)."""
        from planet_express.missions.events import _is_secret_key, redact_value
        try:
            doc = json.loads(line.decode("utf-8"))
        except ValueError:
            return b""
        if not isinstance(doc, dict):
            return b""
        actor = doc.get("Actor") if isinstance(doc.get("Actor"), dict) else {}
        attrs = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
        if doc.get("Type") == "container" and self.policy.scope is not None:
            from .policy import ContainerInfo
            info = ContainerInfo(str(actor.get("ID", "")), str(attrs.get("name", "")), {str(k): str(v) for k, v in attrs.items()})
            if self.policy._verdict_container(info) == "protected":
                return b""
        if attrs:
            actor["Attributes"] = {k: ("[REDACTED]" if _is_secret_key(str(k)) else redact_value(v)) for k, v in attrs.items()}
        return json.dumps(redact_value(doc), separators=(",", ":")).encode() + b"\n"

    def _stream(self, conn: socket.socket, target: str, session, transform=None) -> None:
        sock, head_bytes, rest = self.upstream.stream(target)
        try:
            status, headers = status_of(head_bytes)
            ctype = headers.get("content-type", "application/octet-stream").replace("\r", "").replace("\n", "")[:200]
            if status != 200:
                data = rest + self._drain(sock, 65536)
                respond(conn, status, data[:65536], content_type=ctype)
                return
            self._send_head(conn, 200, ctype)
            self._pump(conn, sock, rest, self.limits.stream_bytes, self.limits.stream_s, session, one_way=True,
                       transform=transform)
        finally:
            sock.close()

    @staticmethod
    def _drain(sock: socket.socket, cap: int) -> bytes:
        out = b""
        sock.settimeout(5)
        try:
            while len(out) < cap:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                out += chunk
        except OSError:
            pass
        return out

    def _hijack(self, conn: socket.socket, target: str, plan: Plan, upgrade: bool, session) -> None:
        sock, head_bytes, rest = self.upstream.hijack(target, plan.body or b"", upgrade=upgrade)
        status = 0
        try:
            status, headers = status_of(head_bytes)
            if status not in (101, 200):
                respond(conn, status, (rest + self._drain(sock, 65536))[:65536])
                return
            ctype = "application/vnd.docker.raw-stream"
            if status == 101:
                self._send_head(conn, 101, ctype)
            else:
                self._send_head(conn, 200, headers.get("content-type", ctype)[:200])
            self._pump(conn, sock, rest, self.limits.hijack_bytes, self.limits.hijack_s, session, one_way=False)
        finally:
            sock.close()
            if self.gate is not None:
                try:
                    self.gate.record(session, {"event": "docker.result", "route": plan.route.name, "status": status})
                except Exception:                                # noqa: BLE001
                    log.warning("docker proxy: result event not recorded")

    def _pump(self, client: socket.socket, up: socket.socket, first: bytes, cap: int, seconds: float, session, *,
              one_way: bool, transform=None) -> None:
        """daemon -> client (and client -> daemon unless one_way), bounded in total bytes and time, ended as soon as the
        session is revoked or paused. A client that has sent everything is half-closed toward the daemon."""
        deadline, moved, pending = time.monotonic() + seconds, 0, b""
        sources = [up] if one_way else [up, client]
        client.settimeout(10)

        def lines(chunk: bytes) -> bytes:
            """With a transform the daemon's bytes are handled a line at a time (a partial line is held back)."""
            nonlocal pending
            pending += chunk
            if len(pending) > 65536 and b"\n" not in pending:
                raise OSError("an events line is too long")
            *done, pending = pending.split(b"\n")
            return b"".join(transform(line) for line in done if line)

        if first:
            out = lines(first) if transform else first
            if out:
                client.sendall(out)
            moved += len(first)
        while sources and moved < cap and time.monotonic() < deadline and self._alive(session):
            ready, _, _ = select.select(sources, [], [], 1.0)
            for src in ready:
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    if src is up:
                        return
                    sources.remove(src)
                    try:
                        up.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                before, moved = moved, moved + len(data)
                dest = client if src is up else up
                dest.settimeout(10)
                data = data if moved <= cap else data[: cap - before]
                try:
                    out = lines(data) if transform and src is up else data
                    if out:
                        dest.sendall(out)
                except OSError:
                    return


__all__ = ["DEFAULT_LIMITS", "SESSION_HEADER", "ExecTable", "Gate", "Limits", "ProxyServer"]
