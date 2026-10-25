"""The mission Supervisor (contract §11) for Observe/Propose missions and, from slice 7, Repair missions.

It owns spawning, the brief, budgets, heartbeats, and the order of operations at the end of a session:

    revoke the token -> kill -> wait until `containment.is_empty()` -> ONLY THEN ask for a transition.

`is_empty()` is the sole "provably dead" predicate. Nothing here requests a terminal (or any other)
transition on a runtime's say-so, a timeout or a status report. Observe/Propose sessions can never enter
`executing`; a mutating tier is refused at `start()` unless the daemon wired the checkpoint service, the tripwire monitor and
the verifier (slice 7a), the side-door gate is closed and the mission is approved.

A Repair mission waits in `awaiting_decision` for its approval rows. When the one execution predicate holds, `start()` takes the
checkpoint (refused without a backup or baseline), captures the tripwire baseline, and only then issues the session (which takes
the mission into `executing`). When the session is provably dead the post-change manifest is captured, and the end of the session
is decided from host state: a result goes to verification, anything else is classified by `recovery` and never replayed.

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
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from planet_express.core.redact import redact
from planet_express.core.store import Store
from planet_express.integrations.rpc import RpcError
from planet_express.memory import store as memory_store
from planet_express.memory.models import Card
from planet_express.memory.retrieval import render_card, retrieve

from . import approvals, artifacts, leases, recovery
from .brief import DEFAULT_MAX_BYTES, EvidenceItem, build_brief
from .containment import ContainedSession, ContainmentError, SessionContainment
from .events import insert_event
from .hooks import HOOK_LOG_NAME, MAX_POLICY_FILE_BYTES
from .launcher.spec import SESSION_ID, LaunchSpec
from .models import Mission
from .runtime import (
    RUNTIME_EVENT_KINDS,
    AgentResult,
    AgentRuntime,
    AgentUpdate,
    RunContext,
)
from .sessions import SessionRow, issue_session, set_leader_pid
from .state import (
    TERMINAL,
    IllegalTransition,
    MissionNotFound,
    transition,
    transition_in_txn,
)
from .store import (
    MissionStore,
    NoFreeUid,
    SessionConflict,
    grant_resources,
    stack_lease_paths,
)

log = logging.getLogger(__name__)

ACTOR = "supervisor"
DECISION_ACTOR = "worker:decision"   # the actor of a card the worker raised (never `worker`, whose notes are free text)
SESSION_TTL_PAD_S = 300
LEASE_TTL_S = 60
PREFLIGHT_RETRY_S = 300       # while the preflight fails, look again this often (an operator may have repaired the host)
PREFLIGHT_REPROVE_S = 600     # a passing preflight is proven again this often, and at the start of every Repair session
START_DEFER_MAX_S = 1800      # a start waiting on a lease or a uid is handed to the operator after this long
START_DEFER_RETRY_S = 10      # and is asked again no sooner than this
VERIFY_WORKERS = 2            # verifications run on their own small pool, never on the poll thread
VERIFY_SHUTDOWN_WAIT_S = 5.0  # shutdown waits at most this long for in-flight verifications
START_ATTEMPTS = 3            # consecutive failures to even start a queued mission before the operator is told
MAX_MEMORY_HITS = 20
MAX_HOOK_READ = 256 * 1024       # bytes of a session's hook log read per poll (the final drain reads on until it is empty)
MAX_FINAL_DRAINS = 64
MAX_HOOK_LINE = 4096
CLEANUP_PREFIX = "X-"          # a cleanup session is named for the session whose workspace it empties
CLEANUP_SH, CLEANUP_FIND = "/bin/sh", "/usr/bin/find"


def cleanup_id(session_id: str) -> str:
    """The cleanup session's own id: fixed for a given session (a restarted Core finds it again) and always inside the
    launcher's 64-character limit, whatever the length of the session id."""
    return CLEANUP_PREFIX + hashlib.sha256(session_id.encode()).hexdigest()[:40]


# The session may have removed its own write permission on a directory it made, so restore the owner's access first
# (chmod does not follow symlinks), then delete. The workspace path is a positional parameter, never part of the script.
# Both discard their output: the launcher's merged output pipe is never read for a cleanup session, and a full pipe would block find.
CLEANUP_SCRIPT = ('chmod -R u+rwX -- "$1" >/dev/null 2>&1; exec ' + CLEANUP_FIND
                  + ' "$1" -xdev -mindepth 1 -delete >/dev/null 2>&1')       # nobody drains the session's output: never let it fill
CLEANUP_WALL_S = 60.0          # a cleanup session that has not finished by then is killed and tried again
CLEANUP_RETRY_S = 300.0        # after a failed cleanup, wait this long before launching another for the same session
HOOK_POLICY_ENV_MAX = 3500    # above this the hook policy travels as a file (the launcher caps an environment value at 4096)
MAX_HOOK_FIELD = 400
MAX_DECISION_REQUESTS = 5       # worker-raised decision cards per mission; they never approve anything
MAX_DECISION_TEXT = 1000
MAX_OPTION_TEXT = 120
MAX_OPTIONS = 6


class MissionsDisabled(RuntimeError):
    """`missions.enabled` is false: nothing may be started."""


class UnsupportedTier(RuntimeError):
    """Only Observe/Propose missions run under this supervisor."""


class RepairRefused(UnsupportedTier):
    """A mutating-tier start refused because a side door around the worker boundary is open (brief 6.8), or because
    nobody could say it is closed. A subclass of UnsupportedTier: mutating tiers were already refused here."""


class RepairNotReady(RepairRefused):
    """A Repair start that cannot happen yet for a reason that may clear by itself (no approval, the monitor is not running, the
    side doors are open): the mission waits and the poll loop asks again. Not counted as a failed start."""


class StartDeferred(RepairNotReady):
    """A start that must wait for a lease, a free uid or an earlier session to clear. Transient: never counted as a failed
    start, noted once per reason, and handed to the operator only after START_DEFER_MAX_S."""


class ResumeRefused(RuntimeError):
    """An operator action on a tripwire card that cannot be taken now (the message says why and is safe to show)."""


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
    paused_at: float | None = None            # a tripwire pause is not session time: the wall budget does not run meanwhile
    pause_extended_to: float | None = None    # how far the session's expiry has been moved to cover that pause
    hook_offset: int = 0                      # how much of the session's hook log has been read
    hook_log_fault: bool = False              # a shrunk or vanished log has been reported once
    hook_dir: Path | None = None              # the Core-owned directory holding this session's hook settings
    hook_sha: str | None = None               # sha256 of settings.json at launch: it is checked at every poll
    policy_sha: str | None = None             # sha256 of policy.json when the policy was too large for the environment


def default_memory_cards(store: Store, mission: Mission) -> list[Card]:
    grant = mission.grant()
    tags = [f"stack:{s}" for s in grant.scope.stacks] + [f"service:{s}" for s in grant.scope.services]
    try:
        with store._connect() as conn:
            return list(retrieve(conn, tags, mission.goal).cards)
    except Exception:
        return []


MAX_EVIDENCE_TEXT = 4000


def default_incident_evidence(store: Store, mission: Mission) -> list[EvidenceItem]:
    """The incident evidence `MissionService.investigate_incident` recorded for the mission, as brief evidence. Only an event
    written by an operator actor counts (a session can append notes, and its words are never evidence about the incident). The
    text is redacted and bounded again here; the brief quotes it as DATA and redacts the document once more."""
    try:
        with store._connect() as conn:
            row = conn.execute("SELECT body_json FROM mission_events WHERE mission_id=? AND kind='note' "
                               "AND actor LIKE 'operator:%' AND body_json LIKE '%\"incident_evidence\"%' "
                               "ORDER BY seq LIMIT 1", (mission.id,)).fetchone()
        body = json.loads(row[0]) if row else None
    except Exception:                                   # noqa: BLE001 - evidence is an aid; a read failure is no evidence
        return []
    evidence = body.get("incident_evidence") if isinstance(body, dict) else None
    if not isinstance(evidence, dict):
        return []
    facts = "\n".join(f"{k}: {evidence[k]}" for k in ("id", "kind", "resource", "severity", "status", "condition", "summary")
                      if isinstance(evidence.get(k), (str, int, float)))
    items = [EvidenceItem(f"incident:{mission.incident_id or 'unknown'}", redact(facts)[:MAX_EVIDENCE_TEXT])] if facts else []
    events = evidence.get("events")
    if isinstance(events, list) and events:
        items.append(EvidenceItem("incident:events", redact("\n".join(str(e) for e in events[-10:]))[:MAX_EVIDENCE_TEXT]))
    return items


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

    def env(self, cs: ContainedSession) -> dict[str, str]:
        """Extra environment for the session once its sockets are bound (e.g. `PE_DOCKER_SOCKET`, its own proxy socket)."""
        return {}


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
                 repair_gate: Callable[[], Any] | None = None, verifier: Any = None,
                 checkpoint: Any = None, tripwire: Any = None, docker_proxy: Any = None,
                 hook_policy: Callable[[Mission], str] | None = None,
                 hook_install: Callable[[Path], Mapping[str, str] | None] | None = None, hook_root: Path | None = None,
                 model_broker: Any = None):
        self.store = store
        self.hook_install = hook_install              # Path -> writes the runtime's hook settings into that (Core-owned) directory
        self.hook_root = Path(hook_root) if hook_root else None    # Core-owned, session-readable, never session-writable
        self.hook_policy = hook_policy                # Mission -> the hook policy JSON put in the session's environment
        self.checkpoint = checkpoint                  # `checkpoint.CheckpointService`; None: no Repair mission can start
        self.tripwire = tripwire                      # `tripwire.TripwireMonitor`; None: no Repair mission can start
        self.docker_proxy = docker_proxy              # slice 7b: `.resume(mission_id, grant_hash)`; None: nothing to re-enable
        self.model_broker = model_broker              # the per-session model broker: paused and resumed with the proxy
        self.verifier = verifier                      # `verify.runner.Verifier`; None: a verifying mission goes to the operator
        self.repair_gate = repair_gate                # side-door report (brief 6.8); None means a repair start is refused
        self.tool_broker = tool_broker                # `missions.tools.ToolBroker`; None: every tool call is refused
        if tool_broker is not None and getattr(tool_broker, "on_refusal_cap", None) is None:
            tool_broker.on_refusal_cap = self.freeze_for_tool_abuse
        self.socket_provider = socket_provider
        self.preflight_fn = preflight                 # returns a PreflightReport; None means "trusted" (tests)
        self.preflight_report: Any = None
        self._preflight_at = 0.0
        self.ms = MissionStore(store)
        # So a stack lease also leases the stack's directory: under the checkpoint's resolved root and the tripwire's configured one.
        self.ms.stacks_roots = tuple(dict.fromkeys(str(r) for r in (getattr(checkpoint, "stacks_root", None),
                                                                    getattr(tripwire, "stacks_root", None)) if r))
        self.containment = containment
        self.runtime_factory = runtime_factory
        self.clock = clock
        self._enabled = enabled if callable(enabled) else (lambda: bool(enabled))
        self._start_failures: dict[str, int] = {}
        self._cancel_held: set[str] = set()     # missions whose held cancel has been recorded
        self._waiting: dict[str, str] = {}               # mission id -> the last "cannot start yet" reason noted
        self._deferred_since: dict[str, float] = {}      # mission id -> when its start first had to wait for a resource
        self._deferred_retry: dict[str, float] = {}      # mission id -> do not ask again before this time
        self.heartbeat_interval_s = max(1.0, lease_ttl_s / 3)    # live leases are refreshed this often during a slow start
        self.artifact_dir = Path(artifact_dir) if artifact_dir else store.path.parent / "mission-artifacts"
        self.workspace_dir = Path(workspace_dir) if workspace_dir else store.path.parent / "mission-workspaces"
        self.memory_provider = memory_provider or (lambda m: default_memory_cards(store, m))
        self.evidence_provider = evidence_provider or (lambda m: default_incident_evidence(store, m))
        self.worker_env = dict(worker_env or {})
        self.max_brief_bytes = max_brief_bytes
        self.lease_ttl_s = lease_ttl_s
        self._runs: dict[str, _Run] = {}
        self._cleaners: dict[str, tuple[float, int]] = {}    # cleanup session id -> (launched at, uid), see `_clear_workspace`
        self._cleanup_leftovers_reaped: set[str] = set()    # session ids whose cleanup cgroup a previous Core may have left was checked
        self._cleanup_retry_at: dict[str, float] = {}    # session id -> do not launch another cleanup session before this time
        self._verify_pool: Any = None
        self._verifying: dict[str, Any] = {}
        self._verify_guard = threading.Lock()
        self._shutting_down = False
        self._lifecycle = threading.RLock()
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

    def launcher_ready(self, *, reprove: bool = False) -> bool:
        """The launcher preflight gate (contract 22.4). True when no preflight is configured, or the last one passed.
        While it fails the host is proposal-only: it is re-run at most every PREFLIGHT_RETRY_S. A pass is not forever: it is
        proven again every PREFLIGHT_REPROVE_S, and at once when `reprove` is set (every Repair start)."""
        if self.preflight_fn is None:
            return True
        now = float(self.clock())
        report = self.preflight_report
        stale = report is not None and report.ok and (reprove or now - self._preflight_at >= PREFLIGHT_REPROVE_S)
        if report is None or stale or (not report.ok and now - self._preflight_at >= PREFLIGHT_RETRY_S):
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

    def _launcher_proven(self) -> bool:
        """The last preflight passed. Unlike `launcher_ready` this never runs one: a cleanup sweep must not run probe sessions on the
        poll thread or use up the retry slot a real mission start needs. Until the first preflight has run, it is False."""
        if self.preflight_fn is None:
            return True
        return bool(self.preflight_report is not None and self.preflight_report.ok)

    def _note(self, mission_id: str, body: dict[str, Any], kind: str = "note") -> None:
        self.ms.append_event(mission_id, kind, ACTOR, body)

    def _attempts(self, mission_id: str) -> int:
        """Sessions ever started for the mission, counted from session rows (events can carry worker text)."""
        with self.store._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM mission_sessions WHERE mission_id=?", (mission_id,)).fetchone()[0]

    # ── start ───────────────────────────────────────────────────────────────
    @property
    def tracked_sessions(self) -> int:
        """Sessions Core still supervises: after `shutdown()`, any left were not proven gone."""
        return len(self._runs) + len(self._cleaners)

    def begin_shutdown(self) -> None:
        """No new session starts from here on (the runner calls this first; `shutdown()` then kills what is running)."""
        with self._lifecycle:                 # waits for a start that is already inside its launch, so none outlives this
            self._shutting_down = True

    @_per_mission
    def start(self, mission_id: str) -> None:
        # The whole start (checkpoint, baseline, session, launch) runs under the lifecycle lock, and `begin_shutdown` takes
        # it to set the flag: a start either finishes (its run is registered, so shutdown() kills it while the tripwire is
        # still watching) or sees the flag and launches nothing. There is no window in between.
        with self._lifecycle:
            return self._start_locked(mission_id)

    def _start_locked(self, mission_id: str) -> None:
        self._require_enabled()
        if self._shutting_down:
            raise RepairNotReady("the supervisor is shutting down")
        if not self.launcher_ready():
            raise ProposalOnly(self.preflight_report.reason or "the launcher preflight failed")
        mission = self.ms.get_mission(mission_id)
        if mission.tier not in ("observe", "propose"):
            self.require_repair_preconditions()
            if mission.tier != "repair" or not self.mutating_ready:
                raise UnsupportedTier(f"{mission.tier} missions are not run by this supervisor")
            if not self.launcher_ready(reprove=True):         # a mutating session re-proves the launcher every time
                raise ProposalOnly(self.preflight_report.reason or "the launcher preflight failed")
            with self._heartbeating(mission_id):
                return self._start_repair(mission)
        if mission_id in self._runs:
            raise ValueError(f"{mission_id} already has a running session")
        if mission.status != "queued":
            raise IllegalTransition(f"{mission_id} is {mission.status}, not queued")
        if mission.cancel_requested_at is not None:
            raise IllegalTransition(f"{mission_id} has a cancel requested")
        with self._heartbeating(mission_id):
            self._launch(mission)

    @contextlib.contextmanager
    def _heartbeating(self, mission_id: str):
        """A start can spend a long time in the checkpoint, the baseline and the launch, all on the one poll thread. While it
        does, a helper thread keeps refreshing the leases of every live session (and of the mission being started), so
        a slow start cannot make another session lose its lease. It touches the store only, and takes no mission lock."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_interval_s):
                ids = {mid for mid, run in list(self._runs.items()) if run.phase == "running"} | {mission_id}
                for mid in ids:
                    with contextlib.suppress(Exception):          # a lost lease is the poll step's business
                        leases.heartbeat(self.store, mid, self.lease_ttl_s)

        thread = threading.Thread(target=beat, name="pe-start-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(5)

    @property
    def mutating_ready(self) -> bool:
        """Slice 7a: the three things a Repair mission cannot run without are wired. Missing any, a mutating start is refused."""
        return self.checkpoint is not None and self.tripwire is not None and self.verifier is not None

    def _start_repair(self, mission: Mission) -> None:
        """Checkpoint, tripwire baseline, then the session. Every refusal leaves the mission in `awaiting_decision` with
        nothing launched. The approval predicate is asked first and again, atomically, by `issue_session`."""
        if mission.id in self._runs:
            raise ValueError(f"{mission.id} already has a running session")
        if mission.status != "awaiting_decision":
            raise IllegalTransition(f"{mission.id} is {mission.status}, not awaiting_decision")
        if mission.cancel_requested_at is not None:
            raise IllegalTransition(f"{mission.id} has a cancel requested")
        verdict = approvals.execution_predicate(self.store, mission.id)
        if not verdict.ok:
            raise RepairNotReady("; ".join(verdict.reasons)[:300])
        if not getattr(self.tripwire, "running", False):
            # A Repair session without a running monitor is a defect: nothing would pause it. The runner restarts the
            # monitor on its next loop; the start waits for that.
            raise RepairNotReady("the tripwire monitor is not running")
        # The leases come FIRST: a mission that cannot have its resources waits cheaply, before a checkpoint is spent on it. The
        # acquire is idempotent for the same mission, so `issue_session` takes the same leases (and fences) again.
        held_early = self._acquire_early(mission)
        try:
            retire = getattr(self.checkpoint, "retire_unused", None)
            if mission.checkpoint_id is not None and retire is not None:
                # An earlier start got as far as a checkpoint but never launched a session. That picture of the host may be
                # stale by now; take a fresh one rather than reuse it. (A checkpoint a session ran under is never retired.)
                try:
                    if retire(mission.id):                 # the service records the retirement itself
                        mission = self.ms.get_mission(mission.id)
                except Exception as exc:                   # noqa: BLE001 - a stale checkpoint that cannot be retired must not be used
                    self._note(mission.id, {"checkpoint_retire_failed": type(exc).__name__, "why": str(exc)[:200]})
                    raise RepairNotReady("the unused checkpoint from an earlier start could not be retired; trying again later") from None
            if mission.checkpoint_id is None:
                from .checkpoint import CheckpointScope
                grant = mission.grant()
                scope = CheckpointScope.from_grant_scope(grant.scope, units=tuple(r.unit for r in grant.sudo.rules))
                try:
                    created = self.checkpoint.create(mission.id, scope)
                except Exception as exc:              # noqa: BLE001 - no checkpoint, no mutation (S5: backup floor, baseline)
                    self._note(mission.id, {"checkpoint_refused": type(exc).__name__, "why": str(exc)[:200]})
                    raise RepairRefused(f"no checkpoint could be taken ({type(exc).__name__})") from None
                self._note(mission.id, {"checkpoint_taken": str(created), "no_backup": bool(getattr(created, "no_backup", False))})
            try:
                self.tripwire.begin(mission.id)
            except Exception as exc:                  # noqa: BLE001 - a baseline that cannot be captured means no session
                self._note(mission.id, {"baseline_refused": type(exc).__name__, "why": str(exc)[:200]})
                raise RepairRefused(f"the tripwire baseline could not be captured ({type(exc).__name__})") from None
            self._launch(self.ms.get_mission(mission.id))
        except BaseException:
            if held_early and mission.id not in self._runs:
                self._release_early(mission.id)        # nothing started: the leases do not stay held by a mission that is not running
            raise

    def _acquire_early(self, mission: Mission) -> bool:
        """Take the grant's leases before the expensive start steps. A conflict is a wait (StartDeferred), not a failure."""
        grant = mission.grant()
        wanted = grant_resources(grant)
        if not wanted:
            return False
        try:
            declared = stack_lease_paths(grant, self.ms.stacks_roots)
        except ValueError as exc:                    # a stacks root that is not absolute: stack directories cannot be leased safely
            raise RepairNotReady(str(exc)) from None
        try:
            leases.acquire(self.store, wanted, mission.id, self.lease_ttl_s, declared)
        except leases.LeaseConflict as exc:
            raise StartDeferred(f"waiting for {exc.resource}: leased by {exc.holder}") from None
        return True

    def _release_early(self, mission_id: str) -> None:
        with contextlib.suppress(Exception):
            if self.ms.get_mission(mission_id).status == "awaiting_decision":
                leases.release(self.store, mission_id)

    def require_repair_preconditions(self) -> None:
        """Brief 6.8: a Repair-tier (or higher) worker starts only if every side door is closed. Asked afresh every time,
        because a door can open between missions. A gate that is missing, crashes or returns anything but a passing
        report refuses; Observe and Propose never come here."""
        if self.repair_gate is None:
            raise RepairNotReady("the side-door preflight is not configured, so no mutating mission may start")
        try:
            report = self.repair_gate()
            ok = bool(report.ok)
            reason = None if ok else (report.reason or "a side door is open")
        except Exception as exc:                   # noqa: BLE001 - a gate that cannot run is a closed gate
            raise RepairNotReady(f"the side-door preflight could not run ({type(exc).__name__})") from None
        if not ok:
            raise RepairNotReady(reason)

    def _launch(self, mission: Mission) -> None:
        if not self.launcher_ready():          # also on a restart after a crash: proposal-only means no session at all
            raise ProposalOnly(self.preflight_report.reason or "the launcher preflight failed")
        grant = mission.grant()
        session_id = f"S-{secrets.token_hex(8)}"
        # Intent is journaled FIRST (contract 11), before anything is registered, issued or launched: a failed journal
        # write raises here with nothing started, no `launching` row and no uid held. A launch that is then refused
        # closes its own intent as `not_applied`.
        intent_id = recovery.journal_intent(self.ms, mission.id, "investigation" if mission.status == "investigating" else "execution",
                                f"session {session_id} start", actor=ACTOR)
        # `start_session` is the sole uid allocator: it commits the row (state `launching`, uid held) before any
        # launcher runs, so nothing is left behind if it refuses.
        try:
            cs, token = issue_session(
                self.ms, mission.id, session_id, self.containment.cgroup_path(session_id),
                session_ttl_s=grant.budgets.wall_s + SESSION_TTL_PAD_S, lease_ttl_s=self.lease_ttl_s,
                uid_range=self.containment.uid_range, actor=ACTOR, expected_grant_hash=mission.grant_hash)
        except BaseException as exc:
            with contextlib.suppress(Exception):
                recovery.journal_done(self.ms, mission.id, intent_id, "not_applied", actor=ACTOR)   # only OUR intent
            # A grant that changed under the start is a refusal (the caller re-reads), not a wait.
            if isinstance(exc, (leases.LeaseConflict, NoFreeUid, SessionConflict)) and "grant changed" not in str(exc):
                raise StartDeferred(f"waiting: {type(exc).__name__}: {str(exc)[:200]}") from None
            raise
        run = _Run(mission.id, cs, None, None, cs.session_id, float(self.clock()))
        self._runs[mission.id] = run
        if self._shutting_down:
            # Shutdown began while this start was in flight (its snapshot of `_runs` may predate this run): the run is
            # registered FIRST and the flag read AFTER, so either shutdown() sees it or this does. Nothing is launched.
            self._shutdown_one(run)
            raise RepairNotReady("the supervisor is shutting down")
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
        install_env: dict[str, str] = {}
        if self.hook_install is not None:
            # The settings that install the hooks live OUTSIDE the session-owned workspace, in a directory Core owns (0755,
            # settings.json 0644): the session can read them and cannot edit or delete them. Its hash is taken now and checked
            # at every poll. Anything that raises here fails the start: a mission configured for hooks never runs without them.
            if self.hook_root is None:
                raise ContainmentError("hooks are configured to install but no Core-owned hook root is")
            hook_dir = self.hook_root / cs.session_id
            hook_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            hook_dir.chmod(0o755)
            run.hook_dir = hook_dir
            install_env = dict(self.hook_install(hook_dir) or {})
            settings = hook_dir / "settings.json"
            settings.chmod(0o644)
            run.hook_sha = hashlib.sha256(settings.read_bytes()).hexdigest()
        def spawn(argv: Sequence[str], env: dict[str, str] | None = None, cwd: str | None = None,
                  extra_fds: tuple[int, ...] = ()) -> int:
            if extra_fds:
                raise ContainmentError("the launcher's fd map is fixed (0, 1, 2, 3): no extra fds")
            full_env = {**self.worker_env, **(env or {}), **install_env, "PE_MISSION_ID": mission.id,
                        "PE_SESSION_TOKEN_FD": "3"}          # fd 3 is always the token pipe in the child
            if self.hook_policy is not None:
                # The hook layer's inputs, set by Core and nowhere else: the policy it enforces and where it records. The
                # runtime's hooks inherit them; the session token is never given to a hook (it is on fd 3 only).
                policy_text = self.hook_policy(mission)
                if len(policy_text) <= HOOK_POLICY_ENV_MAX:
                    full_env["PE_HOOK_POLICY"] = policy_text
                else:
                    # The launcher refuses any environment value over 4096 characters, and a host with many protected paths
                    # can exceed that. The policy then goes in the Core-owned hook directory (the session can read it and
                    # cannot change it) and its hash is checked at every poll, like settings.json.
                    if run.hook_dir is None:
                        raise ContainmentError("the hook policy is too large for the session environment and no Core-owned "
                                               "hook directory is configured")
                    if len(policy_text.encode("utf-8")) > MAX_POLICY_FILE_BYTES:
                        raise ContainmentError("the hook policy is larger than the hook will read")
                    policy_file = run.hook_dir / "policy.json"
                    policy_file.write_text(policy_text, encoding="utf-8")
                    policy_file.chmod(0o644)
                    run.policy_sha = hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
                    full_env["PE_HOOK_POLICY_FILE"] = str(policy_file)
                full_env["PE_HOOK_LOG"] = str(workspace / HOOK_LOG_NAME)
            if self.socket_provider is not None:
                full_env["PE_WORKER_SOCKET"] = self.socket_provider.path(cs.session_id)
                # Bound BEFORE the launch (so the agent's first connect cannot find nothing); the launcher then hands
                # the directory and sockets to the session. Registered first so a failed launch stops it again.
                run.stop_socket = self.socket_provider.start(cs)
                full_env.update(self.socket_provider.env(cs))
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
        if mission.tier in ("observe", "propose"):
            first = f"This mission is {mission.tier}: observe and propose only; make no changes to the host."
        else:
            first = (f"This mission is {mission.tier}: you may change only what the grant's scope names, and a verification "
                     "plan Core agreed beforehand decides whether you succeeded, not your own report.")
        out = [first, "Stay inside the scope above; never touch a protected resource.", budgets]
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
        if mission.cancel_requested_at is not None:
            return self._kill_and_advance(run, "cancel")
        expected = "executing" if mission.tier == "repair" else "investigating"
        if mission.tier == "repair" and mission.status == "paused_tripwire":
            # The monitor froze the session and paused the mission: leave both alone (no kill, no budget clock) until an
            # operator resumes, rolls back, takes over or cancels. The lease is kept alive so a resume is not fenced out.
            if run.paused_at is None:
                run.paused_at = float(self.clock())
            self._extend_session_for_pause(run)      # the pause is not session time, so the session must not expire in it
            with contextlib.suppress(leases.LeaseLost):
                leases.heartbeat(self.store, run.mission_id, self.lease_ttl_s)
            return
        if mission.status != expected:
            return self._kill_and_advance(run, "state_changed")
        if run.paused_at is not None:
            self._extend_session_for_pause(run)          # a pause that ends outside resume() still moves the expiry
            run.started_at += float(self.clock()) - run.paused_at
            run.paused_at = run.pause_extended_to = None
        if not self._hook_settings_intact(run):
            self._note(run.mission_id, {"hook_settings": "changed or missing", "session": run.session_id})
            return self._kill_and_advance(run, "hook_tamper")        # a session that edited its own hooks does not keep running
        try:
            raw = run.runtime.status(run.handle)
            update = AgentUpdate.model_validate(raw.model_dump() if isinstance(raw, AgentUpdate) else raw)
        except Exception as exc:
            self._note(run.mission_id, {"runtime_status_error": type(exc).__name__})
            # An adapter whose process vanished may raise instead of reporting: that is a crash, not a bug.
            return self._kill_and_advance(run, "crash" if self.containment.is_empty(run.cs) else "runtime_error")
        self._record(run, update)
        self._drain_hook_log(run)
        grant = mission.grant()

        def over_budget() -> str | None:
            elapsed = float(self.clock()) - run.started_at
            for name, over in (("wall", elapsed > grant.budgets.wall_s),
                               ("tool_calls", run.tool_calls > grant.budgets.tool_calls),
                               ("output_bytes", run.output_bytes > grant.budgets.output_bytes)):
                if over:
                    return name
            return None
        if (name := over_budget()) is not None:
            return self._kill_and_advance(run, f"budget:{name}")
        try:
            leases.heartbeat(self.store, run.mission_id, self.lease_ttl_s)
        except leases.LeaseLost:
            return self._kill_and_advance(run, "lease_lost")
        if update.state == "finished" or run.submitted is not None:
            return self._collect(run)
        if update.state == "failed":
            return self._kill_and_advance(run, "crash")
        if self.containment.is_empty(run.cs):
            # The session emptied after the status read above. A clean finish lands here as often as a crash does (the agent
            # wrote its result and exited between the two reads), so ask the runtime once more before calling it a crash.
            try:
                raw = run.runtime.status(run.handle)
                again = AgentUpdate.model_validate(raw.model_dump() if isinstance(raw, AgentUpdate) else raw)
            except Exception:                                       # noqa: BLE001 -- unreadable is a crash, as above
                again = None
            if again is not None:
                self._record(run, again)
                self._drain_hook_log(run)
                if (name := over_budget()) is not None:         # the last events count against the budgets too
                    return self._kill_and_advance(run, f"budget:{name}")
                if again.state == "finished":
                    return self._collect(run)
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

    def _hook_settings_intact(self, run: _Run) -> bool:
        if run.hook_dir is None:
            return True
        for name, want in (("settings.json", run.hook_sha), ("policy.json", run.policy_sha)):
            if want is None:
                continue
            try:
                fd = os.open(run.hook_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                return False
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return False
                digest, total = hashlib.sha256(), 0
                while chunk := os.read(fd, 1 << 16):        # to EOF: a single read may legally return less than the file holds
                    digest.update(chunk)
                    total += len(chunk)
                    if total > 2 * MAX_POLICY_FILE_BYTES:
                        return False
                if digest.hexdigest() != want:
                    return False
            except OSError:
                return False
            finally:
                os.close(fd)
        return True

    def _hook_log_fault(self, run: _Run, what: str) -> None:
        if not run.hook_log_fault:
            run.hook_log_fault = True
            self._note(run.mission_id, {"hook_log_fault": what, "session": run.session_id})

    def _drain_hook_log(self, run: _Run, *, final: bool = False) -> None:
        """Append what the session's hooks recorded to the mission's events. The log lives in the session's own workspace, so
        everything in it is untrusted data: opened without following a symlink, regular files only, a bounded read per poll,
        complete lines only, EVERY complete line in what was read (the offset advances by exactly what was processed, so nothing is
        skipped and nothing is read twice), each parsed strictly and cut down to a few clipped string fields. The events carry
        actor `worker` and kind `tool` or `note`: nothing a hook line says can name a state or a decision. `final=True` (after
        the session is provably dead, before anything is settled) reads on until the log is empty. A log that shrank or vanished
        is reported as a session fault."""
        if self.hook_policy is None or run.workspace is None:
            return
        for _ in range(MAX_FINAL_DRAINS if final else 1):
            if not self._drain_once(run):
                return

    def _drain_once(self, run: _Run) -> bool:
        """Read and process one chunk. True when there may be more to read."""
        assert run.workspace is not None
        try:
            fd = os.open(run.workspace / HOOK_LOG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if run.hook_offset > 0:
                self._hook_log_fault(run, "the log disappeared")
            return False
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return False
            if info.st_size < run.hook_offset:
                self._hook_log_fault(run, "the log is shorter than what was already read")
                run.hook_offset = info.st_size
                return False
            os.lseek(fd, run.hook_offset, os.SEEK_SET)
            data = os.read(fd, MAX_HOOK_READ)
        except OSError:
            return False
        finally:
            os.close(fd)
        end = data.rfind(b"\n") + 1
        if end == 0:
            if len(data) >= MAX_HOOK_READ:
                run.hook_offset += len(data)              # one enormous line: skip it
                return True
            return False
        run.hook_offset += end
        for raw in data[:end].splitlines():
            if len(raw) > MAX_HOOK_LINE:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("kind") not in ("tool", "note") or not isinstance(line.get("data"), dict):
                continue
            body = {"via": "hook"}
            for key in ("hook", "tool", "verdict", "class", "reason", "input", "outcome"):
                value = line["data"].get(key)
                if isinstance(value, str):
                    body[key] = value[:MAX_HOOK_FIELD]
            if len(body) == 1:
                continue                                  # nothing a hook records: not an event
            self.ms.append_event(run.mission_id, line["kind"], "worker", body)
            run.output_bytes += len(raw)
            if line["kind"] == "tool":
                run.tool_calls += 1
        return len(data) >= MAX_HOOK_READ                  # a full chunk: there may be more behind it

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

    # ── operator actions on a Repair mission (the actor is the authenticated operator, built by the caller) ──
    @staticmethod
    def _operator(actor: str) -> str:
        if not actor.startswith("operator:") or len(actor) <= len("operator:"):
            raise ValueError("this is an operator decision: pass actor='operator:<name>'")
        return actor

    def _repair_mission(self, mission_id: str, *statuses: str) -> Mission:
        mission = self.ms.get_mission(mission_id)
        if mission.tier != "repair":
            raise ResumeRefused(f"{mission_id} is not a repair mission")
        if mission.status not in statuses:
            raise ResumeRefused(f"{mission_id} is {mission.status}; this action needs {' or '.join(statuses)}")
        return mission

    @_per_mission
    def resume(self, mission_id: str, actor: str) -> str:
        """Operator 'resume' on a tripwire card. In this order, every step before the thaw able to refuse: the grant hash is
        re-authorised (the execution predicate), the tripwire baseline is re-taken (the operator has seen what tripped it), the
        mission goes back to `executing`, the brokered endpoints and the session's Docker proxy are re-opened, and only then is
        the cgroup thawed. A failure after the transition freezes and pauses it again. After a Core restart there is no
        supervised session to thaw, so a resume is refused (roll back, take over or cancel instead)."""
        self._require_enabled()
        actor = self._operator(actor)
        mission = self._repair_mission(mission_id, "paused_tripwire")
        run = self._runs.get(mission_id)
        if run is None or run.phase != "running" or run.cs is None:
            raise ResumeRefused("no supervised session for this mission (Core restarted): roll back, take over or cancel")
        if mission.cancel_requested_at is not None:
            raise ResumeRefused("a cancel is already requested")
        verdict = approvals.execution_predicate(self.store, mission_id)
        if not verdict.ok:
            raise ResumeRefused("the approval no longer covers this grant: " + "; ".join(verdict.reasons)[:200])
        if self.tripwire is None or not getattr(self.tripwire, "running", False):
            raise ResumeRefused("the tripwire monitor is not running")
        # The reviewed state becomes the new baseline, but the baseline that tripped is kept until the resume has held: a resume
        # that fails after this point puts it back, so the evidence of what tripped is not lost to a failed attempt.
        try:
            self.tripwire.begin(mission_id, keep_previous=True)
        except Exception as exc:                          # noqa: BLE001
            raise ResumeRefused(f"the tripwire baseline could not be re-taken ({type(exc).__name__})") from None
        try:
            transition(self.store, mission_id, "executing", actor)
        except IllegalTransition as exc:
            self._restore_tripped_baseline(mission_id)
            raise ResumeRefused(str(exc)[:200]) from None
        try:
            restore = getattr(self.tool_broker, "restore", None)
            if restore is not None:
                restore(mission_id)
            if self.docker_proxy is not None:
                self.docker_proxy.resume(mission_id, mission.grant_hash)
            if self.model_broker is not None:
                self.model_broker.resume(mission_id, mission.grant_hash)
            self._extend_session_for_pause(run)       # the whole pause, including the part since the last poll
            leases.heartbeat(self.store, mission_id, self.lease_ttl_s)      # a lost lease refuses the resume
            self.containment.thaw(run.cs)
        except Exception as exc:                          # noqa: BLE001 - back to paused, frozen and closed
            self._pause_again(mission_id, run, actor, type(exc).__name__)
            self._restore_tripped_baseline(mission_id)
            raise ResumeRefused(f"the session could not be resumed ({type(exc).__name__}); it stays paused") from None
        with contextlib.suppress(Exception):
            self.tripwire.drop_previous_baseline(mission_id)
        if run.paused_at is not None:
            run.started_at += float(self.clock()) - run.paused_at      # the pause was not session time
            run.paused_at = run.pause_extended_to = None
        self.ms.append_event(mission_id, "decision", actor, {"event": "mission.resumed", "grant_hash": mission.grant_hash})
        return "executing"

    def _restore_tripped_baseline(self, mission_id: str) -> None:
        with contextlib.suppress(Exception):
            self.tripwire.restore_previous_baseline(mission_id)

    def _extend_session_for_pause(self, run: _Run) -> None:
        """Move the session's `expires_at` by the time it has spent paused (once, whole seconds, remainder carried): the wall
        budget is shifted by a pause, so the expiry must be too, or a resumed session is unauthenticated."""
        if run.paused_at is None:
            return
        since = run.pause_extended_to if run.pause_extended_to is not None else run.paused_at
        delta = int(float(self.clock()) - since)
        if delta > 0:
            self.ms.extend_session(run.session_id, delta)
            run.pause_extended_to = since + delta

    def _pause_again(self, mission_id: str, run: _Run, actor: str, why: str) -> None:
        with contextlib.suppress(Exception):
            revoke = getattr(self.tool_broker, "revoke", None)
            if revoke is not None:
                revoke(mission_id)
        with contextlib.suppress(Exception):
            if self.docker_proxy is not None:
                self.docker_proxy.pause(mission_id)
        with contextlib.suppress(Exception):
            if self.model_broker is not None:
                self.model_broker.pause(mission_id)
        with contextlib.suppress(Exception):
            if run.cs is not None:
                self.containment.freeze(run.cs)
        try:
            now = int(self.store._clock())
            with self.store._write() as conn:
                transition_in_txn(conn, mission_id, "paused_tripwire", "core", now)
                insert_event(conn, mission_id, "decision", "core", {"event": "mission.resume_failed", "by": actor, "why": why}, now)
        except Exception:                                 # noqa: BLE001
            log.exception("could not re-pause %s after a failed resume; killing its session", mission_id)
            if run.cs is not None:
                with contextlib.suppress(Exception):
                    self.containment.kill(run.cs)

    @_per_mission
    def roll_back(self, mission_id: str, actor: str, *, ack_drift: bool = False) -> Any:
        """Operator 'roll back'. A paused mission goes through the monitor (kill, wait for empty, capture, restore). A
        mission the session has already left (needs_operator, recovery_required, rollback_pending, verifying) is restored here
        after proving no session is alive. The restore's own refusals (drift, missing manifest) are reported and left with
        the operator; `ack_drift` is the operator's explicit acknowledgement that overrides a drift refusal."""
        self._require_enabled()
        actor = self._operator(actor)
        mission = self._repair_mission(mission_id, "paused_tripwire", "needs_operator", "recovery_required",
                                       "rollback_pending", "verifying")
        if self.checkpoint is None or not mission.checkpoint_id:
            raise ResumeRefused("this mission has no checkpoint to restore")
        if mission.status == "paused_tripwire":
            if ack_drift:
                raise ResumeRefused("acknowledging drift is for a restore the first attempt refused; roll back first")
            report = self.tripwire.roll_back(mission_id, actor)
        else:
            self._stop_session_for_restore(mission_id)
            try:
                self.checkpoint.capture_post(mission.checkpoint_id)
            except Exception as exc:                  # noqa: BLE001 - nothing has changed yet: the operator can try again
                log.warning("mission %s: the post-change capture failed", mission_id, exc_info=exc)
                raise ResumeRefused(f"the post-change capture failed ({type(exc).__name__}); nothing was restored") from None
            if mission.status != "rollback_pending":
                transition(self.store, mission_id, "rollback_pending", actor)
            try:
                report = self.checkpoint.restore(mission.checkpoint_id, ack_drift=ack_drift, actor=actor)
            except Exception as exc:                  # noqa: BLE001 - a purged or corrupt checkpoint: end in a defined state
                return self._restore_raised(mission_id, exc)
            if report.ok:
                transition(self.store, mission_id, "rolled_back", actor)
        if self.ms.get_mission(mission_id).status == "rolled_back":
            with contextlib.suppress(Exception):
                recovery.close_open_intents(self.ms, mission_id, "rolled_back", actor=ACTOR)
        return report

    def _restore_raised(self, mission_id: str, exc: Exception) -> Any:
        """The restore itself raised (checkpoint not found or corrupt, say). The mission must not stay in `rollback_pending`, which
        an operator cannot leave except by a restore that will fail the same way: hand it over, as the tripwire's rollback does."""
        reason = (f"the checkpoint could not be restored ({type(exc).__name__}: {str(exc)[:120]}). The restore does not raise once it "
                  "has started writing, so nothing was written; retrying will fail the same way")
        log.warning("mission %s: %s", mission_id, reason, exc_info=exc)
        if self.tripwire is not None:
            return self.tripwire.hand_over_after_failed_rollback(mission_id, reason)
        from .checkpoint import RestoreReport
        transition(self.store, mission_id, "needs_operator", ACTOR)
        self._note(mission_id, {"rollback_incomplete": reason})
        return RestoreReport(status="failed", reason=reason)

    def _stop_session_for_restore(self, mission_id: str) -> None:
        run = self._runs.get(mission_id)
        if run is not None and run.phase == "running":
            raise ResumeRefused("the session is still running: cancel or take over first")
        cs = run.cs if run is not None else self._last_session(mission_id)
        if cs is not None and not self.containment.is_empty(cs):
            self.containment.kill(cs)
            if not self.containment.is_empty(cs):
                raise ResumeRefused("the session is not provably dead")
        if self.ms.live_session(mission_id) is not None and cs is None:
            raise ResumeRefused("a live session exists that cannot be located")

    @_per_mission
    def take_over(self, mission_id: str, actor: str) -> None:
        """Operator 'take over': stop the worker for good and hand the host to the operator (`needs_operator`, with a
        `takeover` event). Nothing is rolled back and nothing is verified."""
        self._require_enabled()
        actor = self._operator(actor)
        mission = self._repair_mission(mission_id, "paused_tripwire", "executing", "recovery_required")
        self.ms.append_event(mission_id, "takeover", actor, {"event": "mission.takeover", "from": mission.status})
        run = self._runs.get(mission_id)
        if run is not None:
            self._kill_and_advance(run, "takeover")
        elif not self._adopt(mission, "takeover"):
            self._settle_repair(self.ms.get_mission(mission_id), "takeover", None)

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
        self._drain_hook_log(run, final=True)          # what the dead session's hooks recorded last, before anything is judged
        self._capture_post(run.mission_id)             # the session is provably dead: only now is the post-change manifest honest
        if run.cs is not None:
            self.containment.cleanup(run.cs)
        self._runs.pop(run.mission_id, None)
        self._reap_session(run)
        self._settle(run)

    def _capture_post(self, mission_id: str) -> None:
        mission = self.ms.get_mission(mission_id)
        if mission.tier != "repair" or not mission.checkpoint_id or self.checkpoint is None:
            return
        try:
            self.checkpoint.capture_post(mission.checkpoint_id)
        except Exception as exc:                       # noqa: BLE001 - the restore refuses without it and says so
            self._note(mission_id, {"capture_post_failed": type(exc).__name__})

    def _reap_session(self, run: _Run) -> None:
        """The session is provably dead and its cgroup is gone: stop its socket, remove its workspace and release its
        uid. A uid is released only when no process runs as it; otherwise it stays held and `sweep_uids` retries."""
        if run.stop_socket is not None:
            with contextlib.suppress(Exception):
                run.stop_socket()
            run.stop_socket = None
        if run.hook_dir is not None:
            shutil.rmtree(run.hook_dir, ignore_errors=True)
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
                if not self._clear_workspace(row["id"], cs.session_uid):
                    continue
                freed += int(self.ms.release_session_uid(cs.session_id))
            except Exception as exc:
                log.warning("could not reap session %s: %s", row["id"], type(exc).__name__)
        return freed

    def _clear_workspace(self, session_id: str, uid: int) -> bool:
        """True when the dead session's workspace is gone and no process runs as its uid, so the uid may be released.

        A session can leave directories Core cannot remove (the agent CLI makes its cache directory 0700, owned by the
        session uid). Core never gets more power to delete them; instead the uid that owns them deletes them. A short cleanup
        session, same uid, is launched from an empty scratch directory (the launcher refuses a workspace holding anything
        but regular files, so the old one cannot be its cwd), restores the owner's access, then runs
        `find <workspace> -mindepth 1 -delete`. Core then removes the empty directory. The uid stays held until all of that
        has happened. Each sweep takes one step.
        """
        if not SESSION_ID.fullmatch(session_id):
            return False                                    # never build a path or a command from anything but a session id
        workspace = self.workspace_dir / session_id
        cleaner_id = cleanup_id(session_id)
        cleaner = ContainedSession(cleaner_id, "local_cgroup", self.containment.cgroup_path(cleaner_id), uid)
        scratch = self.workspace_dir / cleaner_id
        entry = self._cleaners.get(cleaner_id)
        launched_at = entry[0] if entry else None
        try:
            if launched_at is not None:
                if not self.containment.is_empty(cleaner):
                    if float(self.clock()) - launched_at > CLEANUP_WALL_S:
                        log.warning("cleanup session %s is overdue: killing it", cleaner_id)
                        self._cleanup_retry_at[session_id] = float(self.clock()) + CLEANUP_RETRY_S
                        self.containment.kill(cleaner)
                    return False
                self.containment.cleanup(cleaner)
                self._cleaners.pop(cleaner_id, None)
            elif session_id not in self._cleanup_leftovers_reaped:
                # Nothing of ours runs it, but a previous Core may have left its cgroup: once per session, remove what is provably empty.
                self._cleanup_leftovers_reaped.add(session_id)
                with contextlib.suppress(Exception):
                    self.containment.kill(cleaner)
                    self.containment.cleanup(cleaner)
        except Exception as exc:
            log.warning("cleanup session %s could not be stopped or removed yet: %s", cleaner_id, type(exc).__name__)
            return False
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        if self.containment.uid_in_use(uid):
            return False
        shutil.rmtree(workspace, ignore_errors=True)
        if not workspace.exists():
            self._cleanup_retry_at.pop(session_id, None)
            return True
        now = float(self.clock())
        if launched_at is not None:
            log.warning("workspace %s survived its cleanup session: uid %s stays held", workspace, uid)
            self._cleanup_retry_at[session_id] = now + CLEANUP_RETRY_S
            return False
        if self._shutting_down or now < self._cleanup_retry_at.get(session_id, 0.0):
            return False
        if not self._launcher_proven():
            return False                                    # no session of any kind starts on a host whose preflight has not passed
        # Like every session start, the final shutdown check, the launch and the registration happen under the lifecycle lock that
        # `begin_shutdown` takes: a cleanup session either is registered before `shutdown()` looks, or is never launched.
        with self._lifecycle:
            if self._shutting_down:
                return False
            try:
                scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
                spec = LaunchSpec(uid=uid, cwd=str(scratch), argv=(CLEANUP_SH, "-c", CLEANUP_SCRIPT, "sh", str(workspace)),
                                  env={"PATH": "/usr/bin:/bin"}, token=secrets.token_urlsafe(32), sock_dir=False)
                self.containment.launch(cleaner_id, spec)
                self._cleaners[cleaner_id] = (now, uid)
            except Exception as exc:
                self._cleanup_retry_at[session_id] = now + CLEANUP_RETRY_S
                shutil.rmtree(scratch, ignore_errors=True)
                log.warning("could not start the cleanup session for %s: %s", session_id, type(exc).__name__)
        return False

    def _settle(self, run: _Run) -> None:
        reason = run.reason or "crash"
        mission = self.ms.get_mission(run.mission_id)
        if mission.tier in ("observe", "propose"):
            # The session is dead and an observe/propose session could not have changed anything: the intent is closed.
            with contextlib.suppress(Exception):
                recovery.close_open_intents(self.ms, mission.id, "not_applied", actor=ACTOR)
        if mission.status in TERMINAL:
            return
        live = self.ms.live_session(mission.id)
        if live is not None and live["id"] != run.session_id:
            # A replacement session is live and not ours: terminalizing would release it unmanaged.
            self._note(mission.id, {"stale_run_left_alone": run.session_id})
            if mission.cancel_requested_at is not None or reason == "cancel":
                self._adopt(mission, "cancel")           # a cancel must still reach the replacement
            return
        if mission.tier == "repair":
            return self._settle_repair(mission, reason, run.result)
        if mission.status == "verifying" and reason == "result":
            self.verify_and_close(mission.id)            # the session is dead: Core's own verification decides
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

    def _settle_repair(self, mission: Mission, reason: str, result: AgentResult | None) -> None:
        """The end of a Repair session, decided from the store and the host, never from the runtime. A result moves the mission
        to verification (the agent's report decides nothing). A cancel cancels. A take-over hands the host to the operator.
        Anything else (crash, budget, lease loss, restart) is classified from host state and is NEVER replayed: there is no
        automatic restart of a mutating session."""
        if reason == "cancel" or mission.cancel_requested_at is not None:
            return self._settle_repair_cancel(mission)
        if reason == "takeover":
            try:
                transition(self.store, mission.id, "needs_operator", ACTOR)
            except IllegalTransition as exc:
                self._note(mission.id, {"transition_refused": "needs_operator", "why": str(exc)[:200]})
            return
        if mission.status == "verifying":
            self.verify_and_close(mission.id)            # a restart lands here: verification is read-only, so run it again
            return
        if mission.status != "executing":
            # paused by a tripwire, or someone else already moved it: the session is dead and cleaned up; leave the state
            self._note(mission.id, {"session_ended": reason, "left_in": mission.status})
            return
        if reason == "result" and result is not None:
            try:
                transition(self.store, mission.id, "verifying", ACTOR)
            except IllegalTransition as exc:
                self._note(mission.id, {"transition_refused": "verifying", "why": str(exc)[:200]})
                return
            self.verify_and_close(mission.id)
            return
        self._classify_and_apply(mission, reason)

    def _settle_repair_cancel(self, mission: Mission) -> None:
        """A cancel of a Repair mission whose session may have changed the host. The host is classified FIRST: unchanged from
        the checkpoint means `cancelled`; anything else (changed, unreadable, no manifest) means `needs_operator` with the cancel
        recorded and a rollback offered. A mission is never closed `cancelled` over a half-applied host."""
        if mission.status in TERMINAL:
            return
        unchanged = not mission.checkpoint_id          # no checkpoint: no session was ever allowed to start, nothing was applied
        if mission.checkpoint_id and self.checkpoint is not None:
            try:
                manifest = self.checkpoint.manifest(mission.checkpoint_id)
                unchanged = recovery.matches_checkpoint(recovery.collect_host_state(self.checkpoint, manifest), manifest)
            except Exception as exc:                   # noqa: BLE001 - an unreadable host is not "unchanged"
                self._note(mission.id, {"host_state_unreadable": type(exc).__name__})
                unchanged = False
        if unchanged:
            return self._settle_without_session(mission.id, "cancel", _force=True)
        if mission.id in self._cancel_held:
            return
        self._cancel_held.add(mission.id)
        self.ms.set_outcome(mission.id, mission.outcome, "partially_applied", ACTOR)
        self.ms.append_event(mission.id, "decision", ACTOR, {
            "event": "cancel.held", "reason": "the checkpointed files or services differ from the checkpoint, so the mission is not closed as cancelled",
            "offers": ["rollback", "take_over"], "cancel_requested_by": mission.cancel_requested_by})
        if mission.status not in ("needs_operator", "recovery_required", "rollback_pending"):
            try:
                transition(self.store, mission.id, "needs_operator", ACTOR)
            except IllegalTransition as exc:
                self._note(mission.id, {"transition_refused": "needs_operator", "why": str(exc)[:200]})

    def _finish_pending_repair_cancels(self) -> None:
        """A cancel asked for through a surface that cannot judge the host (MissionService) is finished here, the same way."""
        for status in ("executing", "paused_tripwire", "verifying", "needs_operator", "recovery_required", "rollback_pending"):
            for mission in self.ms.list_missions(status):
                if (mission.tier == "repair" and mission.cancel_requested_at is not None and mission.id not in self._runs
                        and self.ms.live_session(mission.id) is None):
                    with contextlib.suppress(Exception):
                        with self._lock_for(mission.id):
                            self._settle_repair_cancel(self.ms.get_mission(mission.id))

    def _classify_and_apply(self, mission: Mission, reason: str) -> None:
        manifest = host = None
        if mission.checkpoint_id and self.checkpoint is not None:
            try:
                manifest = self.checkpoint.manifest(mission.checkpoint_id)
                host = recovery.collect_host_state(self.checkpoint, manifest)
            except Exception as exc:                   # noqa: BLE001 - an unreadable host is classified as such
                self._note(mission.id, {"host_state_unreadable": type(exc).__name__})
                manifest = host = None
        c = recovery.classify(mission, host, manifest, self.ms.events(mission.id))
        self._note(mission.id, {"session_ended": reason, "classified": c.state})
        final = recovery.apply_classification(self.ms, mission.id, c, actor=ACTOR)
        if final == "verifying":
            self.verify_and_close(mission.id)

    def _settle_without_session(self, mission_id: str, reason: str, result: AgentResult | None = None,
                                _force: bool = False) -> None:
        self._clear_waiting(mission_id)                   # a settled mission keeps no wait state, so a later wait starts afresh
        mission = self.ms.get_mission(mission_id)
        if (not _force and mission.tier == "repair" and mission.checkpoint_id
                and (reason == "cancel" or mission.cancel_requested_at is not None)):
            return self._settle_repair_cancel(mission)    # the host is judged before a Repair mission is closed as cancelled
        if reason == "cancel" or mission.cancel_requested_at is not None:
            target = "cancelled"
        elif reason == "result" and result is not None:
            # An agent's say-so never completes a mission (invariant I1): the operator, or a later slice's
            # Core-run verification, decides. Observe and propose both land in awaiting_decision.
            target = "awaiting_decision"
        else:
            target = "needs_operator"
        # A cancel is the requesting operator's act (and leaving needs_operator is reserved to an operator).
        actor = mission.cancel_requested_by if target == "cancelled" and mission.cancel_requested_by else ACTOR
        try:
            transition(self.store, mission_id, target, actor)
        except IllegalTransition as exc:
            self._note(mission_id, {"transition_refused": target, "why": str(exc)[:200]})

    def verify_and_close(self, mission_id: str, *, wait: bool = False) -> str:
        """A mutating mission's session has ended and the mission is `verifying`: hand it to the verification worker
        (a small bounded pool, NOT the poll thread and NOT under the mission lock) and return at once. The mission stays
        `verifying` while it runs, so cancel and the tripwire freeze stay responsive. `wait=True` blocks for the result
        (tests, tools). One verification per mission at a time; a second request joins the first."""
        self._require_enabled()
        if self.ms.get_mission(mission_id).status != "verifying":
            return self.ms.get_mission(mission_id).status
        with self._verify_guard:
            if self._verify_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._verify_pool = ThreadPoolExecutor(max_workers=VERIFY_WORKERS, thread_name_prefix="pe-verify")
            future = self._verifying.get(mission_id)
            if future is None or future.done():
                future = self._verify_pool.submit(self._verify_job, mission_id)
                self._verifying[mission_id] = future
        return future.result() if wait else "verifying"

    def _verify_job(self, mission_id: str) -> str:
        """Runs on a verification worker. The verifier itself runs WITHOUT the mission lock; the lock is taken only to
        re-read the mission and take the one transition its result allows. `completed` is reachable only through a passed
        result for the current grant hash (the store enforces that again, and a repeat run finds the mission already
        moved on and does nothing, so a restart or a duplicate job cannot complete twice). The agent's report is never
        an input."""
        mission = self.ms.get_mission(mission_id)
        if mission.status != "verifying":
            return mission.status
        target, flags, summary = "needs_operator", None, {"event": "verify.unavailable"}
        if self.verifier is not None:
            try:
                result = self.verifier.run(mission)
                summary = {"event": "verify.closed", "status": result.status, "reason": result.reason,
                           "failed_checks": [c.id for c in result.checks if not c.ok][:20]}
                if result.status == "passed":
                    target = "completed"
                else:
                    flags = "partially_applied"
                    if mission.grant().on_verify_fail == "rollback" and mission.checkpoint_id:
                        target = "rollback_pending"
            except Exception as exc:                      # noqa: BLE001 - a verifier that breaks is a failed verification
                log.exception("verifier for %s raised", mission_id)
                summary = {"event": "verify.error", "error": type(exc).__name__}
                flags = "partially_applied"
        with self._lock_for(mission_id):
            if self._shutting_down:
                # The daemon is stopping: this result is dropped (nothing committed), the mission stays `verifying`
                # and the restart re-classifies it and verifies again.
                self._note(mission_id, {"verify_dropped": "shutdown"})
                return "verifying"
            current = self.ms.get_mission(mission_id)
            if current.status != "verifying" or current.grant_hash != mission.grant_hash:
                # Cancelled, paused, rolled back or re-granted while the verification ran: that decision stands, and
                # this result decides nothing and sets no outcome.
                self._note(mission_id, {"verify_superseded": current.status})
                return current.status
            self._note(mission_id, summary)
            for attempt in (target, "needs_operator"):
                try:
                    transition(self.store, mission_id, attempt, ACTOR)
                    break
                except IllegalTransition as exc:
                    self._note(mission_id, {"transition_refused": attempt, "why": str(exc)[:200]})
                    if self.ms.get_mission(mission_id).status != "verifying":
                        break                              # something else moved it (a cancel): leave it alone
            final = self.ms.get_mission(mission_id).status
            if final in ("completed", "needs_operator", "rollback_pending"):
                # The label is the state the mission actually ended in, never the one we intended.
                if final == "completed":
                    recovery.close_open_intents(self.ms, mission_id, "applied", actor=ACTOR)
                elif flags is None:
                    flags = "partially_applied"
                self.ms.set_outcome(mission_id, final, None if final == "completed" else flags, ACTOR)
            return final

    # ── the daemon's loop and shutdown ──────────────────────────────────────
    def tick(self) -> None:
        """One turn of the daemon's poll thread: start queued Observe/Propose missions, then advance every
        running session. One misbehaving mission never stops the others."""
        if not self.enabled:
            return
        if self._shutting_down:
            with contextlib.suppress(Exception):
                self.poll_all()                  # sessions are still advanced (and killed) but nothing new starts
            return
        ready = self.launcher_ready()            # proposal-only: queued missions wait, nothing is started or retried
        for mission in self.ms.list_missions("queued"):
            if mission.tier not in ("observe", "propose") or mission.id in self._runs:
                continue
            if mission.cancel_requested_at is not None:
                with contextlib.suppress(Exception):
                    self._settle_without_session(mission.id, "cancel")
                continue
            if not ready or self._defer_pending(mission.id):
                continue
            try:
                self.start(mission.id)
                self._start_failures.pop(mission.id, None)
                self._clear_waiting(mission.id)
            except (StartDeferred, ProposalOnly) as exc:
                self._start_waits(mission.id, exc)
            except Exception as exc:
                self._start_failed(mission.id, exc)
        if self.mutating_ready:
            self._start_approved_repairs(ready)
            with contextlib.suppress(Exception):
                self._finish_pending_repair_cancels()
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

    def _start_approved_repairs(self, ready: bool) -> None:
        """Repair missions wait in `awaiting_decision` for approval. Each tick, those whose execution predicate holds are started.
        A start that cannot happen yet (the monitor or a side door not ready) just waits and is noted once per reason; any
        other refusal is a failed start and, after START_ATTEMPTS, hands the mission to the operator."""
        if self._shutting_down:
            return
        for mission in self.ms.list_missions("awaiting_decision"):
            if mission.tier != "repair" or mission.id in self._runs:
                continue
            if mission.cancel_requested_at is not None:
                with contextlib.suppress(Exception):
                    self._settle_without_session(mission.id, "cancel")
                continue
            if (not ready or self._defer_pending(mission.id)
                    or not approvals.execution_predicate(self.store, mission.id).ok):
                continue
            try:
                self.start(mission.id)
                self._start_failures.pop(mission.id, None)
                self._clear_waiting(mission.id)
            except (StartDeferred, ProposalOnly) as exc:
                self._start_waits(mission.id, exc)
            except RepairNotReady as exc:
                self._wait_noted(mission.id, str(exc))
            except Exception as exc:                  # noqa: BLE001 - one mission's start never stops the others
                self._start_failed(mission.id, exc)

    def _clear_waiting(self, mission_id: str) -> None:
        for table in (self._waiting, self._deferred_since, self._deferred_retry):
            table.pop(mission_id, None)

    def _defer_pending(self, mission_id: str) -> bool:
        return float(self.clock()) < self._deferred_retry.get(mission_id, 0.0)

    def _start_waits(self, mission_id: str, exc: Exception) -> None:
        """A start that must wait (a lease, a uid, a live session, the launcher proof): not a failed start. The reason is
        recorded once, the retry is paced, and a wait that outlasts START_DEFER_MAX_S is handed to the operator."""
        now = float(self.clock())
        since = self._deferred_since.setdefault(mission_id, now)
        self._wait_noted(mission_id, str(exc))
        self._deferred_retry[mission_id] = now + START_DEFER_RETRY_S
        if now - since >= START_DEFER_MAX_S and mission_id not in self._runs:
            self._note(mission_id, {"start_wait_expired": str(exc)[:200], "waited_s": int(now - since)})
            self._clear_waiting(mission_id)
            self._settle_without_session(mission_id, "start_failed")

    def _wait_noted(self, mission_id: str, why: str) -> None:
        if self._waiting.get(mission_id) != why:
            self._waiting[mission_id] = why
            self._note(mission_id, {"start_waiting": why[:300]})

    def _start_failed(self, mission_id: str, exc: Exception) -> None:
        self._deferred_since.pop(mission_id, None)         # a start that failed outright is not a continuing wait
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
        with self._lifecycle:                       # a start or cleanup launch already inside its launch registers first
            self._shutting_down = True              # in-flight verification jobs drop their result under the lock
        if self._verify_pool is not None:
            self._verify_pool.shutdown(wait=False, cancel_futures=True)     # a verification is re-run after restart
            deadline = time.monotonic() + VERIFY_SHUTDOWN_WAIT_S
            for future in list(self._verifying.values()):
                with contextlib.suppress(Exception):
                    future.result(max(0.0, deadline - time.monotonic()))
        for run in list(self._runs.values()):
            self._shutdown_one(run)
        self._stop_cleaners()

    def _stop_cleaners(self) -> None:
        """Cleanup sessions are sessions too: none outlives the daemon. A workspace left over is swept after the next start."""
        for cleaner_id, (_, uid) in list(self._cleaners.items()):
            cleaner = ContainedSession(cleaner_id, "local_cgroup", self.containment.cgroup_path(cleaner_id), uid)
            try:
                self.containment.kill(cleaner)
                if self.containment.is_empty(cleaner):
                    self.containment.cleanup(cleaner)
                    shutil.rmtree(self.workspace_dir / cleaner_id, ignore_errors=True)
                    self._cleaners.pop(cleaner_id, None)
            except Exception:
                log.exception("could not stop cleanup session %s", cleaner_id)

    def _shutdown_one(self, run: _Run) -> None:
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
            if mission.tier == "repair":
                self._release_prestart_leases(mission)
                self._recover_repair(mission)
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

    def _release_prestart_leases(self, mission: Mission) -> None:
        """A start takes the grant's leases before its checkpoint (`_acquire_early`). If Core died in that window the mission
        still waits for its start and holds leases that no session will ever heartbeat. With no session ever issued for it,
        nothing could be running under them, so they are released (recorded as proven dead for that reason)."""
        if mission.status not in ("awaiting_decision", "blocked") or mission.id in self._runs:
            return
        try:
            if self._attempts(mission.id) == 0:
                leases.release_after_proven_dead(self.store, mission.id, "no session was ever issued: the leases were a pre-start reservation")
        except Exception:                                  # noqa: BLE001 - recovery of one mission never blocks the others
            log.exception("could not release the pre-start leases of %s", mission.id)

    def _recover_repair(self, mission: Mission) -> None:
        """A Repair mission found in flight at startup. Nothing is replayed and nothing restarts by itself:
        - paused_tripwire: the monitor's own `recover()` re-freezes it; the supervisor leaves it alone;
        - executing: whatever its last session left is killed and, once provably empty, the end is classified from host state;
        - verifying: a leftover session is killed, then verification (read-only) runs again;
        - needs_operator / recovery_required / rollback_pending: a leftover session is killed, the state is the operator's;
        - awaiting_decision / blocked: no session exists, nothing to do."""
        if not self.mutating_ready or mission.status in ("awaiting_decision", "blocked", "paused_tripwire"):
            return
        reason = "cancel" if mission.cancel_requested_at is not None else "recover"
        try:
            with self._lock_for(mission.id):
                adopted = self._adopt(mission, reason, only_if_live=mission.status != "executing")
                if not adopted:
                    if mission.status == "executing":
                        self._settle_repair(mission, reason, None)     # no session left at all: classify from the host
                    elif mission.status == "verifying" and reason != "cancel":
                        self.verify_and_close(mission.id)
        except Exception as exc:                       # noqa: BLE001 - one mission must not block the others
            log.warning("recovering mission %s failed: %s", mission.id, type(exc).__name__)
            self._note(mission.id, {"recover_failed": type(exc).__name__})

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
    def request_decision(self, session: SessionRow, summary: str, options: list[str]) -> dict:
        """`mission.request_decision`: the worker asks the operator something. It records a CARD (a note event the surfaces
        list) and does nothing else: no state changes, no approval row, no grant edit. A bounded number per session."""
        run = self._live_run(session)
        body = {"event": "decision.requested", "summary": summary[:MAX_DECISION_TEXT],
                "options": [str(o)[:MAX_OPTION_TEXT] for o in options[:MAX_OPTIONS]], "session": session.id}
        # The quota counts rows written under the card's own actor, which `post_event` can never write (a worker's own notes are
        # actor `worker`): a note that merely says "decision.requested" cannot use the quota up. The count and the insert are
        # one transaction, by SQL, with no pass over the mission's events.
        now = int(self.store._clock())
        with self.store._write() as conn:
            asked = conn.execute("SELECT COUNT(*) FROM mission_events WHERE mission_id=? AND kind='note' AND actor=?",
                                 (run.mission_id, DECISION_ACTOR)).fetchone()[0]
            if asked >= MAX_DECISION_REQUESTS:
                raise RpcError("Refused", "unauthorized")
            insert_event(conn, run.mission_id, "note", DECISION_ACTOR, body, now)
        run.output_bytes += len(json.dumps(body))
        return {"recorded": True}

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
        if self.docker_proxy is not None:
            self.docker_proxy.pause(session.mission_id)
        if self.model_broker is not None:
            self.model_broker.pause(session.mission_id)

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


__all__ = ["ExternalRecord", "MissionsDisabled", "ProposalOnly", "RepairNotReady", "RepairRefused", "ResumeRefused",
           "SocketProvider", "StartDeferred", "Supervisor",
           "UnsupportedTier", "default_incident_evidence", "default_memory_cards"]
