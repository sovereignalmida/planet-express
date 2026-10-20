"""Mission CRUD, the redacting event append, and the one-transaction session start (contract §2)."""

import hashlib
import re
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from planet_express.core.store import Store

from . import leases
from .approvals import has_approving_row
from .canonical import canonical_json, sha256_hex
from .events import insert_event
from .models import Grant, Mission, MissionEvent, Origin, SessionKind
from .state import MissionNotFound, transition_in_txn


class GrantLocked(RuntimeError):
    """An approving row already exists for this grant hash, so the grant is immutable."""


class StandingGrantInactive(RuntimeError):
    """The standing grant a change was meant to replace is already revoked (or gone): nothing was written."""


class SessionConflict(RuntimeError):
    """The mission already has a live session."""


class NoFreeUid(RuntimeError):
    """Every uid in the session range is held by a session that has not been reaped yet."""


def grant_resources(grant: Grant) -> list[str]:
    """The lease set a grant implies: every scoped stack, service, mount and path on every host."""
    out: list[str] = []
    for host in grant.scope.hosts:
        out += [f"host:{host}/stack:{s}" for s in grant.scope.stacks]
        out += [f"host:{host}/service:{s}" for s in grant.scope.services]
        out += [f"host:{host}/mount:{p}" for p in grant.scope.mounts]
        out += [f"host:{host}/path:{p}" for p in grant.scope.paths]
    return out


class MissionStore:
    def __init__(self, store: Store):
        self.store = store

    def _now(self) -> int:
        return int(self.store._clock())

    # ── missions ────────────────────────────────────────────────────────────
    def create_mission(self, *, title: str, origin: Origin, grant: Grant, goal: str, success_criteria: str,
                       runtime: str, priority: int = 50, incident_id: str | None = None,
                       standing_grant_id: str | None = None, actor: str = "core") -> Mission:
        with self.store._write() as conn:
            mission_id = self.create_in_txn(
                conn, title=title, origin=origin, grant=grant, goal=goal, success_criteria=success_criteria,
                runtime=runtime, priority=priority, incident_id=incident_id,
                standing_grant_id=standing_grant_id, actor=actor)
        return self.get_mission(mission_id)

    def create_in_txn(self, conn, *, title: str, origin: Origin, grant: Grant, goal: str, success_criteria: str,
                      runtime: str, priority: int = 50, incident_id: str | None = None,
                      standing_grant_id: str | None = None, actor: str = "core") -> str:
        """Insert a draft mission inside the caller's write transaction and return its id, so a caller can
        make creation and what follows (queueing, an evidence note) one atomic unit."""
        now = self._now()
        grant_json = grant.canonical()
        row = conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id, 3) AS INTEGER)), 0) + 1 FROM missions"
        ).fetchone()
        mission_id = f"M-{row[0]}"
        # Validate with the strict model BEFORE the insert, so no unreadable row can be committed.
        Mission(id=mission_id, title=title, origin=origin, incident_id=incident_id, priority=priority,
                status="draft", tier=grant.tier, goal=goal, success_criteria=success_criteria,
                grant_json=grant_json, grant_hash=sha256_hex(grant.model_dump(mode="json")),
                standing_grant_id=standing_grant_id, runtime=runtime, created_at=now, updated_at=now)
        conn.execute(
            "INSERT INTO missions (id, title, origin, incident_id, priority, status, tier, goal, "
            "success_criteria, grant_json, grant_hash, standing_grant_id, runtime, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mission_id, title, origin, incident_id, priority, grant.tier, goal, success_criteria,
             grant_json, sha256_hex(grant.model_dump(mode="json")), standing_grant_id, runtime, now, now),
        )
        insert_event(conn, mission_id, "state", actor, {"from": None, "to": "draft"}, now)
        return mission_id

    def get_mission(self, mission_id: str) -> Mission:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise MissionNotFound(mission_id)
        return Mission(**dict(row))

    def list_missions(self, status: str | None = None) -> list[Mission]:
        with self.store._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM missions ORDER BY created_at, id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM missions WHERE status=? ORDER BY created_at, id",
                                    (status,)).fetchall()
        return [Mission(**dict(r)) for r in rows]

    @staticmethod
    def _check_readable(conn, mission_id: str, **changes: Any) -> None:
        """Build the row's `Mission` with the proposed values BEFORE writing, so a value the read path would
        refuse (size caps, enums) never reaches the table. Raises pydantic's ValidationError (a ValueError)."""
        row = dict(conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())
        Mission(**{**row, **changes})

    def set_grant(self, mission_id: str, grant: Grant, actor: str = "core") -> Mission:
        """Replace a grant. Refused once any approving row exists for the current hash (contract §2); a
        grant that must change after approval is a new mission, or, before any approval, a new hash."""
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT status, grant_hash FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if has_approving_row(conn, mission_id, row["grant_hash"]):
                raise GrantLocked(f"{mission_id}: grant is approved and immutable")
            if row["status"] not in ("draft", "queued", "investigating", "awaiting_decision", "blocked",
                                     "needs_operator"):
                raise GrantLocked(f"{mission_id}: grant cannot change while {row['status']}")
            self._check_readable(conn, mission_id, grant_json=grant.canonical(), tier=grant.tier,
                                 grant_hash=sha256_hex(grant.model_dump(mode="json")))
            conn.execute(
                "UPDATE missions SET grant_json=?, grant_hash=?, tier=?, updated_at=? WHERE id=?",
                (grant.canonical(), sha256_hex(grant.model_dump(mode="json")), grant.tier, now, mission_id),
            )
            insert_event(conn, mission_id, "decision", actor,
                         {"grant_replaced": sha256_hex(grant.model_dump(mode="json"))}, now)
        return self.get_mission(mission_id)

    def set_plan(self, mission_id: str, plan: Mapping[str, Any], actor: str = "core") -> Mission:
        """Attach a plan (hashed here from its canonical JSON). Refused once the plan hash is approved."""
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT plan_hash FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["plan_hash"] and has_approving_row(conn, mission_id, row["plan_hash"]):
                raise GrantLocked(f"{mission_id}: plan is approved and immutable")
            plan_json, plan_hash = canonical_json(plan), sha256_hex(plan)
            self._check_readable(conn, mission_id, plan_json=plan_json, plan_hash=plan_hash)
            conn.execute("UPDATE missions SET plan_json=?, plan_hash=?, updated_at=? WHERE id=?",
                         (plan_json, plan_hash, now, mission_id))
            insert_event(conn, mission_id, "decision", actor, {"plan_replaced": sha256_hex(plan)}, now)
        return self.get_mission(mission_id)

    def set_verification_plan(self, mission_id: str, plan: Any, actor: str = "core") -> Mission:
        """Attach the verification plan (contract 10). It must be valid and, for a mutating grant, the exact plan the
        grant's `verification.plan_sha256` names: a different plan has a different hash, so it cannot be attached
        under an approved grant, and a changed plan means a changed grant hash and a new approval."""
        from .verify.plan import PlanInvalid, binding_problems, plan_hash, validate_plan
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT grant_json, grant_hash, verification_plan_json FROM missions WHERE id=?",
                               (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            try:
                validate_plan(plan)
            except PlanInvalid as exc:
                raise ValueError(str(exc)) from None
            grant = Grant.model_validate_json(row["grant_json"])
            problems = binding_problems(grant, plan)
            if problems:
                raise ValueError("; ".join(problems))
            plan_json = canonical_json(plan.model_dump(mode="json"))
            if (row["verification_plan_json"] and row["verification_plan_json"] != plan_json
                    and has_approving_row(conn, mission_id, row["grant_hash"])):
                raise GrantLocked(f"{mission_id}: the verification plan is approved and immutable")
            self._check_readable(conn, mission_id, verification_plan_json=plan_json)
            conn.execute("UPDATE missions SET verification_plan_json=?, updated_at=? WHERE id=?",
                         (plan_json, now, mission_id))
            insert_event(conn, mission_id, "decision", actor, {"verification_plan_set": plan_hash(plan)}, now)
        return self.get_mission(mission_id)

    def set_verification_result(self, mission_id: str, result: Any, actor: str = "core") -> Mission:
        """Store Core's verification result. Only while `verifying`, and only for the mission's CURRENT grant hash
        and checkpoint: a result for anything else is refused here and would be refused by the completion gate too."""
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT status, grant_hash, checkpoint_id FROM missions WHERE id=?",
                               (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["status"] != "verifying":
                raise ValueError(f"{mission_id}: a verification result is stored only while verifying ({row['status']})")
            if result.grant_hash != row["grant_hash"] or result.checkpoint_id != row["checkpoint_id"]:
                raise ValueError(f"{mission_id}: the result is not for the current grant hash and checkpoint")
            text = result.canonical()
            self._check_readable(conn, mission_id, verification_result_json=text)
            conn.execute("UPDATE missions SET verification_result_json=?, updated_at=? WHERE id=?",
                         (text, now, mission_id))
            insert_event(conn, mission_id, "decision", actor,
                         {"verification_result": result.status, "checks": len(result.checks)}, now)
        return self.get_mission(mission_id)

    def set_outcome(self, mission_id: str, outcome: str | None, flags: str | None = None, actor: str = "core") -> Mission:
        """Record how a mission ended (`outcome`, `partially_applied` in `flags`). A label only: it decides nothing."""
        now = self._now()
        with self.store._write() as conn:
            self._check_readable(conn, mission_id, outcome=outcome, outcome_flags=flags)
            cur = conn.execute("UPDATE missions SET outcome=?, outcome_flags=?, updated_at=? WHERE id=?",
                               (outcome, flags, now, mission_id))
            if cur.rowcount != 1:
                raise MissionNotFound(mission_id)
            insert_event(conn, mission_id, "decision", actor, {"outcome": outcome, "flags": flags}, now)
        return self.get_mission(mission_id)

    # ── events ──────────────────────────────────────────────────────────────
    def append_event(self, mission_id: str, kind: str, actor: str, body: dict[str, Any]) -> int:
        """Redacts the body, then inserts. There is deliberately no update or delete here."""
        with self.store._write() as conn:
            return insert_event(conn, mission_id, kind, actor, body, self._now())

    def events(self, mission_id: str, after_seq: int = 0) -> list[MissionEvent]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM mission_events WHERE mission_id=? AND seq>? ORDER BY seq",
                                (mission_id, after_seq)).fetchall()
        return [MissionEvent(**dict(r)) for r in rows]

    # ── sessions ────────────────────────────────────────────────────────────
    def start_session(self, mission_id: str, *, kind: SessionKind, token_hash: str, session_ttl_s: int,
                      lease_ttl_s: int, resources: Sequence[str] | None = None,
                      declared: Mapping[str, Sequence[str]] | None = None, cgroup_path: str | None = None,
                      leader_pid: int | None = None, session_uid: int | None = None,
                      actor: str = "core", session_id: str | None = None,
                      expected_grant_hash: str | None = None, state: str = "live",
                      uid_range: tuple[int, int] | None = None) -> str:
        """Status CAS, approval predicate, lease acquire and session insert in ONE BEGIN IMMEDIATE.

        Observe and propose missions move queued -> investigating and take no leases (they cannot mutate).
        Repair and above move into `executing`, which re-evaluates the execution predicate, then lease the
        grant's scope. Any failure rolls everything back, so a refused start leaves no lease behind.

        `uid_range` makes this the SOLE uid allocator (contract 22.8.3): the lowest uid in the range that no
        unreleased session holds is committed on the row, in this same transaction, BEFORE any launcher runs. A
        row in state `launching` is not a live worker yet (`resolve_session` still authenticates it: the worker socket must work during launch); `mark_session_live` flips it."""
        if state not in ("launching", "live"):
            raise ValueError("state must be launching or live")
        if not re.fullmatch(r"[0-9a-f]{64}", token_hash):
            raise ValueError("token_hash must be a sha256 hex digest")
        now = self._now()
        session_id = session_id or f"S-{secrets.token_hex(8)}"
        with self.store._write() as conn:
            row = conn.execute("SELECT status, tier, grant_json, grant_hash FROM missions WHERE id=?",
                               (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if expected_grant_hash is not None and row["grant_hash"] != expected_grant_hash:
                raise SessionConflict(f"{mission_id}: the grant changed since the caller read it")
            if conn.execute("SELECT 1 FROM mission_sessions WHERE mission_id=? AND revoked_at IS NULL",
                            (mission_id,)).fetchone():
                raise SessionConflict(f"{mission_id} already has a live session")
            mutating = row["tier"] not in ("observe", "propose")
            target = "executing" if mutating else "investigating"
            if mutating or row["status"] != "investigating":
                transition_in_txn(conn, mission_id, target, actor, now)
            fences: dict[str, int] = {}
            if mutating:
                # `resources` can only add to the grant's own lease set, never replace it.
                wanted = grant_resources(Grant.model_validate_json(row["grant_json"]))
                wanted += [r for r in (resources or ()) if r not in wanted]
                if not wanted:
                    raise ValueError(f"{mission_id}: a {row['tier']} grant with an empty scope has nothing to lease")
                fences = leases.acquire_in_txn(conn, wanted, mission_id, lease_ttl_s, now, declared)
            if uid_range is not None and session_uid is None:
                held = {r[0] for r in conn.execute("SELECT session_uid FROM mission_sessions "
                                                   "WHERE session_uid IS NOT NULL AND uid_released_at IS NULL")}
                free = next((u for u in range(uid_range[0], uid_range[1] + 1) if u not in held), None)
                if free is None:
                    raise NoFreeUid(f"all session uids in {uid_range[0]}-{uid_range[1]} are held")
                session_uid = free
            try:
                conn.execute(
                    "INSERT INTO mission_sessions (id, mission_id, token_hash, issued_at, expires_at, kind, "
                    "cgroup_path, leader_pid, session_uid, fence_json, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, mission_id, token_hash, now, now + session_ttl_s, kind, cgroup_path,
                     leader_pid, session_uid, canonical_json(fences), state),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionConflict(str(exc)) from exc
            insert_event(conn, mission_id, "note", actor, {"session_started": session_id, "kind": kind}, now)
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def mark_session_live(self, session_id: str, leader_pid: int | None = None) -> bool:
        """`launching` -> `live`, only while the session is not revoked. False means it was not flipped."""
        with self.store._write() as conn:
            cur = conn.execute("UPDATE mission_sessions SET state='live', leader_pid=COALESCE(?, leader_pid) "
                               "WHERE id=? AND state='launching' AND revoked_at IS NULL", (leader_pid, session_id))
            return cur.rowcount == 1

    def release_session_uid(self, session_id: str) -> bool:
        """Free the uid for reuse. Called only after the session cgroup is gone and no process runs as the uid."""
        with self.store._write() as conn:
            cur = conn.execute("UPDATE mission_sessions SET uid_released_at=? WHERE id=? "
                               "AND session_uid IS NOT NULL AND uid_released_at IS NULL", (self._now(), session_id))
            return cur.rowcount == 1

    def unreleased_revoked_sessions(self) -> list[dict[str, Any]]:
        """Revoked sessions whose uid is still held: the reaper retries releasing them."""
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM mission_sessions WHERE revoked_at IS NOT NULL "
                                "AND session_uid IS NOT NULL AND uid_released_at IS NULL").fetchall()
        return [dict(r) for r in rows]

    def revoke_session(self, session_id: str, actor: str = "core") -> None:
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT mission_id FROM mission_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise LookupError(session_id)
            conn.execute("UPDATE mission_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                         (now, session_id))
            insert_event(conn, row["mission_id"], "note", actor, {"session_revoked": session_id}, now)

    def live_session(self, mission_id: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_sessions WHERE mission_id=? AND revoked_at IS NULL",
                               (mission_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    # ── standing grants ─────────────────────────────────────────────────────
    def create_standing_grant(self, *, grant_id: str, scope: Mapping[str, Any], tier: str, created_by: str,
                              expires_at: int, window: Mapping[str, Any] | None = None, version: int = 1) -> None:
        from .models import GrantScope, Window
        scope_json = canonical_json(GrantScope.model_validate(scope).model_dump(mode="json"))
        window_json = canonical_json(Window.model_validate(window).model_dump(mode="json")) if window else None
        with self.store._write() as conn:
            conn.execute(
                "INSERT INTO standing_grants (id, scope_json, tier, window_json, created_by, created_at, "
                "expires_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (grant_id, scope_json, tier, window_json, created_by, self._now(), expires_at, version),
            )

    def supersede_standing_grant(self, *, old_id: str, grant_id: str, scope: Mapping[str, Any], tier: str, created_by: str,
                                 expires_at: int, window: Mapping[str, Any] | None, version: int) -> None:
        """A new version and the revocation of the one it replaces, in ONE transaction: a crash can never leave both active."""
        from .models import GrantScope, Window
        scope_json = canonical_json(GrantScope.model_validate(scope).model_dump(mode="json"))
        window_json = canonical_json(Window.model_validate(window).model_dump(mode="json")) if window else None
        now = self._now()
        with self.store._write() as conn:
            # Conditional on the old row still being active, checked FIRST: a revoke that landed after the caller read it
            # must not be undone by a new active version, so zero rows updated aborts the whole transaction.
            cur = conn.execute("UPDATE standing_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, old_id))
            if cur.rowcount != 1:
                raise StandingGrantInactive(f"{old_id} is not an active standing grant")
            conn.execute(
                "INSERT INTO standing_grants (id, scope_json, tier, window_json, created_by, created_at, "
                "expires_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (grant_id, scope_json, tier, window_json, created_by, now, expires_at, version))

    def revoke_standing_grant(self, grant_id: str) -> None:
        with self.store._write() as conn:
            conn.execute("UPDATE standing_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                         (self._now(), grant_id))


__all__ = ["GrantLocked", "MissionStore", "NoFreeUid", "SessionConflict", "StandingGrantInactive", "grant_resources"]
