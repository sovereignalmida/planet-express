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
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from planet_express.core.store import Store
from planet_express.memory.models import Card
from planet_express.memory.retrieval import retrieve

from . import artifacts, leases
from .brief import DEFAULT_MAX_BYTES, EvidenceItem, build_brief
from .containment import ContainedSession, SessionContainment
from .events import insert_event
from .models import Mission
from .runtime import AgentResult, AgentRuntime, AgentUpdate, RunContext
from .sessions import issue_session, set_leader_pid
from .state import TERMINAL, IllegalTransition, MissionNotFound, transition
from .store import MissionStore

ACTOR = "supervisor"
SESSION_TTL_PAD_S = 300
LEASE_TTL_S = 60


class MissionsDisabled(RuntimeError):
    """`missions.enabled` is false: nothing may be started."""


class UnsupportedTier(RuntimeError):
    """Only Observe/Propose missions run under this supervisor."""


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


def default_memory_cards(store: Store, mission: Mission) -> list[Card]:
    grant = mission.grant()
    tags = [f"stack:{s}" for s in grant.scope.stacks] + [f"service:{s}" for s in grant.scope.services]
    try:
        with store._connect() as conn:
            return list(retrieve(conn, tags, mission.goal).cards)
    except Exception:  # noqa: BLE001 -- adapter/provider code is untrusted; it must not crash the supervisor
        return []


def _serialised(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Supervisor:
    def __init__(self, store: Store, containment: SessionContainment,
                 runtime_factory: Callable[[Mission], AgentRuntime],
                 clock: Callable[[], float] = time.time, *, enabled: bool = False,
                 artifact_dir: Path | None = None, workspace_dir: Path | None = None,
                 memory_provider: Callable[[Mission], Sequence[Card]] | None = None,
                 evidence_provider: Callable[[Mission], Sequence[EvidenceItem]] | None = None,
                 worker_env: dict[str, str] | None = None, max_brief_bytes: int = DEFAULT_MAX_BYTES,
                 lease_ttl_s: int = LEASE_TTL_S):
        self.store = store
        self.ms = MissionStore(store)
        self.containment = containment
        self.runtime_factory = runtime_factory
        self.clock = clock
        self.enabled = enabled
        self.artifact_dir = Path(artifact_dir) if artifact_dir else store.path.parent / "mission-artifacts"
        self.workspace_dir = Path(workspace_dir) if workspace_dir else store.path.parent / "mission-workspaces"
        self.memory_provider = memory_provider or (lambda m: default_memory_cards(store, m))
        self.evidence_provider = evidence_provider or (lambda m: [])
        self.worker_env = dict(worker_env or {})
        self.max_brief_bytes = max_brief_bytes
        self.lease_ttl_s = lease_ttl_s
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()        # start/poll/cancel/recover never interleave in-process

    # ── guards ──────────────────────────────────────────────────────────────
    def _require_enabled(self) -> None:
        if not self.enabled:
            raise MissionsDisabled("missions are disabled (missions.enabled is false)")

    def _note(self, mission_id: str, body: dict[str, Any], kind: str = "note") -> None:
        self.ms.append_event(mission_id, kind, ACTOR, body)

    def _attempts(self, mission_id: str) -> int:
        """Sessions ever started for the mission, counted from session rows (events can carry worker text)."""
        with self.store._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM mission_sessions WHERE mission_id=?", (mission_id,)).fetchone()[0]

    # ── start ───────────────────────────────────────────────────────────────
    @_serialised
    def start(self, mission_id: str) -> None:
        self._require_enabled()
        mission = self.ms.get_mission(mission_id)
        if mission.tier not in ("observe", "propose"):
            raise UnsupportedTier(f"{mission.tier} missions are not run by this supervisor")
        if mission_id in self._runs:
            raise ValueError(f"{mission_id} already has a running session")
        if mission.status != "queued":
            raise IllegalTransition(f"{mission_id} is {mission.status}, not queued")
        if mission.cancel_requested_at is not None:
            raise IllegalTransition(f"{mission_id} has a cancel requested")
        self._launch(mission)

    def _launch(self, mission: Mission) -> None:
        grant = mission.grant()
        cs = self.containment.create(f"S-{secrets.token_hex(8)}")
        try:
            _, token = issue_session(self.ms, mission.id, cs, session_ttl_s=grant.budgets.wall_s + SESSION_TTL_PAD_S,
                                     lease_ttl_s=self.lease_ttl_s, actor=ACTOR,
                                     expected_grant_hash=mission.grant_hash)
        except BaseException:
            self.containment.cleanup(cs)           # nothing was spawned in it, so it is empty
            raise
        run = _Run(mission.id, cs, None, None, cs.session_id, float(self.clock()))
        self._runs[mission.id] = run
        if self.ms.get_mission(mission.id).cancel_requested_at is not None:
            return self._kill_and_advance(run, "cancel")     # a cancel raced the launch: nothing is spawned
        # The token goes to the child on an inherited fd; it never touches argv, env, events or disk.
        read_fd = write_fd = None
        try:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, token.encode())
            os.close(write_fd)
            write_fd = None
            self._start_runtime(mission, run, read_fd)
        except Exception as exc:  # noqa: BLE001 -- adapter/provider code is untrusted; it must not crash the supervisor
            self._note(mission.id, {"start_failed": type(exc).__name__})
            self._kill_and_advance(run, "start_failed")
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)

    def _start_runtime(self, mission: Mission, run: _Run, token_fd: int) -> None:
        assert run.cs is not None
        cs = run.cs
        grant = mission.grant()
        brief = build_brief(mission, list(self.evidence_provider(mission)), list(self.memory_provider(mission)),
                            self._constraints(mission), max_bytes=self.max_brief_bytes)
        artifacts.store_text(self.store, self.artifact_dir, mission.id, "brief.md", brief.text, ACTOR)
        workspace = self.workspace_dir / mission.id
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)

        def spawn(argv: Sequence[str], env: dict[str, str] | None = None, cwd: str | None = None,
                  extra_fds: tuple[int, ...] = ()) -> int:
            full_env = {**self.worker_env, **(env or {}), "PE_MISSION_ID": mission.id,
                        "PE_TOKEN_FD": str(token_fd)}
            return self.containment.spawn(cs, list(argv), full_env, cwd or str(workspace),
                                          pass_fds=(token_fd, *extra_fds))

        context = RunContext(brief=brief.text, session=cs, token_fd=token_fd, env=dict(self.worker_env),
                             cwd=str(workspace), spawn=spawn)
        runtime = self.runtime_factory(mission)
        run.runtime = runtime
        run.handle = runtime.start(mission, context, grant)
        pid = getattr(run.handle, "pid", None)
        if isinstance(pid, int):
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
    @_serialised
    def poll_all(self) -> dict[str, str]:
        return {mid: self.poll(mid) for mid in list(self._runs)}

    @_serialised
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
        except Exception as exc:  # noqa: BLE001 -- adapter/provider code is untrusted; it must not crash the supervisor
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
        if update.state == "finished":
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
            raw = run.runtime.collect(run.handle)
            result = AgentResult.model_validate(raw.model_dump() if isinstance(raw, AgentResult) else raw)
        except Exception as exc:  # noqa: BLE001 -- adapter/provider code is untrusted; it must not crash the supervisor
            self._note(run.mission_id, {"result_invalid": type(exc).__name__})
            return self._kill_and_advance(run, "result_invalid")
        run.output_bytes += len(json.dumps(result.model_dump(mode="json")))
        if run.output_bytes > self.ms.get_mission(run.mission_id).grant().budgets.output_bytes:
            return self._kill_and_advance(run, "budget:output_bytes")      # the final result counts too
        try:
            artifacts.store_json(self.store, self.artifact_dir, run.mission_id, "result.json",
                                 result.model_dump(mode="json"), ACTOR)
        except Exception as exc:  # noqa: BLE001 -- a full disk must not leave a live session behind
            self._note(run.mission_id, {"result_store_failed": type(exc).__name__})
            return self._kill_and_advance(run, "result_invalid")
        run.result = result
        self._kill_and_advance(run, "result")        # the session is over: make sure nothing outlives it

    # ── ending a session ────────────────────────────────────────────────────
    @_serialised
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
        self._settle(run)

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
                except Exception as exc:  # noqa: BLE001 -- adapter/provider code is untrusted; it must not crash the supervisor
                    self._note(mission.id, {"restart_failed": type(exc).__name__})
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

    # ── restart recovery ────────────────────────────────────────────────────
    @_serialised
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
            if self._adopt(mission, reason, only_if_live=mission.status != "investigating"):
                acted.append(mission.id)
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
        self._runs[mission.id] = run
        self._kill_and_advance(run, reason)
        return True

    def _last_session(self, mission_id: str) -> ContainedSession | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_sessions WHERE mission_id=? "
                               "ORDER BY issued_at DESC, rowid DESC LIMIT 1", (mission_id,)).fetchone()
        if row is None:
            return None
        kind = "local_uid" if row["kind"] == "local_uid" else "local_cgroup"
        return ContainedSession(row["id"], kind, row["cgroup_path"], row["session_uid"])

    # ── external sessions (contract §11.1) ──────────────────────────────────
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


__all__ = ["ExternalRecord", "MissionsDisabled", "Supervisor", "UnsupportedTier", "default_memory_cards"]
