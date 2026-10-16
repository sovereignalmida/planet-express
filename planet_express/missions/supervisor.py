"""The mission Supervisor (contract §11) for Observe/Propose missions.

It owns spawning, the brief, budgets, heartbeats, and the order of operations at the end of a session:

    revoke the token -> kill -> wait until `containment.is_empty()` -> ONLY THEN ask for a transition.

`is_empty()` is the sole "provably dead" predicate. Nothing here requests a terminal (or any other)
transition on a runtime's say-so, a timeout or a status report. Observe/Propose sessions can never enter
`executing`; a mutating tier is refused at `start()`.

The Supervisor is poll-driven and holds no thread: `poll(mission_id)` advances one session by one step, so
it is deterministic under test. Per-run state lives in memory, but everything that decides a transition is
re-derived from the store (mission row, session rows, events) and from containment, so `recover()` can
rebuild after a crash.
"""

import contextlib
import functools
import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from planet_express.core.store import Store
from planet_express.integrations.rpc import RpcError
from planet_express.memory import store as memory_store
from planet_express.memory.models import Card
from planet_express.memory.retrieval import render_card, retrieve

from . import artifacts, leases
from .brief import DEFAULT_MAX_BYTES, EvidenceItem, build_brief
from .containment import ContainedSession, ContainmentError, SessionContainment
from .events import insert_event
from .launcher.spec import LaunchSpec
from .models import Mission
from .runtime import (
    RUNTIME_EVENT_KINDS,
    AgentResult,
    AgentRuntime,
    AgentUpdate,
    RunContext,
)
from .sessions import SessionRow, issue_session, set_leader_pid
from .state import TERMINAL, IllegalTransition, MissionNotFound, transition
from .store import MissionStore

log = logging.getLogger(__name__)

ACTOR = "supervisor"
SESSION_TTL_PAD_S = 300
LEASE_TTL_S = 60
PREFLIGHT_RETRY_S = 300       # while the preflight fails, look again this often (an operator may have repaired the host)
START_ATTEMPTS = 3            # consecutive failures to even start a queued mission before the operator is told
MAX_MEMORY_HITS = 20


class MissionsDisabled(RuntimeError):
    """`missions.enabled` is false: nothing may be started."""


class UnsupportedTier(RuntimeError):
    """Only Observe/Propose missions run under this supervisor."""


class RepairRefused(UnsupportedTier):
    """A mutating-tier start refused because a side door around the worker boundary is open (brief 6.8), or because
    nobody could say it is closed. A subclass of UnsupportedTier: mutating tiers were already refused here."""


class ProposalOnly(RuntimeError):
    """The launcher preflight failed on this host: no session may start (there is no weaker containment)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExternalEvent(_Strict):
    kind: Literal["note", "tool", "diff"]
    body: dict[Annotated[StrictStr, Field(max_length=512)], object] = {}


class ExternalArtifact(_Strict):
    name: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    text: Annotated[StrictStr, Field(max_length=262144)]


class ExternalRecord(_Strict):
    """A session record this system did not launch: evidence only (contract §11.1)."""
    events: Annotated[list[ExternalEvent], Field(max_length=1000)] = []
    artifacts: Annotated[list[ExternalArtifact], Field(max_length=32)] = []


@dataclass
class _Run:
    mission_id: str
    cs: ContainedSession | None
    runtime: AgentRuntime | None
    handle: Any
    session_id: str | None
    started_at: float
    tool_calls: int = 0
    output_bytes: int = 0
    phase: Literal["running", "killing"] = "running"
    reason: str | None = None
    result: AgentResult | None = None
    waiting_logged: bool = False
    workspace: Path | None = None             # this session's own directory, removed at reap
    stop_socket: Callable[[], None] | None = None
    brief: str = ""
    submitted: AgentResult | None = None      # a result the worker handed in over the socket


def default_memory_cards(store: Store, mission: Mission) -> list[Card]:
    grant = mission.grant()
    tags = [f"stack:{s}" for s in grant.scope.stacks] + [f"service:{s}" for s in grant.scope.services]
    try:
        with store._connect() as conn:
            return list(retrieve(conn, tags, mission.goal).cards)
    except Exception:
        return []


def _per_mission(fn):
    """Serialise on the mission named by the first argument. Missions never wait for each other, so a slow
    kill (up to the containment's kill timeout) on one mission cannot stall another's polling or worker calls."""
    @functools.wraps(fn)
    def wrapper(self, mission_id, *args, **kwargs):
        with self._lock_for(mission_id):
            return fn(self, mission_id, *args, **kwargs)
    return wrapper


WORKER_LOCK_WAIT_S = 2.0


def _per_session(fn):
    """Worker-socket entry points. A worker whose mission is mid-kill, or whose mission lock is busy for longer
    than a moment, is refused at once instead of tying up one of the socket's few handler threads."""
    @functools.wraps(fn)
    def wrapper(self, session, *args, **kwargs):
        run = self._runs.get(session.mission_id)
        if run is None or run.phase != "running":
            raise RpcError("Refused", "unauthorized")
        lock = self._lock_for(session.mission_id)
        if not lock.acquire(timeout=WORKER_LOCK_WAIT_S):
            raise RpcError("Busy", "busy")
        try:
            return fn(self, session, *args, **kwargs)
        finally:
            lock.release()
    return wrapper


class SocketProvider:
    """Per-session worker sockets (contract 22.8.1). `path(session_id)` is where the session's socket will be, in a
    directory named for the session under the socket root; `start(cs)` creates that directory, binds the socket and
    returns a stop callable, all BEFORE the launch (the launcher then chowns them to the session)."""

    def path(self, session_id: str) -> str:
        raise NotImplementedError

    def start(self, cs: ContainedSession) -> Callable[[], None]:
        raise NotImplementedError


class Supervisor:
    def __init__(self, store: Store, containment: SessionContainment,
                 runtime_factory: Callable[[Mission], AgentRuntime],
                 clock: Callable[[], float] = time.time, *, enabled: bool | Callable[[], bool] = False,
                 artifact_dir: Path | None = None, workspace_dir: Path | None = None,
                 memory_provider: Callable[[Mission], Sequence[Card]] | None = None,
                 evidence_provider: Callable[[Mission], Sequence[EvidenceItem]] | None = None,
                 worker_env: dict[str, str] | None = None, max_brief_bytes: int = DEFAULT_MAX_BYTES,
                 lease_ttl_s: int = LEASE_TTL_S, socket_provider: "SocketProvider | None" = None,
                 preflight: Callable[[], Any] | None = None, tool_broker: Any = None,
                 repair_gate: Callable[[], Any] | None = None):
        self.store = store
        self.repair_gate = repair_gate                # side-door report (brief 6.8); None means a repair start is refused
        self.tool_broker = tool_broker                # `missions.tools.ToolBroker`; None: every tool call is refused
        if tool_broker is not None and getattr(tool_broker, "on_refusal_cap", None) is None:
            tool_broker.on_refusal_cap = self.freeze_for_tool_abuse
        self.socket_provider = socket_provider
        self.preflight_fn = preflight                 # returns a PreflightReport; None means "trusted" (tests)
        self.preflight_report: Any = None
        self._preflight_at = 0.0
        self.ms = MissionStore(store)
        self.containment = containment
        self.runtime_factory = runtime_factory
        self.clock = clock
        self._enabled = enabled if callable(enabled) else (lambda: bool(enabled))
        self._start_failures: dict[str, int] = {}
        self.artifact_dir = Path(artifact_dir) if artifact_dir else store.path.parent / "mission-artifacts"
        self.workspace_dir = Path(workspace_dir) if workspace_dir else store.path.parent / "mission-workspaces"
        self.memory_provider = memory_provider or (lambda m: default_memory_cards(store, m))
        self.evidence_provider = evidence_provider or (lambda m: [])
        self.worker_env = dict(worker_env or {})
        self.max_brief_bytes = max_brief_bytes
        self.lease_ttl_s = lease_ttl_s
        self._runs: dict[str, _Run] = {}
        self._locks: dict[str, threading.RLock] = {}      # one per mission: its start/poll/cancel never interleave
        self._locks_guard = threading.Lock()

    def _lock_for(self, mission_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(mission_id, threading.RLock())

    # ── guards ──────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """`missions.enabled`, read at use time so a config reload is honoured."""
        return bool(self._enabled())

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise MissionsDisabled("missions are disabled (missions.enabled is false)")

    def launcher_ready(self) -> bool:
        """The launcher preflight gate (contract 22.4). True when no preflight is configured, or the last one passed.
        While it fails the host is proposal-only: it is re-run at most every PREFLIGHT_RETRY_S."""
        if self.preflight_fn is None:
            return True
        now = float(self.clock())
        report = self.preflight_report
        if report is None or (not report.ok and now - self._preflight_at >= PREFLIGHT_RETRY_S):
            try:
                report = self.preflight_fn()
            except Exception as exc:       # a preflight that cannot run is a failed preflight
                from .launcher.preflight import failed_report
                report = failed_report("unknown", "preflight could not run", type(exc).__name__)
            self.preflight_report, self._preflight_at = report, now
            if report.ok:
                log.info("%s", report.render())
            else:
                log.warning("Mission sessions are disabled on this host (proposal-only):\n%s", report.render())
        return bool(report.ok)

    def _note(self, mission_id: str, body: dict[str, Any], kind: str = "note") -> None:
        self.ms.append_event(mission_id, kind, ACTOR, body)

    def _attempts(self, mission_id: str) -> int:
        """Sessions ever started for the mission, counted from session rows (events can carry worker text)."""
        with self.store._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM mission_sessions WHERE mission_id=?", (mission_id,)).fetchone()[0]

    # ── start ───────────────────────────────────────────────────────────────
    @_per_mission
    def start(self, mission_id: str) -> None:
        self._require_enabled()
        if not self.launcher_ready():
            raise ProposalOnly(self.preflight_report.reason or "the launcher preflight failed")
        mission = self.ms.get_mission(mission_id)
        if mission.tier not in ("observe", "propose"):
            self.require_repair_preconditions()
            raise UnsupportedTier(f"{mission.tier} missions are not run by this supervisor")
        if mission_id in self._runs:
            raise ValueError(f"{mission_id} already has a running session")
        if mission.status != "queued":
            raise IllegalTransition(f"{mission_id} is {mission.status}, not queued")
        if mission.cancel_requested_at is not None:
            raise IllegalTransition(f"{mission_id} has a cancel requested")
        self._launch(mission)

    def require_repair_preconditions(self) -> None:
        """Brief 6.8: a Repair-tier (or higher) worker starts only if every side door is closed. Asked afresh every time,
        because a door can open between missions. A gate that is missing, crashes or returns anything but a passing
        report refuses; Observe and Propose never come here."""
        if self.repair_gate is None:
            raise RepairRefused("the side-door preflight is not configured, so no mutating mission may start")
        try:
            report = self.repair_gate()
            ok = bool(report.ok)
            reason = None if ok else (report.reason or "a side door is open")
        except Exception as exc:                   # noqa: BLE001 - a gate that cannot run is a closed gate
            raise RepairRefused(f"the side-door preflight could not run ({type(exc).__name__})") from None
        if not ok:
            raise RepairRefused(reason)

    def _launch(self, mission: Mission) -> None:
        if not self.launcher_ready():          # also on a restart after a crash: proposal-only means no session at all
            raise ProposalOnly(self.preflight_report.reason or "the launcher preflight failed")
        grant = mission.grant()
        session_id = f"S-{secrets.token_hex(8)}"
        # `start_session` is the sole uid allocator: it commits the row (state `launching`, uid held) before any
        # launcher runs, so nothing is left behind if it refuses.
        cs, token = issue_session(
            self.ms, mission.id, session_id, self.containment.cgroup_path(session_id),
            session_ttl_s=grant.budgets.wall_s + SESSION_TTL_PAD_S, lease_ttl_s=self.lease_ttl_s,
            uid_range=self.containment.uid_range, actor=ACTOR, expected_grant_hash=mission.grant_hash)
        run = _Run(mission.id, cs, None, None, cs.session_id, float(self.clock()))
        self._runs[mission.id] = run
        if self.ms.get_mission(mission.id).cancel_requested_at is not None:
            return self._kill_and_advance(run, "cancel")     # a cancel raced the launch: nothing is launched
        # The launcher hands the token to the child on fd 3 (the helper's token pipe); it never touches argv, env,
        # events or disk. `read_fd` is Core's own copy for in-process runtimes (tests), never inherited.
        read_fd = write_fd = None
        try:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, token.encode())
            os.close(write_fd)
            write_fd = None
            self._start_runtime(mission, run, read_fd, token)
        except Exception as exc:
            self._note(mission.id, {"start_failed": type(exc).__name__})
            self._kill_and_advance(run, "start_failed")
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)

    def _start_runtime(self, mission: Mission, run: _Run, token_fd: int, token: str) -> None:
        assert run.cs is not None
        cs = run.cs
        grant = mission.grant()
        brief = build_brief(mission, list(self.evidence_provider(mission)), list(self.memory_provider(mission)),
                            self._constraints(mission), max_bytes=self.max_brief_bytes)
        artifacts.store_text(self.store, self.artifact_dir, mission.id, "brief.md", brief.text, ACTOR)
        run.brief = brief.text
        # One workspace per session: a finished session's directory belongs to its (released) uid, so a retry
        # never reuses it. The launcher hands this directory to the session uid and keeps Core's group on it.
        workspace = self.workspace_dir / cs.session_id
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        run.workspace = workspace

        def spawn(argv: Sequence[str], env: dict[str, str] | None = None, cwd: str | None = None,
                  extra_fds: tuple[int, ...] = ()) -> int:
            if extra_fds:
                raise ContainmentError("the launcher's fd map is fixed (0, 1, 2, 3): no extra fds")
            full_env = {**self.worker_env, **(env or {}), "PE_MISSION_ID": mission.id,
                        "PE_SESSION_TOKEN_FD": "3"}          # fd 3 is always the token pipe in the child
            if self.socket_provider is not None:
                full_env["PE_WORKER_SOCKET"] = self.socket_provider.path(cs.session_id)
                # Bound BEFORE the launch (so the agent's first connect cannot find nothing); the launcher then hands
                # the directory and sockets to the session. Registered first so a failed launch stops it again.
                run.stop_socket = self.socket_provider.start(cs)
            spec = LaunchSpec(uid=cs.session_uid, cwd=cwd or str(workspace), argv=tuple(argv), env=full_env,
                              token=token, sock_dir=self.socket_provider is not None)
            launched = self.containment.launch(cs.session_id, spec)
            if launched != cs:
                raise ContainmentError("the launcher started a different session than the one requested")
            pids = self.containment.pids(cs)
            leader = pids[0] if pids else None
            if not self.ms.mark_session_live(cs.session_id, leader):
                raise ContainmentError("the session was revoked while it launched")
            return leader or 0

        context = RunContext(brief=brief.text, session=cs, token_fd=token_fd, env=dict(self.worker_env),
                             cwd=str(workspace), spawn=spawn)
        runtime = self.runtime_factory(mission)
        run.runtime = runtime
        run.handle = runtime.start(mission, context, grant)
        pid = getattr(run.handle, "pid", None)
        if isinstance(pid, int) and pid:
            set_leader_pid(self.store, cs.session_id, pid)

    @staticmethod
    def _constraints(mission: Mission) -> list[str]:
        grant = mission.grant()
        budgets = (f"Budgets: {grant.budgets.wall_s}s wall time, {grant.budgets.tool_calls} tool calls, "
                   f"{grant.budgets.output_bytes} output bytes.")
        out = [f"This mission is {mission.tier}: observe and propose only; make no changes to the host.",
               "Stay inside the scope above; never touch a protected resource.", budgets]
        out += [f"Excluded: {x}" for x in grant.exclusions]
        return out

    # ── driving a session ───────────────────────────────────────────────────
    def poll_all(self) -> dict[str, str]:
        return {mid: self.poll(mid) for mid in list(self._runs)}

    @_per_mission
    def poll(self, mission_id: str) -> str:
        """Advance one step. Returns the mission's status afterwards."""
        run = self._runs.get(mission_id)
        if run is not None:
            if run.phase == "killing":
                self._advance(run)
            else:
                self._step(run)
        return self.ms.get_mission(mission_id).status

    def _step(self, run: _Run) -> None:
        if run.runtime is None or run.cs is None:
            return self._kill_and_advance(run, "crash")      # a run that never started cannot be trusted
        mission = self.ms.get_mission(run.mission_id)
        if mission.status != "investigating":
            return self._kill_and_advance(run, "state_changed")
        if mission.cancel_requested_at is not None:
            return self._kill_and_advance(run, "cancel")
        try:
            raw = run.runtime.status(run.handle)
            update = AgentUpdate.model_validate(raw.model_dump() if isinstance(raw, AgentUpdate) else raw)
        except Exception as exc:
            self._note(run.mission_id, {"runtime_status_error": type(exc).__name__})
            # An adapter whose process vanished may raise instead of reporting: that is a crash, not a bug.
            return self._kill_and_advance(run, "crash" if self.containment.is_empty(run.cs) else "runtime_error")
        self._record(run, update)
        grant = mission.grant()
        elapsed = float(self.clock()) - run.started_at
        for name, over in (("wall", elapsed > grant.budgets.wall_s),
                           ("tool_calls", run.tool_calls > grant.budgets.tool_calls),
                           ("output_bytes", run.output_bytes > grant.budgets.output_bytes)):
            if over:
                return self._kill_and_advance(run, f"budget:{name}")
        try:
            leases.heartbeat(self.store, run.mission_id, self.lease_ttl_s)
        except leases.LeaseLost:
            return self._kill_and_advance(run, "lease_lost")
        if update.state == "finished" or run.submitted is not None:
            return self._collect(run)
        if update.state == "failed" or self.containment.is_empty(run.cs):
            return self._kill_and_advance(run, "crash")

    def _record(self, run: _Run, update: AgentUpdate) -> None:
        for event in update.events:
            if event.kind == "tool":
                run.tool_calls += 1
            try:
                size = len(json.dumps(event.body, default=str))
                self.ms.append_event(run.mission_id, event.kind, "worker", dict(event.body))
            except (TypeError, ValueError):
                size = 0
                self._note(run.mission_id, {"dropped_event": event.kind})
            run.output_bytes += size
        run.output_bytes += update.output_bytes

    def _collect(self, run: _Run) -> None:
        assert run.runtime is not None
        try:
            raw = run.submitted if run.submitted is not None else run.runtime.collect(run.handle)
            result = AgentResult.model_validate(raw.model_dump() if isinstance(raw, AgentResult) else raw)
        except Exception as exc:
            self._note(run.mission_id, {"result_invalid": type(exc).__name__})
            return self._kill_and_advance(run, "result_invalid")
        run.output_bytes += len(json.dumps(result.model_dump(mode="json")))
        if run.output_bytes > self.ms.get_mission(run.mission_id).grant().budgets.output_bytes:
            return self._kill_and_advance(run, "budget:output_bytes")      # the final result counts too
        try:
            artifacts.store_json(self.store, self.artifact_dir, run.mission_id, "result.json",
                                 result.model_dump(mode="json"), ACTOR)
        except Exception as exc:
            self._note(run.mission_id, {"result_store_failed": type(exc).__name__})
            return self._kill_and_advance(run, "result_invalid")
        run.result = result
        self._persist_candidates(run.mission_id, result)
        self._kill_and_advance(run, "result")        # the session is over: make sure nothing outlives it

    def _persist_candidates(self, mission_id: str, result: AgentResult) -> None:
        """`memory_candidates` become CANDIDATE cards (source `inferred`, actor `runtime:<mission>`): they stay
        quarantined until an operator confirms them. A bad or secret-shaped one is dropped and recorded."""
        if not result.memory_candidates:
            return
        mission = self.ms.get_mission(mission_id)
        grant = mission.grant()
        tags = [f"stack:{x}" for x in grant.scope.stacks] + [f"service:{x}" for x in grant.scope.services]
        stored = dropped = 0
        for index, text in enumerate(result.memory_candidates):
            try:
                card = _candidate_card(mission_id, index, text, tags, datetime.fromtimestamp(float(self.clock()), UTC))
                with self.store._write() as conn:
                    memory_store.upsert_candidate(conn, card, f"runtime:{mission_id}",
                                                  now=datetime.fromtimestamp(float(self.clock()), UTC))
                stored += 1
            except Exception as exc:
                dropped += 1
                self._note(mission_id, {"candidate_dropped": index, "why": type(exc).__name__})
        self._note(mission_id, {"memory_candidates": {"stored": stored, "dropped": dropped}})

    # ── ending a session ────────────────────────────────────────────────────
    def cancel(self, mission_id: str, by: str) -> None:
        if not (by.startswith("operator:") or by in ("system", "core")):
            raise ValueError("cancel must name an operator or a core actor")
        now = int(self.store._clock())
        with self.store._write() as conn:
            row = conn.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["status"] in TERMINAL:
                return
            conn.execute("UPDATE missions SET cancel_requested_at=?, cancel_requested_by=?, updated_at=? "
                         "WHERE id=?", (now, by, now, mission_id))
            insert_event(conn, mission_id, "decision", by, {"cancel_requested": True}, now)
        # The request above is recorded without the mission lock (so it is never delayed by a slow kill); the
        # kill itself runs under it.
        with self._lock_for(mission_id):
            run = self._runs.get(mission_id)
            if run is not None:
                self._kill_and_advance(run, "cancel")
            elif not self._adopt(self.ms.get_mission(mission_id), "cancel"):
                self._settle_without_session(mission_id, "cancel")

    def _kill_and_advance(self, run: _Run, reason: str) -> None:
        self._begin_kill(run, reason)
        self._advance(run)

    def _begin_kill(self, run: _Run, reason: str) -> None:
        """Revoke the token first (the worker's endpoints stop answering), then kill. No transition yet."""
        if run.phase == "killing":
            return
        run.phase, run.reason = "killing", reason
        self._note(run.mission_id, {"kill": reason})
        self._revoke(run)
        leases.mark_suspect(self.store, run.mission_id)
        if run.runtime is not None and run.handle is not None:
            with contextlib.suppress(Exception):      # advisory only: containment is what kills
                run.runtime.cancel(run.handle)
        if run.cs is not None:
            self.containment.kill(run.cs)

    def _revoke(self, run: _Run) -> None:
        live = self.ms.live_session(run.mission_id)
        if live is not None and live["id"] == run.session_id:      # never revoke someone else's replacement
            self.ms.revoke_session(live["id"], ACTOR)

    def _advance(self, run: _Run) -> None:
        """Wait for `is_empty()`. Until it is True, no transition is requested, whatever else happened."""
        if run.cs is not None and not self.containment.is_empty(run.cs):
            self.containment.kill(run.cs)             # kill is idempotent; retry while members remain
            if not run.waiting_logged:
                run.waiting_logged = True
                self._note(run.mission_id, {"waiting_for_empty": run.reason})
            return
        self._revoke(run)
        if run.cs is not None:
            self.containment.cleanup(run.cs)
        self._runs.pop(run.mission_id, None)
        self._reap_session(run)
        self._settle(run)

    def _reap_session(self, run: _Run) -> None:
        """The session is provably dead and its cgroup is gone: stop its socket, remove its workspace and release its
        uid. A uid is released only when no process runs as it; otherwise it stays held and `sweep_uids` retries."""
        if run.stop_socket is not None:
            with contextlib.suppress(Exception):
                run.stop_socket()
            run.stop_socket = None
        if run.workspace is not None:
            shutil.rmtree(run.workspace, ignore_errors=True)
            if run.workspace.exists():
                # A leftover owned by the old uid would be inherited by the next session given this uid.
                log.warning("workspace %s was not removed: uid %s stays held", run.workspace, run.cs and run.cs.session_uid)
                return
        self._release_uid(run.cs)

    def _release_uid(self, cs: ContainedSession | None) -> None:
        if cs is None or cs.session_uid is None:
            return
        try:
            if self.containment.uid_in_use(cs.session_uid):
                self._note_held(cs)
                return
        except Exception:
            log.exception("could not check uid %s", cs.session_uid)
            return
        self.ms.release_session_uid(cs.session_id)

    def _note_held(self, cs: ContainedSession) -> None:
        log.warning("uid %s of session %s still has processes: it stays held", cs.session_uid, cs.session_id)

    def sweep_uids(self) -> int:
        """Retry releasing the uid of every revoked session (a crash between kill and release, or a straggler that
        was still running). Kills what is left in the session's cgroup first. Returns how many uids were freed."""
        freed = 0
        active = {r.session_id for r in self._runs.values()}
        for row in self.ms.unreleased_revoked_sessions():
            if row["id"] in active:
                continue
            cs = ContainedSession(row["id"], "local_cgroup", row["cgroup_path"], row["session_uid"])
            try:
                if not self.containment.is_empty(cs):
                    self.containment.kill(cs)
                if not self.containment.is_empty(cs):
                    continue
                self.containment.cleanup(cs)
                if self.containment.uid_in_use(cs.session_uid):
                    continue
                workspace = self.workspace_dir / row["id"]
                shutil.rmtree(workspace, ignore_errors=True)
                if workspace.exists():
                    continue
                freed += int(self.ms.release_session_uid(cs.session_id))
            except Exception as exc:
                log.warning("could not reap session %s: %s", row["id"], type(exc).__name__)
        return freed

    def _settle(self, run: _Run) -> None:
        reason = run.reason or "crash"
        mission = self.ms.get_mission(run.mission_id)
        if mission.status in TERMINAL:
            return
        live = self.ms.live_session(mission.id)
        if live is not None and live["id"] != run.session_id:
            # A replacement session is live and not ours: terminalizing would release it unmanaged.
            self._note(mission.id, {"stale_run_left_alone": run.session_id})
            if mission.cancel_requested_at is not None or reason == "cancel":
                self._adopt(mission, "cancel")           # a cancel must still reach the replacement
            return
        if mission.status != "investigating" and reason != "cancel":
            # Someone else already moved the mission on. The session is dead and cleaned up; leave the state.
            self._note(mission.id, {"session_ended": reason, "left_in": mission.status})
            return
        if reason in ("crash", "recover") and mission.cancel_requested_at is None:
            if self._attempts(mission.id) < 1 + mission.grant().budgets.retries:
                self._note(mission.id, {"restart_after": reason})
                try:
                    return self._launch(mission)
                except Exception as exc:
                    self._note(mission.id, {"restart_failed": type(exc).__name__})
                    if mission.id in self._runs:
                        # The new session was registered and its kill could not be completed: it may still be
                        # running. It stays tracked in `killing`, the poll loop retries until `is_empty()`, and
                        # nothing below may request a transition meanwhile.
                        return
            else:
                self._note(mission.id, {"retries_exhausted": reason})
        self._settle_without_session(mission.id, reason, run.result)

    def _settle_without_session(self, mission_id: str, reason: str, result: AgentResult | None = None) -> None:
        mission = self.ms.get_mission(mission_id)
        if reason == "cancel" or mission.cancel_requested_at is not None:
            target = "cancelled"
        elif reason == "result" and result is not None:
            # An agent's say-so never completes a mission (invariant I1): the operator, or a later slice's
            # Core-run verification, decides. Observe and propose both land in awaiting_decision.
            target = "awaiting_decision"
        else:
            target = "needs_operator"
        try:
            transition(self.store, mission_id, target, ACTOR)
        except IllegalTransition as exc:
            self._note(mission_id, {"transition_refused": target, "why": str(exc)[:200]})

    # ── the daemon's loop and shutdown ──────────────────────────────────────
    def tick(self) -> None:
        """One turn of the daemon's poll thread: start queued Observe/Propose missions, then advance every
        running session. One misbehaving mission never stops the others."""
        if not self.enabled:
            return
        ready = self.launcher_ready()            # proposal-only: queued missions wait, nothing is started or retried
        for mission in self.ms.list_missions("queued"):
            if mission.tier not in ("observe", "propose") or mission.id in self._runs:
                continue
            if mission.cancel_requested_at is not None:
                with contextlib.suppress(Exception):
                    self._settle_without_session(mission.id, "cancel")
                continue
            if not ready:
                continue
            try:
                self.start(mission.id)
                self._start_failures.pop(mission.id, None)
            except Exception as exc:
                self._start_failed(mission.id, exc)
        with contextlib.suppress(Exception):
            self.sweep_uids()
        try:
            self.poll_all()
        except Exception:
            log.exception("mission poll failed")
            for mission_id in list(self._runs):
                try:
                    self.poll(mission_id)
                except Exception:
                    log.exception("mission %s poll failed", mission_id)

    def _start_failed(self, mission_id: str, exc: Exception) -> None:
        count = self._start_failures.get(mission_id, 0) + 1
        self._start_failures[mission_id] = count
        log.warning("mission %s did not start (%s), attempt %d", mission_id, type(exc).__name__, count)
        self._note(mission_id, {"start_failed": type(exc).__name__, "attempt": count})
        if count >= START_ATTEMPTS and mission_id not in self._runs:
            self._start_failures.pop(mission_id, None)
            self._settle_without_session(mission_id, "start_failed")

    def shutdown(self) -> None:
        """Daemon stop: revoke every token and kill every session. Missions are NOT moved on: they stay
        `investigating`, and `recover()` at the next start restarts them within the retry budget. A session
        that cannot be proven empty stays tracked (and its cgroup stays), never silently dropped."""
        for run in list(self._runs.values()):
            try:
                with self._lock_for(run.mission_id):
                    self._begin_kill(run, "shutdown")
                    if run.cs is None or self.containment.is_empty(run.cs):
                        self._revoke(run)
                        if run.cs is not None:
                            self.containment.cleanup(run.cs)
                        self._runs.pop(run.mission_id, None)
                        self._reap_session(run)
            except Exception:
                log.exception("could not stop mission %s cleanly", run.mission_id)

    # ── restart recovery ────────────────────────────────────────────────────
    def recover(self) -> list[str]:
        """Reconcile after a Supervisor restart. For each in-flight Observe/Propose mission: kill whatever
        its last session left in containment, wait for empty, then restart within the retry budget or hand
        it to the operator. Missions this supervisor does not run (mutating tiers) are left alone."""
        self._require_enabled()
        acted: list[str] = []
        for mission in self.ms.list_missions():
            if mission.id in self._runs or mission.status in TERMINAL or mission.status in ("draft", "queued"):
                continue
            if mission.tier not in ("observe", "propose"):
                continue
            reason = "cancel" if mission.cancel_requested_at is not None else "recover"
            try:
                with self._lock_for(mission.id):
                    if self._adopt(mission, reason, only_if_live=mission.status != "investigating"):
                        acted.append(mission.id)
            except Exception as exc:  # one mission whose session cannot be proven dead must not block the others
                # `_adopt` registered the run before killing, so it stays tracked in `killing`: the poll loop keeps
                # retrying, and the mission is not moved on until containment says the session is empty.
                log.warning("recovering mission %s failed: %s", mission.id, type(exc).__name__)
                self._note(mission.id, {"recover_failed": type(exc).__name__})
        with contextlib.suppress(Exception):
            self.sweep_uids()                 # a crash between launch and release leaves held uids behind
        return acted

    def _adopt(self, mission: Mission, reason: str, *, only_if_live: bool = False) -> bool:
        """Take over a session this supervisor instance did not start: kill whatever its last session left in
        containment and wait for empty. Returns False when there is no session to deal with."""
        cs = self._last_session(mission.id)
        if cs is None:
            if mission.status != "investigating" or only_if_live:
                return False
        elif only_if_live and self.ms.live_session(mission.id) is None and self.containment.is_empty(cs):
            self.containment.cleanup(cs)          # nothing to supervise; just don't leak the empty cgroup
            return False
        run = _Run(mission.id, cs, None, None, cs.session_id if cs else None, float(self.clock()))
        run.workspace = self.workspace_dir / cs.session_id if cs else None
        self._runs[mission.id] = run
        self._kill_and_advance(run, reason)
        return True

    def _last_session(self, mission_id: str) -> ContainedSession | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_sessions WHERE mission_id=? "
                               "ORDER BY issued_at DESC, rowid DESC LIMIT 1", (mission_id,)).fetchone()
        if row is None:
            return None
        return ContainedSession(row["id"], "local_cgroup", row["cgroup_path"], row["session_uid"])

    # ── WorkerBackend: what the worker socket's handlers call ───────────────
    # Each method is scoped to the one run the (already authenticated) session belongs to, and re-checks
    # that it is still THE live run: a revoked, replaced or finished session gets the uniform refusal.
    def _live_run(self, session: SessionRow) -> _Run:
        run = self._runs.get(session.mission_id)
        live = self.ms.live_session(session.mission_id) if run is not None else None
        if (run is None or run.phase != "running" or run.session_id != session.id
                or live is None or live["id"] != session.id):
            raise RpcError("Refused", "unauthorized")
        return run

    @_per_session
    def get_context(self, session: SessionRow) -> dict:
        run = self._live_run(session)
        mission = self.ms.get_mission(run.mission_id)
        return {"mission_id": mission.id, "title": mission.title, "goal": mission.goal,
                "success_criteria": mission.success_criteria, "tier": mission.tier,
                "scope": mission.grant().scope.model_dump(mode="json"), "brief": run.brief}

    @_per_session
    def post_event(self, session: SessionRow, kind: str, data: dict) -> dict:
        run = self._live_run(session)
        if kind not in RUNTIME_EVENT_KINDS:
            raise RpcError("Invalid params", "bad_params")      # state/decision/artifact events belong to Core
        try:
            size = len(json.dumps(data, default=str))
            self.ms.append_event(run.mission_id, kind, "worker", dict(data))
        except (TypeError, ValueError):
            raise RpcError("Invalid params", "bad_params") from None
        if kind == "tool":
            run.tool_calls += 1
        run.output_bytes += size
        return {"ok": True}

    @_per_session
    def heartbeat(self, session: SessionRow) -> dict:
        run = self._live_run(session)
        try:
            leases.heartbeat(self.store, run.mission_id, self.lease_ttl_s)
        except leases.LeaseLost:
            raise RpcError("Refused", "unauthorized") from None
        return {"ok": True}

    @_per_session
    def submit_result(self, session: SessionRow, result: AgentResult) -> dict:
        run = self._live_run(session)
        if run.submitted is not None:
            raise RpcError("A result was already submitted", "conflict")
        run.submitted = result          # the next poll validates, stores and ends the session
        self._note(run.mission_id, {"result_submitted": True})
        return {"accepted": True}

    @_per_session
    def _search_scope(self, session: SessionRow) -> Mission:
        return self.ms.get_mission(self._live_run(session).mission_id)

    def memory_search(self, session: SessionRow, query: str, limit: int) -> dict:
        mission = self._search_scope(session)
        grant = mission.grant()
        tags = [f"stack:{x}" for x in grant.scope.stacks] + [f"service:{x}" for x in grant.scope.services]
        with self.store._connect() as conn:
            found = retrieve(conn, tags, query, limit=min(limit, MAX_MEMORY_HITS))       # confirmed cards only
        self._search_scope(session)       # revalidate: a session revoked or killed meanwhile gets nothing back
        return {"cards": [{"id": c.id, "type": c.type, "status": c.status, "title": c.title, "text": render_card(c)}
                          for c in found.cards],
                "truncated": found.truncated}

    # ── external sessions (contract §11.1) ──────────────────────────────────
    def freeze_for_tool_abuse(self, session: Any) -> None:
        """A session that keeps asking for tools it may not use: freeze its cgroup and close its endpoints. Frozen, it
        cannot act; its wall-time budget (or the operator) ends it."""
        run = self._runs.get(session.mission_id)
        if run is None or run.cs is None or run.session_id != session.id:
            return
        self.containment.freeze(run.cs)
        self._note(session.mission_id, {"tool_abuse": "frozen", "session": session.id})
        revoke = getattr(self.tool_broker, "revoke", None)
        if revoke is not None:
            revoke(session.mission_id)

    @_per_session
    def tool_invoke(self, session: SessionRow, name: str, params: dict) -> dict:
        """A brokered tool call (contract 8): the live run only, and only if a broker is configured."""
        self._live_run(session)
        if self.tool_broker is None:
            raise RpcError("Refused", "unauthorized")
        return self.tool_broker.invoke(session, name, params)

    def import_external_session(self, mission_id: str, record: dict[str, Any]) -> None:
        """Store an outside session's record as events and artifacts. Evidence only: this method has no
        path to a transition, and it cannot write state or decision events."""
        parsed = ExternalRecord.model_validate(record)
        self.ms.get_mission(mission_id)
        for event in parsed.events:
            self.ms.append_event(mission_id, event.kind, "import:external", dict(event.body))
        for item in parsed.artifacts:
            artifacts.store_text(self.store, self.artifact_dir, mission_id, item.name, item.text, "import:external")
        self.ms.append_event(mission_id, "note", ACTOR, {"external_import": {
            "events": len(parsed.events), "artifacts": len(parsed.artifacts), "evidence_only": True}})


def _candidate_card(mission_id: str, index: int, text: str, tags: list[str], now: datetime) -> Card:
    digest = hashlib.sha256(f"{mission_id}\0{index}\0{text}".encode()).hexdigest()[:16]
    title = (text.strip().splitlines() or [""])[0][:200] or "candidate"
    scope = [t for t in tags if len(t) <= 160][:16]
    try:
        return Card(id=f"rt-{digest}", title=title, type="fact", scope=scope, statement=text,
                    source="inferred", created_at=now, evidence=[f"mission:{mission_id}"])
    except ValueError:
        # An odd scope value must not cost the candidate its text; the operator widens scope when confirming.
        return Card(id=f"rt-{digest}", title=title, type="fact", scope=[], statement=text,
                    source="inferred", created_at=now, evidence=[f"mission:{mission_id}"])


__all__ = ["ExternalRecord", "MissionsDisabled", "ProposalOnly", "RepairRefused", "SocketProvider", "Supervisor",
           "UnsupportedTier", "default_memory_cards"]
