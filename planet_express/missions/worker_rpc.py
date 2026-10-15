"""The worker socket: six methods, every call authenticated by peer identity AND session token.

A second `RpcServer` with its own handler table whose keys are fixed here. It is never given the
operator handler table (`build_worker_server` refuses any key outside `WORKER_METHODS`), and the
caller is authenticated per request, before the method is looked at:

    SO_PEERCRED pid ──► /proc/<pid>/cgroup ──► must equal the session's cgroup_path
    (uid kind: peer uid == session_uid)     ──► AND sha256(session_token) names a live session

Every failure gets the same answer ("unauthorized"/"Refused"), so a refused peer learns nothing.
Handlers receive `(params, session)`, so each call is scoped to the one mission it belongs to.
"""

import hashlib
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from planet_express.integrations.rpc import PeerCred, RpcError, RpcServer
from planet_express.missions.runtime import AgentResult
from planet_express.missions.sessions import resolve_session

WORKER_METHODS = (
    "mission.get_context",
    "mission.post_event",
    "mission.heartbeat",
    "mission.submit_result",
    "memory.search",
    "tool.invoke",
)
CGROUP_MOUNT = "/sys/fs/cgroup"
MAX_EVENT_DATA_BYTES = 16 * 1024
MAX_TOOL_PARAMS_BYTES = 16 * 1024
_TOKEN_LEN = (16, 256)
_BAD_PATH = re.compile(r"[\x00-\x1f\x7f]|\(deleted\)")


def parse_proc_cgroup(text: str, mount: str = CGROUP_MOUNT) -> str | None:
    """Absolute cgroup2 path from the text of /proc/<pid>/cgroup, or None if anything is odd."""
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        rel = line[3:]
        if not rel.startswith("/") or _BAD_PATH.search(rel) or ".." in rel.split("/"):
            return None
        return mount if rel == "/" else mount + rel.rstrip("/")
    return None


def _proc_cgroup(pid: int, mount: str = CGROUP_MOUNT) -> str | None:
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="utf-8", errors="replace") as handle:
            return parse_proc_cgroup(handle.read(8192), mount)
    except (OSError, ValueError):
        return None


class WorkerAuthenticator:
    """Callable for `RpcServer(authenticator=...)`: returns the session row or None.

    `store` is Core's `Store`; the session lookup is S3a's `resolve_session(store, token_hash, peer_cgroup=,
    peer_uid=)`. `resolve` replaces that lookup (tests only)."""

    def __init__(self, store, *, resolve: Callable[..., Any] | None = None,
                 read_cgroup: Callable[[int], str | None] | None = None, mount: str = CGROUP_MOUNT,
                 core_uid: int | None = None):
        self._core_uid = core_uid          # None: this process's own uid at call time (a session is never Core)
        if store is None and resolve is None:
            raise ValueError("a store (or a resolver) is required")
        self._resolve = resolve or (lambda token_hash, **kw: resolve_session(store, token_hash, **kw))
        self._read_cgroup = read_cgroup or (lambda pid: _proc_cgroup(pid, mount))

    def _core(self) -> int:
        return os.geteuid() if self._core_uid is None else self._core_uid

    def __call__(self, peer: PeerCred, request: dict):
        token = request.get("session_token")
        if not isinstance(token, str) or not _TOKEN_LEN[0] <= len(token) <= _TOKEN_LEN[1]:
            return None
        cgroup = self._read_cgroup(peer.pid)
        row = self._resolve(hashlib.sha256(token.encode()).hexdigest(),
                            peer_cgroup=cgroup, peer_uid=peer.uid)
        if row is None or getattr(row, "revoked_at", None) is not None:
            return None
        # Re-check identity here as well: S3b does not rely on the resolver alone for the
        # decision that keeps a same-uid process outside the session out.
        kind = getattr(row, "kind", None)
        if kind == "local_cgroup":
            # cgroup AND uid AND token (contract 22.1): a session without a unique uid is never accepted
            path, uid = getattr(row, "cgroup_path", None), getattr(row, "session_uid", None)
            ok = (cgroup is not None and isinstance(path, str) and path == cgroup
                  and isinstance(uid, int) and not isinstance(uid, bool) and uid == peer.uid
                  and uid not in (0, self._core()))
            return row if ok else None
        if kind == "local_uid":
            uid = getattr(row, "session_uid", None)
            ok = isinstance(uid, int) and uid == peer.uid and uid not in (0, self._core())
            return row if ok else None
        return None  # "fake" sessions have no real identity to bind to


class WorkerBackend(Protocol):
    """What the Supervisor (S3a) provides; every method is already scoped to `session`."""

    def get_context(self, session) -> dict: ...
    def post_event(self, session, kind: str, data: dict) -> dict: ...
    def heartbeat(self, session) -> dict: ...
    def submit_result(self, session, result: AgentResult) -> dict: ...
    def memory_search(self, session, query: str, limit: int) -> dict: ...
    def tool_invoke(self, session, name: str, params: dict) -> dict: ...


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PostEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    data: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded(self):
        import json
        if len(json.dumps(self.data, allow_nan=False, default=str)) > MAX_EVENT_DATA_BYTES:
            raise ValueError("event data too large")
        return self


class _Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: dict


class _Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=5, ge=1, le=20)


class _Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded(self):
        import json
        if len(json.dumps(self.params, allow_nan=False, default=str)) > MAX_TOOL_PARAMS_BYTES:
            raise ValueError("tool params too large")
        return self


def _parse(model, params):
    try:
        return model.model_validate(params)
    except (ValidationError, ValueError, TypeError):
        raise RpcError("Invalid params", "bad_params") from None


def build_worker_handlers(backend: WorkerBackend) -> dict[str, Callable[[dict, Any], Any]]:
    def get_context(params, session):
        _parse(_Empty, params)
        return backend.get_context(session)

    def post_event(params, session):
        p = _parse(_PostEvent, params)
        return backend.post_event(session, p.kind, p.data)

    def heartbeat(params, session):
        _parse(_Empty, params)
        return backend.heartbeat(session)

    def submit_result(params, session):
        p = _parse(_Submit, params)
        result = _parse(AgentResult, p.result)
        return backend.submit_result(session, result)

    def memory_search(params, session):
        p = _parse(_Search, params)
        return backend.memory_search(session, p.query, p.limit)

    def tool_invoke(params, session):
        p = _parse(_Tool, params)
        return backend.tool_invoke(session, p.name, p.params)

    handlers = {
        "mission.get_context": get_context,
        "mission.post_event": post_event,
        "mission.heartbeat": heartbeat,
        "mission.submit_result": submit_result,
        "memory.search": memory_search,
        "tool.invoke": tool_invoke,
    }
    assert set(handlers) == set(WORKER_METHODS)
    return handlers


def build_worker_server(socket_path, backend: WorkerBackend, authenticator: WorkerAuthenticator, *,
                        group, handlers: dict | None = None, **kwargs) -> RpcServer:
    """The worker-socket server. `handlers` exists only so a caller's mistake is a loud error."""
    table = build_worker_handlers(backend)
    if handlers is not None:
        extra = set(handlers) - set(WORKER_METHODS)
        if extra:
            raise ValueError(f"not worker methods: {sorted(extra)}")
        table.update(handlers)
    return RpcServer(
        socket_path, table, allowed_uids=None, group=group, authenticator=authenticator,
        reserved_methods=frozenset(),
        # The socket directory belongs to setup; never re-group a shared directory (it also holds
        # the operator socket) just because this server's group is the worker's.
        chown_parent=False, **kwargs,
    )
