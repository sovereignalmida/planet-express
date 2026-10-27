"""Per-session proxy lifecycle: who gets a proxy socket, what a write must prove, and pause/resume (contract 6.1).

`DockerProxyService` is what the Supervisor holds as `docker_proxy` (`.pause(mission_id)` when the tripwire closes a
session's endpoints, `.resume(mission_id, grant_hash)` when the operator resumes) and what `SessionSockets` asks to bind
`proxy.sock` next to `worker.sock`. A paused session keeps its socket and every request on it is a 503 until a resume has
re-proved the approval; a revoked session is refused at authentication."""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable

from planet_express.integrations.rpc import PeerCred
from planet_express.missions import approvals, leases
from planet_express.missions.grants import REPAIR_UP
from planet_express.missions.store import PROXY_ACTOR, MissionStore

from .httpwire import Refusal
from .policy import Policy, Scope, served_versions
from .server import DEFAULT_LIMITS, ExecTable, Gate, Limits, ProxyServer
from .upstream import DaemonInspector, Upstream

log = logging.getLogger(__name__)
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
ACTOR = PROXY_ACTOR


class ResumeRefused(RuntimeError):
    """The proxy will not reopen: the approval, the grant or the session no longer allows it."""


class StoreGate:
    """The store-backed `Gate` for one session."""

    def __init__(self, service: "DockerProxyService", session_id: str, mission_id: str, grant_hash: str, authenticator):
        self.service, self.session_id, self.mission_id, self.grant_hash = service, session_id, mission_id, grant_hash
        self.authenticator = authenticator
        self.ms = service.ms

    def authenticate(self, peer: PeerCred, token: str | None):
        """The peer's uid and cgroup must be this session's (the socket is also 0600 and owned by its unique uid). The
        session token is optional defence in depth: absent is fine, present must be this session's valid token."""
        if token is None:
            row = self.authenticator.by_id(peer, self.session_id)
        elif isinstance(token, str):
            row = self.authenticator(peer, {"session_token": token})
        else:
            return None
        return row if row is not None and getattr(row, "id", None) == self.session_id else None

    def check_live(self, session) -> None:
        if self.service.is_paused(self.mission_id):
            raise Refusal("the session is paused", 503)

    def alive(self, session) -> bool:
        if self.service.is_paused(self.mission_id):
            return False
        try:
            row = self.ms.get_session(self.session_id)
            return row is not None and row["revoked_at"] is None and row["expires_at"] > int(self.ms.store._clock())
        except Exception:                                        # noqa: BLE001 - unreadable is not alive
            return False

    def check_write(self, session) -> None:
        if self.service.is_paused(self.mission_id):
            raise Refusal("the session is paused", 503)
        try:
            row = self.ms.get_session(self.session_id)
            if row is None or row["revoked_at"] is not None:
                raise Refusal("the session is revoked")
            mission = self.ms.get_mission(self.mission_id)
            if mission.status != "executing":
                raise Refusal("the mission is not executing")
            if mission.grant_hash != self.grant_hash:
                raise Refusal("the grant changed since this session started")
            verdict = approvals.execution_predicate(self.ms.store, self.mission_id)
            if not verdict.ok:
                raise Refusal("the approval no longer covers this grant")
            for resource, fence in session.fences.items():
                leases.check_fence(self.ms.store, resource, self.mission_id, fence)
        except Refusal:
            raise
        except leases.StaleFence:
            raise Refusal("a lease fence is stale") from None
        except Exception:                                        # noqa: BLE001 - anything unreadable is a refusal
            raise Refusal("the session's authority could not be checked") from None

    def record(self, session, body: dict) -> None:
        self.ms.append_event(self.mission_id, "tool", ACTOR, {"session": self.session_id, **body})


class DockerProxyService:
    def __init__(self, store, *, authenticator, docker_socket: str = DEFAULT_DOCKER_SOCKET, sock_root: str,
                 stacks_root: str | None = None, protected: Callable[[], Iterable[str]] = lambda: (),
                 protected_gids: Iterable[int] = (), limits: Limits = DEFAULT_LIMITS, upstream: Upstream | None = None,
                 label_allow: Iterable[str] = ()):
        self.store, self.ms = store, MissionStore(store)
        self.authenticator, self.sock_root = authenticator, sock_root
        self.docker_socket, self.stacks_root, self.protected = docker_socket, stacks_root, protected
        self.protected_gids, self.limits, self.label_allow = tuple(protected_gids), limits, tuple(label_allow)
        self.upstream = upstream or Upstream(docker_socket)
        self._paused: dict[str, float] = {}         # mission id -> when it was paused (monotonic)
        self._servers: dict[str, tuple[str, ProxyServer]] = {}
        self._readers: dict[str, ProxyServer] = {}      # investigation sessions' read-only servers (nothing to pause: they change nothing)
        self._lock = threading.Lock()

    # ── pause and resume (what the Supervisor and the tripwire call) ────────────────────────────

    def pause(self, mission_id: str) -> None:
        with self._lock:
            self._paused[mission_id] = time.monotonic()

    def is_paused(self, mission_id: str) -> bool:
        with self._lock:
            return mission_id in self._paused

    def resume(self, mission_id: str, grant_hash: str) -> None:
        """Reopen after an operator resume, only if the live session, the grant and the approval all still hold."""
        try:
            mission = self.ms.get_mission(mission_id)
            session = self.ms.live_session(mission_id)
            verdict = approvals.execution_predicate(self.store, mission_id)
        except Exception:                                        # noqa: BLE001
            raise ResumeRefused("the session or approval could not be read") from None
        if session is None:
            raise ResumeRefused("no live session")
        if mission.grant_hash != grant_hash:
            raise ResumeRefused("the grant changed")
        if not verdict.ok:
            raise ResumeRefused("the approval no longer covers this grant")
        with self._lock:
            self._paused.pop(mission_id, None)

    # ── sockets ─────────────────────────────────────────────────────────────────────────────────

    def path(self, session_id: str) -> str:
        return f"{self.sock_root}/{session_id}/proxy.sock"

    def start(self, cs) -> Callable[[], None] | None:
        """Bind this session's proxy socket (the directory exists; the launcher hands the socket to the session). None if the
        session does not get one. The returned callable stops it."""
        began = time.monotonic()
        row = self.ms.get_session(cs.session_id)
        if row is None:
            raise RuntimeError("no such session")
        mission = self.ms.get_mission(row["mission_id"])
        grant = mission.grant()
        if grant.docker != "proxy":
            return None
        if mission.tier not in REPAIR_UP:
            return self._start_investigation(cs)
        scope = Scope.from_grant(grant, protected=tuple(self.protected()), stacks_root=self.stacks_root,
                                 protected_gids=self.protected_gids)
        execs = ExecTable(self.limits.execs)
        policy = Policy(scope, DaemonInspector(self.upstream), exec_owner=execs.owner,
                        versions=served_versions(self.upstream.api_version()), label_allow=self.label_allow)
        gate: Gate = StoreGate(self, cs.session_id, mission.id, mission.grant_hash, self.authenticator)
        server = ProxyServer(self.path(cs.session_id), policy=policy, upstream=self.upstream, gate=gate, execs=execs,
                             limits=self.limits, name=f"dockerproxy-{cs.session_id[:8]}")
        server.start()
        with self._lock:
            self._servers[cs.session_id] = (mission.id, server)
            # A pause left over from an earlier session is cleared (starting this one needed a live approval); a pause that
            # arrived while this one was launching (the tripwire racing the start) is NOT: it is newer than `began`.
            if self._paused.get(mission.id, began) < began:
                del self._paused[mission.id]

        def stop() -> None:
            with self._lock:
                self._servers.pop(cs.session_id, None)
            server.stop()
        return stop

    def _start_investigation(self, cs) -> Callable[[], None] | None:
        """An Observe or Propose session reads the whole Docker host (list, inspect, logs, stats, processes, files, events) and changes nothing:
        the server refuses every write by policy, so the agent never needs the real socket. Any container is readable, protected ones included:
        reading is how an investigation works, and what the session may CHANGE is governed elsewhere (tier, tripwire, hooks)."""
        if cs.session_uid is None:
            return None
        policy = Policy(None, DaemonInspector(self.upstream), read_only=True, investigate=True, versions=served_versions(self.upstream.api_version()))
        server = ProxyServer(self.path(cs.session_id), policy=policy, upstream=self.upstream, gate=None, allowed_uids={cs.session_uid},
                             limits=self.limits, name=f"dockerproxy-ro-{cs.session_id[:8]}")
        server.start()
        with self._lock:
            self._readers[cs.session_id] = server

        def stop() -> None:
            with self._lock:
                self._readers.pop(cs.session_id, None)
            server.stop()
        return stop

    def env(self, cs) -> dict[str, str]:
        with self._lock:
            bound = cs.session_id in self._servers or cs.session_id in self._readers
        path = self.path(cs.session_id)
        return {"PE_DOCKER_SOCKET": path, "DOCKER_HOST": f"unix://{path}"} if bound else {}

    def shutdown(self) -> None:
        with self._lock:
            servers = [s for _, s in self._servers.values()] + list(self._readers.values())
            self._servers.clear()
            self._readers.clear()
        for server in servers:
            server.stop()


def build_read_only(socket_path: str, *, docker_socket: str = DEFAULT_DOCKER_SOCKET, allowed_uids: set[int] | None = None,
                    protected: Iterable[str] = (), limits: Limits = DEFAULT_LIMITS,
                    upstream: Upstream | None = None) -> ProxyServer:
    """The read-only variant for discovery consumers (brief 14.1 item 9): list, inspect, events and nothing else. No
    session; the socket is 0600 and only the given uids (default: this process's own) may connect."""
    up = upstream or Upstream(docker_socket)
    scope = Scope((), (), tuple(protected)) if protected else None
    policy = Policy(scope, DaemonInspector(up), read_only=True, versions=served_versions(up.api_version()))
    return ProxyServer(socket_path, policy=policy, upstream=up, gate=None, allowed_uids=allowed_uids or {os.geteuid()},
                       limits=limits, name="dockerproxy-ro")


__all__ = ["ACTOR", "DEFAULT_DOCKER_SOCKET", "DockerProxyService", "ResumeRefused", "StoreGate", "build_read_only"]
