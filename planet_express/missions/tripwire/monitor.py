"""The tripwire monitor (contract 9, brief 6.2): continuous state diff against the grant while a mission executes.

    scan (own thread, every `interval_s`; each signal 3 s, the whole scan 10 s)
      ├─ any signal failed, timed out or missing        ──► pause (fail closed)
      ├─ baseline unreadable / grant hash changed       ──► pause
      └─ diff vs the baseline captured at executing start
            protected / out_of_scope change             ──► pause
            unexpected_in_scope over budget             ──► pause
            anything else                               ──► keep going

    pause = freeze the session cgroup ─► close the capability endpoints ─► checkpoint.capture_post() ─► ONE durable
            transaction (mission -> paused_tripwire + the `tripwire` event carrying the redacted, bounded diff and the
            operator card). Worst case change -> recorded event: interval + deadline + freeze + write (budget 30 s).

The baseline is the live capture taken at executing start (and checked against the checkpoint's immutable manifest); it is
never advanced to "whatever was seen last". It is written to disk before the first scan so a Core restart keeps it. A
paused mission stays paused across a restart: `recover()` freezes its session again, and a pause that was begun but not
recorded (the `intent` file) is completed. A monitor crash pauses every executing mission and the monitor keeps running.
Nothing here interprets host text: log lines, labels and file contents only ever reach a hash or a bounded, sanitised detail.
"""

import fnmatch
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

from planet_express.core.redact import redact
from planet_express.missions.containment import ContainedSession, SessionContainment
from planet_express.missions.events import insert_event
from planet_express.missions.models import Grant, Mission
from planet_express.missions.resources import BadResource, canon, split
from planet_express.missions.state import (
    IllegalTransition,
    transition,
    transition_in_txn,
)
from planet_express.missions.store import MissionStore, grant_resources

from .diff import Change, DiffContext, clean, diff_snapshots
from .signals import Scanner, ScanResult, blind_resources

log = logging.getLogger(__name__)

ACTOR = "tripwire"
CREATE_GRACE_S = 120      # a container create the proxy announced but has not finished counts as in flight this long
STATE_V = 1
MAX_CHANGES = 30
MAX_STATE_BYTES = 8 * 1024 * 1024
PAUSE_SLA_S = 30
WRITE_RECENT_SLACK_S = 5.0           # a proxy write this recent can still be settling Docker's firewall rules
FIREWALL_EXCUSE_MAX_S = 120          # the longest proxy writes may excuse a firewall change, however many there are
_MISSION_ID = re.compile(r"^M-\d{1,12}$")
BASE_OPTIONS = ("resume", "roll_back", "take_over", "cancel")
NO_ROLLBACK_OPTIONS = ("take_over", "cancel")


class BlindBaseline(RuntimeError):
    """The baseline has resources Core can only stat, not read, and the operator has not accepted that."""


class RollbackRefused(RuntimeError):
    """The operator asked for a rollback that cannot be done safely right now."""


class TripwireStore:
    """Durable per-mission state (baseline, budget counters, pause intent): atomic, 0600, in a 0700 directory."""

    def __init__(self, root: str):
        self.root = root

    def _dir(self, mission_id: str) -> str:
        if not _MISSION_ID.match(mission_id):
            raise ValueError("bad mission id")
        return os.path.join(self.root, mission_id)

    def write(self, mission_id: str, name: str, value: dict[str, Any]) -> None:
        directory = self._dir(mission_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        data = json.dumps(value, sort_keys=True).encode()
        if len(data) > MAX_STATE_BYTES:
            raise ValueError("tripwire state too large")
        tmp = os.path.join(directory, f".{name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(directory, name))
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def read(self, mission_id: str, name: str) -> dict[str, Any] | None:
        path = os.path.join(self._dir(mission_id), name)
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(data) > MAX_STATE_BYTES:
            raise ValueError("tripwire state too large")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("tripwire state is not an object")
        return value

    def remove(self, mission_id: str, name: str) -> None:
        try:
            os.unlink(os.path.join(self._dir(mission_id), name))
        except FileNotFoundError:
            pass


def pause_card(ms: MissionStore, mission_id: str) -> dict[str, Any] | None:
    """The operator card payload of the mission's latest pause, from the durable event."""
    for event in reversed(ms.events(mission_id)):
        if event.kind == "tripwire":
            body = json.loads(event.body_json)
            if body.get("event") == "tripwire.paused":
                return body
    return None


class TripwireMonitor:
    def __init__(self, *, missions: MissionStore, containment: SessionContainment, checkpoint: Any, scanner: Scanner,
                 state_dir: str, enabled: Callable[[], bool] | bool = False, stacks_root: str | None = None,
                 interval_s: float = 15.0, investigating_interval_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 revoke_endpoints: Callable[[str], None] | None = None, crash_backoff_s: float = 5.0,
                 accept_blind: Collection[str] = ()):
        self.ms = missions
        self.containment = containment
        self.checkpoint = checkpoint
        self.scanner = scanner
        self.state = TripwireStore(state_dir)
        self._vetted_volumes: dict[str, set[str]] = {}
        self._proxy_seen: dict[str, tuple] = {}
        self._enabled = enabled if callable(enabled) else (lambda: bool(enabled))
        self.stacks_root = stacks_root
        self.interval_s = interval_s
        self.investigating_interval_s = investigating_interval_s
        self.clock = clock
        self.revoke_endpoints = revoke_endpoints or (lambda mission_id: None)
        self.crash_backoff_s = crash_backoff_s
        self.accept_blind = frozenset(accept_blind)       # resource ids the operator accepts being unable to read
        self._stop = threading.Event()
        self._quiesced = False
        self._pause_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_scan_at: float | None = None
        self.scans = 0

    # ── thread ──────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self._enabled())

    def start(self) -> threading.Thread | None:
        """Start the monitor thread. With missions disabled nothing starts and None is returned."""
        if not self.enabled:
            return None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="tripwire-monitor")
            self._thread.start()
            return self._thread

    @property
    def running(self) -> bool:
        """The monitor thread is alive and not stopping. A Repair launch is refused unless this is True (slice 7a)."""
        thread = self._thread
        return bool(self.enabled and thread is not None and thread.is_alive() and not self._stop.is_set())

    def quiesce(self) -> None:
        """Shutdown has begun and sessions are being killed: stop pausing/freezing them (a frozen cgroup cannot be killed
        cleanly). Scans continue; the monitor itself is stopped only after the sessions are proven gone."""
        with self._pause_lock:               # waits out a pause that is already freezing, so none starts after this returns
            self._quiesced = True

    def unquiesce(self) -> None:
        self._quiesced = False

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=30)

    def ensure_running(self) -> bool:
        """The daemon's tick calls this: a monitor thread that died (anything, including BaseException) pauses every
        executing mission and is restarted. Returns True when it had to act."""
        if not self.enabled or self._stop.is_set():
            return False
        thread = self._thread
        if thread is not None and thread.is_alive():
            return False
        if thread is not None:
            self._pause_all("monitor_died")
        self.start()
        return thread is not None

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = self.interval_s
            try:
                if self.enabled:
                    wait = self.tick()
            except Exception as exc:
                log.exception("tripwire monitor crashed")
                self._pause_all("monitor_crashed", f"{type(exc).__name__}")
                wait = self.crash_backoff_s
            self._stop.wait(wait)

    def tick(self) -> float:
        """One turn: scan if any mission is executing. Returns how long to wait before the next one."""
        executing = self.ms.list_missions("executing")
        if executing:
            self.scan_once(executing)
            return self.interval_s
        return self.investigating_interval_s if self.ms.list_missions("investigating") else self.interval_s

    # ── a scan ──────────────────────────────────────────────────────────────
    @staticmethod
    def _excluded_files(missions: list[Mission]) -> list[tuple[str, str]]:
        """(resource id, path) of every local path or mount a watched grant excludes, so a change under it is SEEN (the
        protected set alone does not name it). Hashed on top of the protected files; each mission's baseline holds its own."""
        out: dict[str, str] = {}
        for mission in missions:
            try:
                for rid in mission.grant().enforced_exclusions():
                    host, kind, name = split(rid)
                    if kind in ("path", "mount") and host == "local":
                        out[rid] = name
            except Exception:                      # noqa: BLE001 - an unreadable grant is judged (and paused) elsewhere
                continue
        return sorted(out.items())

    def scan_once(self, missions: list[Mission] | None = None) -> dict[str, str]:
        """Scan once and judge every executing mission. Returns {mission id: 'ok' | the pause cause}."""
        missions = self.ms.list_missions("executing") if missions is None else missions
        if not missions:
            return {}
        proxy_cursors = {mission.id: self.ms.latest_seq(mission.id) for mission in missions}
        result = self.scanner.scan(self._excluded_files(missions))
        self.last_scan_at = self.clock()
        self.scans += 1
        out: dict[str, str] = {}
        for mission in missions:
            try:
                overlapped = self.ms.proxy_write_since(mission.id, proxy_cursors[mission.id])
                out[mission.id] = self._judge(mission, result, proxy_write_overlapped=overlapped)
            except Exception as exc:         # noqa: BLE001 - one mission's failure pauses that mission only
                log.warning("tripwire could not judge %s: %s", mission.id, type(exc).__name__)
                self._pause(mission, "monitor_error", [], {"error": type(exc).__name__}, result)
                out[mission.id] = "monitor_error"
        return out

    def _proxy_state(self, mission_id: str) -> tuple[frozenset[str], tuple[tuple[str, tuple[str, ...]], ...]]:
        """(ids the proxy recorded creating for this mission, the (name, targets) of each create in flight). Read incrementally from the
        last seq seen (never the whole log per scan); an intent with no end after CREATE_GRACE_S stops counting."""
        seq, created, begun, ended = self._proxy_seen.get(mission_id, (0, set(), {}, set()))
        events, seq = self.ms.proxy_creates_since(mission_id, seq)
        for _ev_seq, at, body in events:
            kind, nonce = body.get("event"), body.get("nonce")
            if kind == "docker.write":
                # The proxy's own record of what it authorised for a create: the named volumes it vetted for this mission.
                if body.get("route") == "container.create" and isinstance(body.get("mounts"), list):
                    vetted = self._vetted_volumes.setdefault(mission_id, set())
                    for mount in body["mounts"]:
                        if isinstance(mount, dict) and mount.get("type") == "volume" and isinstance(mount.get("source"), str):
                            vetted.add(mount["source"])
                continue
            if kind == "docker.create_begin" and isinstance(nonce, str):
                targets = body.get("targets") if isinstance(body.get("targets"), list) else []
                begun[nonce] = (at, str(body.get("name") or ""), tuple(str(t) for t in targets))
            else:
                if kind == "docker.created" and isinstance(body.get("created_id"), str):
                    created.add(body["created_id"])
                if isinstance(nonce, str):
                    ended.add(nonce)
        now = int(self.ms.store._clock())
        for nonce in [n for n in begun if n in ended]:
            del begun[nonce]
        ended.intersection_update(begun)
        self._proxy_seen[mission_id] = (seq, created, begun, ended)
        pending = tuple((name, targets) for at, name, targets in begun.values() if now - at <= CREATE_GRACE_S)
        return frozenset(created), pending

    def _discovery_volumes(self) -> frozenset[str]:
        site = getattr(getattr(self.scanner, "env", None), "protected", None)
        try:
            return site.discovery_only_volumes() if site is not None else frozenset()
        except AttributeError:
            return frozenset()

    def _ctx(self, grant: Grant, mission_id: str | None = None) -> DiffContext:
        scope = tuple(canon(r) for r in grant_resources(grant))
        try:
            prot = grant.effective_protection()
        except BadResource:
            raise ValueError("grant.protected holds a malformed resource") from None
        created, pending = self._proxy_state(mission_id) if mission_id else (frozenset(), ())
        return DiffContext(scope=scope, protected=prot.protected, excluded=prot.excluded, stacks_root=self.stacks_root,
                           created=created, creates_in_flight=pending,
                           vetted_volumes=frozenset(self._vetted_volumes.get(mission_id, ())) if mission_id else frozenset(),
                           discovery_volumes=self._discovery_volumes())

    def _judge(self, mission: Mission, result: ScanResult, *, proxy_write_overlapped: bool = False) -> str:
        if not result.ok:
            return self._pause(mission, "scan_failed", [], result.errors, result)
        baseline = self._baseline_for(mission, result)
        if baseline is None:
            return "baseline_captured"
        if baseline == "blind":
            return self._pause(mission, "blind_baseline", [], {}, result)
        if baseline == "unreadable":
            return self._pause(mission, "baseline_unreadable", [], {}, result)
        if baseline["grant_hash"] != mission.grant_hash or mission.grant().hash() != mission.grant_hash:
            return self._pause(mission, "grant_changed", [], {}, result)
        grant = mission.grant()
        changes = diff_snapshots(baseline["snapshot"], result.snapshot, self._ctx(grant, mission.id))
        firewall = [c for c in changes if c.kind == "firewall_changed"]
        if self._firewall_excused(mission, proxy_write_overlapped, bool(firewall)):
            changes = [c for c in changes if c.kind != "firewall_changed"]
        immediate = [c for c in changes if c.verdict in ("protected", "out_of_scope")]
        counted = self._count(mission, grant, changes)
        if immediate:
            cause = "protected" if any(c.verdict == "protected" for c in immediate) else "out_of_scope"
            return self._pause(mission, cause, immediate + counted["new"], {}, result)
        if counted["over"]:
            return self._pause(mission, counted["over"], counted["new"], {}, result)
        return "ok"

    def _firewall_excused(self, mission: Mission, overlapped: bool, present: bool) -> bool:
        """Whether a firewall change is excused on this scan. Docker rewrites its rules for as long as an already-authorised
        proxy mutation runs (a container stop alone takes seconds), so a change that shows while a proxy write is unresolved
        or has just finished waits for it. The wait is capped from its first scan: continuous writes cannot hide a firewall
        change for longer than FIREWALL_EXCUSE_MAX_S. Every other change is judged on every scan."""
        try:
            held = self.state.read(mission.id, "firewall-excused.json")
        except (ValueError, OSError):
            held = None
        now = self.ms._now()
        if not present or not (overlapped or self.ms.proxy_write_active(
                mission.id, self.interval_s + WRITE_RECENT_SLACK_S, FIREWALL_EXCUSE_MAX_S)):
            if held is not None:
                self.state.remove(mission.id, "firewall-excused.json")
            return False
        if held is not None and now - int(held.get("last_seen", 0)) > 2 * self.interval_s + WRITE_RECENT_SLACK_S:
            held = None                                           # left over from before a pause or resume
        since = int(held["since"]) if held is not None else now
        self.state.write(mission.id, "firewall-excused.json", {"v": STATE_V, "since": since, "last_seen": now})
        return now - since < FIREWALL_EXCUSE_MAX_S

    def _count(self, mission: Mission, grant: Grant, changes: list[Change]) -> dict[str, Any]:
        """Distinct changes against the budgets. The baseline never moves, so the same difference shows on every scan:
        it is counted once, by its key, and the keys are kept on disk so a restart does not count it again."""
        seen = (self.state.read(mission.id, "budget.json") or {"v": STATE_V, "keys": {}})
        keys: dict[str, dict] = seen["keys"]
        new = []
        for change in changes:
            if change.key not in keys:
                keys[change.key] = {"verdict": change.verdict, "resource": change.resource}
                if change.verdict == "unexpected_in_scope":
                    new.append(change)
        unexpected = sum(1 for k in keys.values() if k["verdict"] == "unexpected_in_scope")
        over = None
        if unexpected > grant.effects.max_unexpected_in_scope:
            over = "unexpected_in_scope_budget"
        for pattern, budget in grant.effects.per_resource.items():
            hits = sum(1 for k in keys.values() if _matches(k["resource"], pattern))
            if hits > budget.changes:
                over = "per_resource_budget"
        self.state.write(mission.id, "budget.json", {"v": STATE_V, "keys": keys})
        if new:
            self.ms.append_event(mission.id, "tripwire", ACTOR, {
                "event": "tripwire.unexpected_in_scope", "changes": [c.as_dict() for c in new[:MAX_CHANGES]],
                "total_unexpected": unexpected, "budget": grant.effects.max_unexpected_in_scope})
        return {"new": new, "over": over}

    # ── baseline ────────────────────────────────────────────────────────────
    def begin(self, mission_id: str, *, keep_previous: bool = False) -> dict[str, Any]:
        """Capture the live baseline at executing start. The caller (the supervisor, before the session is launched)
        must see this succeed: a failed capture means the mission must not run. `keep_previous` (a resume) first sets the
        baseline that tripped aside, so a resume that then fails can put it back (`restore_previous_baseline`) and the
        trip evidence is not lost; `drop_previous_baseline` forgets it once the resume has held."""
        mission = self.ms.get_mission(mission_id)
        result = self.scanner.scan(self._excluded_files([mission]))
        if not result.ok:
            raise RuntimeError(f"cannot capture the tripwire baseline: {sorted(result.errors)}")
        if keep_previous:
            try:
                previous = self.state.read(mission_id, "baseline.json")
            except (ValueError, OSError):
                previous = None
            if previous is not None:
                self.state.write(mission_id, "baseline.prev.json", previous)
            else:
                # Nothing readable to set aside: a copy left by an earlier crashed resume must not be restored over this one.
                with contextlib.suppress(Exception):
                    self.state.remove(mission_id, "baseline.prev.json")
        return self._capture_baseline(mission, result)

    def restore_previous_baseline(self, mission_id: str) -> bool:
        """Put the baseline that was set aside by `begin(keep_previous=True)` back. False when there was none."""
        try:
            previous = self.state.read(mission_id, "baseline.prev.json")
        except (ValueError, OSError):
            return False
        if previous is None:
            return False
        self.state.write(mission_id, "baseline.json", previous)
        self.state.remove(mission_id, "baseline.prev.json")
        return True

    def drop_previous_baseline(self, mission_id: str) -> None:
        with contextlib.suppress(Exception):
            self.state.remove(mission_id, "baseline.prev.json")

    def _capture_baseline(self, mission: Mission, result: ScanResult) -> dict[str, Any]:
        drift = self._manifest_drift(mission, result.snapshot)
        blind = blind_resources(result.snapshot)
        unaccepted = [b for b in blind if b not in self.accept_blind]
        if unaccepted:
            # Fail closed: a watch that cannot read what it watches is not a watch. Fix the permissions (Core needs read
            # access to its protected paths) or accept the blindness by name in missions.tripwire_accept_blind.
            raise BlindBaseline(f"the tripwire cannot read: {', '.join(unaccepted[:6])}")
        record = {"v": STATE_V, "mission_id": mission.id, "grant_hash": mission.grant_hash,
                  "checkpoint_id": mission.checkpoint_id, "snapshot": result.snapshot,
                  "manifest_drift": drift, "blind": blind}
        self.state.write(mission.id, "baseline.json", record)
        self.ms.append_event(mission.id, "tripwire", ACTOR, {
            "event": "tripwire.baseline", "checkpoint": mission.checkpoint_id, "blind": blind,
            "manifest_drift": drift[:MAX_CHANGES],
            "counts": {k: (len(v) if hasattr(v, "__len__") else 1) for k, v in result.snapshot.items()}})
        return record

    def _manifest_drift(self, mission: Mission, snapshot: dict[str, Any]) -> list[str]:
        """Services whose live image or networks already differ from the checkpoint manifest at executing start."""
        if not mission.checkpoint_id:
            return []
        try:
            facts = self.checkpoint.manifest(mission.checkpoint_id)["facts"]["services"]
        except Exception:        # noqa: BLE001 - an unreadable manifest is reported by the checkpoint code, not here
            return ["manifest unreadable"]
        by_resource = {c["resource"]: c for c in snapshot.get("containers", {}).values()}
        drifted = []
        for service, fact in sorted(facts.items()):
            try:
                live = by_resource.get(canon(f"service:{service}"))
            except BadResource:
                continue
            if fact is None or live is None:
                if (fact is None) != (live is None):
                    drifted.append(clean(service, 120))
            elif fact["image_id"] != live["image"] or sorted(fact["networks"]) != sorted(live["networks"]):
                drifted.append(clean(service, 120))
        return drifted

    def _baseline_for(self, mission: Mission, result: ScanResult) -> dict | str | None:
        try:
            record = self.state.read(mission.id, "baseline.json")
        except (ValueError, OSError):
            return "unreadable"
        if record is not None:
            if record.get("v") != STATE_V or not isinstance(record.get("snapshot"), dict):
                return "unreadable"
            return record
        # No baseline yet. Before the session is live nothing can have changed anything, so take it now; once it
        # is live the baseline is missing for a reason nobody can vouch for: pause.
        session = self.ms.live_session(mission.id)
        if session is None or session["state"] == "launching":
            try:
                self._capture_baseline(mission, result)
            except BlindBaseline:
                return "blind"
            return None
        return "unreadable"

    # ── pause ───────────────────────────────────────────────────────────────
    def _session(self, mission_id: str) -> ContainedSession | None:
        with self.ms.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_sessions WHERE mission_id=? "
                               "ORDER BY issued_at DESC, rowid DESC LIMIT 1", (mission_id,)).fetchone()
        if row is None:
            return None
        return ContainedSession(row["id"], "local_cgroup", row["cgroup_path"], row["session_uid"])

    def pause_for_operator(self, mission_id: str, actor: str) -> bool:
        """The operator's word to stop: freeze the run exactly as a tripwire pause would (same freeze, same endpoint revocation, same
        post-change capture, same card), with cause `operator`. Only an executing mission can be paused; anything else returns False."""
        mission = self.ms.get_mission(mission_id)
        if mission.status != "executing":
            return False
        self._pause(mission, "operator", [], {"by": actor}, None)
        return self.ms.get_mission(mission_id).status == "paused_tripwire"

    def _pause_all(self, cause: str, detail: str = "") -> None:
        for mission in self.ms.list_missions("executing"):
            try:
                self._pause(mission, cause, [], {"detail": detail} if detail else {}, None)
            except Exception:
                log.exception("could not pause %s cleanly; killing its session", mission.id)
                cs = self._session(mission.id)
                if cs is not None:
                    try:
                        self.containment.kill(cs)
                    except Exception:
                        log.exception("could not kill the session of %s", mission.id)

    def _pause(self, mission: Mission, cause: str, changes: list[Change], extra: dict, result: ScanResult | None) -> str:
        with self._pause_lock:
            if self._quiesced:
                log.warning("tripwire quiesced for shutdown: not pausing %s (%s)", mission.id, cause)
                return cause
            return self._pause_locked(mission, cause, changes, extra, result)

    def _pause_locked(self, mission: Mission, cause: str, changes: list[Change], extra: dict, result: ScanResult | None) -> str:
        started = self.clock()
        try:
            self.state.write(mission.id, "intent.json", {"v": STATE_V, "cause": cause, "grant_hash": mission.grant_hash})
        except (OSError, ValueError):
            log.exception("could not write the pause intent for %s; pausing anyway", mission.id)   # freezing comes first
        frozen = self._freeze(mission.id)
        try:
            self.revoke_endpoints(mission.id)
        except Exception:
            log.exception("could not close the capability endpoints of %s", mission.id)
        rollback = self._capture_post(mission)
        options = list(BASE_OPTIONS if rollback["available"] else NO_ROLLBACK_OPTIONS)
        shown = changes[:MAX_CHANGES]
        body: dict[str, Any] = {
            "event": "tripwire.paused", "cause": cause, "frozen": frozen,
            "changes": [c.as_dict() for c in shown], "truncated": max(0, len(changes) - len(shown)),
            "errors": {clean(k, 40): clean(redact(str(v)), 160) for k, v in list(extra.items())[:16]},
            "rollback": rollback, "card": {"mission": mission.id, "options": options},
            "scan_errors": {clean(k, 40): clean(v, 160) for k, v in list((result.errors if result else {}).items())[:16]},
        }
        body["latency_s"] = round(self.clock() - started, 3)
        self._record(mission, body)
        self.state.remove(mission.id, "intent.json")
        return cause

    def _freeze(self, mission_id: str) -> bool:
        cs = self._session(mission_id)
        if cs is None:
            return False
        try:
            self.containment.freeze(cs)
            return True
        except Exception:
            log.exception("could not freeze the session of %s; killing it", mission_id)
            try:
                self.containment.kill(cs)
            except Exception:
                log.exception("could not kill the session of %s", mission_id)
            return False

    def _capture_post(self, mission: Mission) -> dict[str, Any]:
        if not mission.checkpoint_id:
            return {"available": False, "reason": "this mission has no checkpoint"}
        try:
            self.checkpoint.capture_post(mission.checkpoint_id)
        except Exception as exc:    # noqa: BLE001 - never offer a rollback the restore would refuse
            return {"available": False, "reason": f"post-change capture failed: {type(exc).__name__}"}
        return {"available": True, "checkpoint": mission.checkpoint_id}

    def _record(self, mission: Mission, body: dict[str, Any]) -> None:
        """Mission -> paused_tripwire and the event, in ONE transaction: the pause is recorded or it is not."""
        now = int(self.ms.store._clock())
        try:
            with self.ms.store._write() as conn:
                transition_in_txn(conn, mission.id, "paused_tripwire", ACTOR, now)
                insert_event(conn, mission.id, "tripwire", ACTOR, body, now)
        except IllegalTransition:
            # No longer executing (it finished, was cancelled or was already paused): keep the evidence anyway.
            self.ms.append_event(mission.id, "tripwire", ACTOR, {**body, "event": "tripwire.pause_not_applied"})

    # ── restart ─────────────────────────────────────────────────────────────
    def recover(self) -> list[str]:
        """At Core start: re-freeze every paused session, and finish a pause that was begun but never recorded.

        Per mission and isolated: one that cannot be re-frozen (and cannot be killed) or whose pause cannot be completed is
        marked `recovery_required` with an event and a log line, and the others are still recovered. Only a failure to list
        the missions at all raises: there is nothing to isolate then, and Core must not start unwatched."""
        acted: list[str] = []
        for mission in self.ms.list_missions("paused_tripwire"):
            try:
                cs = self._session(mission.id)
                if cs is not None:
                    try:
                        self.containment.freeze(cs)
                    except Exception:
                        log.exception("could not re-freeze %s; killing its session", mission.id)
                        self.containment.kill(cs)
                self.ms.append_event(mission.id, "tripwire", ACTOR, {"event": "tripwire.refrozen"})
                acted.append(mission.id)
            except Exception as exc:       # noqa: BLE001
                self._recovery_failed(mission.id, f"re-freezing failed ({type(exc).__name__})")
        for mission in self.ms.list_missions("executing"):
            try:
                try:
                    intent = self.state.read(mission.id, "intent.json")
                except (ValueError, OSError):
                    intent = {"cause": "intent_unreadable"}
                if intent is not None:
                    self._pause(mission, "recovered_pause", [], {"was": clean(intent.get("cause", "?"), 40)}, None)
                    acted.append(mission.id)
            except Exception as exc:       # noqa: BLE001
                self._recovery_failed(mission.id, f"completing the pause failed ({type(exc).__name__})")
        return acted

    def _recovery_failed(self, mission_id: str, reason: str) -> None:
        """A mission whose session could not be held at restart. Stop its session if at all possible, and hand it to the
        operator as `recovery_required`. Never raises: it is the last resort."""
        log.error("tripwire recovery of %s failed: %s", mission_id, reason)
        try:
            cs = self._session(mission_id)
            if cs is not None:
                try:
                    self.containment.kill(cs)
                except Exception:
                    log.exception("could not kill the session of %s", mission_id)
            body = {"event": "tripwire.recovery_failed", "reason": clean(reason, 200),
                    "card": {"mission": mission_id, "options": list(NO_ROLLBACK_OPTIONS)}}
            now = int(self.ms.store._clock())
            with self.ms.store._write() as conn:
                row = conn.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
                if row is not None and row["status"] not in ("recovery_required", "completed", "failed", "rolled_back", "cancelled"):
                    transition_in_txn(conn, mission_id, "recovery_required", ACTOR, now)
                insert_event(conn, mission_id, "tripwire", ACTOR, body, now)
        except Exception:
            log.exception("could not record the failed recovery of %s", mission_id)

    # ── operator action: roll back ──────────────────────────────────────────
    def roll_back(self, mission_id: str, actor: str) -> Any:
        """The operator's 'roll back' on a paused mission: kill the (frozen) session, wait until it is provably dead,
        take the post-change manifest again, then call the checkpoint restore. The restore's own refusals (drift,
        missing manifest) leave the mission with the operator; they are reported, never overridden here."""
        if not actor.startswith("operator:") or len(actor) <= 9:
            raise ValueError("a rollback is an operator decision: pass actor='operator:<name>'")
        mission = self.ms.get_mission(mission_id)
        if mission.status != "paused_tripwire":
            raise RollbackRefused(f"{mission_id} is {mission.status}, not paused_tripwire")
        card = pause_card(self.ms, mission_id)
        if not mission.checkpoint_id or not card or not card["rollback"].get("available"):
            raise RollbackRefused("rollback is not available for this pause")
        cs = self._session(mission_id)
        if cs is not None:
            try:
                self.containment.kill(cs)
                for _ in range(50):
                    if self.containment.is_empty(cs):
                        break
                    self.containment.kill(cs)
                    time.sleep(0.1)
                else:
                    raise RollbackRefused("the session is not provably dead")
            except RollbackRefused:
                raise
            except Exception as exc:        # noqa: BLE001 - the mission stays paused; the operator can try again
                raise RollbackRefused(f"the session could not be stopped ({type(exc).__name__})") from None
            try:
                self.containment.cleanup(cs)
            except Exception:               # noqa: BLE001 - an empty cgroup left behind is the reaper's to remove
                log.warning("could not remove the cgroup of %s", mission_id)
        # From here on every outcome ends in a DEFINED state: rolled_back on a clean restore, otherwise needs_operator
        # with the report and a card. Nothing is left in paused_tripwire half-done or in rollback_pending.
        try:
            self.checkpoint.capture_post(mission.checkpoint_id)
            transition(self.ms.store, mission_id, "rollback_pending", actor)
        except Exception as exc:            # noqa: BLE001
            return self._rollback_failed(mission_id, f"the post-change capture failed ({type(exc).__name__})", None)
        try:
            report = self.checkpoint.restore(mission.checkpoint_id, actor=actor)
        except Exception as exc:            # noqa: BLE001 - a restore that raised may have written some files
            return self._rollback_failed(mission_id, f"the restore raised {type(exc).__name__}", None)
        if report.ok:
            transition(self.ms.store, mission_id, "rolled_back", actor)
            return report
        self._rollback_failed(mission_id, report.reason or "the restore did not complete cleanly", report)
        return report

    def hand_over_after_failed_rollback(self, mission_id: str, reason: str, report: Any = None) -> Any:
        """For a rollback that was driven from outside the monitor (the supervisor's restore of a mission it already left)."""
        return self._rollback_failed(mission_id, reason, report)

    def _rollback_failed(self, mission_id: str, reason: str, report: Any) -> Any:
        """Hand the mission to the operator with what happened. The event carries a bounded summary and a card."""
        from planet_express.missions.checkpoint import RestoreReport
        summary: dict[str, Any] = {"status": "failed"}
        if report is not None:
            summary = {
                "status": report.status, "partial": report.partial, "written": [clean(w, 160) for w in report.written[:20]],
                "drifted": [clean(json.dumps(d, sort_keys=True), 200) for d in report.drifted[:10]],
                "unrestored": [{"service": clean(u.get("service", "?"), 80), "fields": [clean(f, 40) for f in u.get("fields", [])[:10]]}
                               for u in report.unrestored[:10]],
                "meta_skipped": len(report.meta_skipped)}
        else:
            report = RestoreReport(status="failed", reason=reason)
        body = {"event": "tripwire.rollback_incomplete", "reason": clean(reason, 200), "summary": summary,
                "card": {"mission": mission_id, "options": list(NO_ROLLBACK_OPTIONS)}}
        now = int(self.ms.store._clock())
        try:
            with self.ms.store._write() as conn:
                row = conn.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
                if row is not None and row["status"] != "needs_operator":
                    transition_in_txn(conn, mission_id, "needs_operator", ACTOR, now)
                insert_event(conn, mission_id, "tripwire", ACTOR, body, now)
        except Exception:
            log.exception("could not hand %s to the operator after a failed rollback", mission_id)
            raise
        return report


def _matches(resource: str, pattern: str) -> bool:
    try:
        _host, kind, name = split(resource)
    except ValueError:
        return False
    return fnmatch.fnmatchcase(f"{kind}:{name}", pattern) or fnmatch.fnmatchcase(resource, pattern)


__all__ = ["ACTOR", "PAUSE_SLA_S", "BlindBaseline", "RollbackRefused", "TripwireMonitor", "TripwireStore", "pause_card"]
