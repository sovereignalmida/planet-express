"""Worker session identity (contract §6): a 256-bit token whose sha256 is the only thing stored, bound to
the session's cgroup (or uid). The token is returned once, to be delivered on an inherited file
descriptor; it is never logged, never put in argv or env, never written to an event or artifact."""

import json
import secrets
from dataclasses import dataclass

from planet_express.core.store import Store

from .containment import ContainedSession
from .store import MissionStore


@dataclass(frozen=True)
class SessionRow:
    id: str
    mission_id: str
    kind: str
    cgroup_path: str | None
    leader_pid: int | None
    session_uid: int | None
    issued_at: int
    expires_at: int
    fences: dict[str, int]


def new_token() -> str:
    return secrets.token_hex(32)          # 256 bits


def issue_session(ms: MissionStore, mission_id: str, cs: ContainedSession, *, session_ttl_s: int,
                  lease_ttl_s: int, actor: str = "core",
                  expected_grant_hash: str | None = None) -> tuple[str, str]:
    """Start the session for `mission_id` inside `cs` (status CAS, approval predicate, leases and the
    one-live-session index are all enforced by `MissionStore.start_session`, in one transaction).
    Returns (session_id, token). Only sha256(token) is stored; the caller must deliver the token itself."""
    token = new_token()
    kind = "local_uid" if cs.kind == "local_uid" else "local_cgroup"      # the fake presents as a cgroup
    sid = ms.start_session(mission_id, kind=kind, token_hash=MissionStore.hash_token(token),
                           session_ttl_s=session_ttl_s, lease_ttl_s=lease_ttl_s, cgroup_path=cs.cgroup_path,
                           session_uid=cs.session_uid, actor=actor, session_id=cs.session_id,
                           expected_grant_hash=expected_grant_hash)
    return sid, token


def set_leader_pid(store: Store, session_id: str, pid: int) -> None:
    with store._write() as conn:
        conn.execute("UPDATE mission_sessions SET leader_pid=? WHERE id=?", (pid, session_id))


def resolve_session(store: Store, token_hash: str, peer_cgroup: str | None = None,
                    peer_uid: int | None = None) -> SessionRow | None:
    """The live session this token belongs to, or None. A token alone is never enough: the caller's
    cgroup (or uid, for the per-uid fallback) must equal the one the session is bound to. Revoked and
    expired sessions never resolve."""
    now = int(store._clock())
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM mission_sessions WHERE token_hash=?", (token_hash,)).fetchone()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
        return None
    if row["cgroup_path"] is not None:
        if peer_cgroup is None or peer_cgroup != row["cgroup_path"]:
            return None
    elif row["session_uid"] is not None:
        if peer_uid is None or peer_uid != row["session_uid"]:
            return None
    else:
        return None                        # an unbound session cannot be proven to be the caller's
    return SessionRow(
        id=row["id"], mission_id=row["mission_id"], kind=row["kind"], cgroup_path=row["cgroup_path"],
        leader_pid=row["leader_pid"], session_uid=row["session_uid"], issued_at=row["issued_at"],
        expires_at=row["expires_at"], fences=json.loads(row["fence_json"]))


def revoke_session(ms: MissionStore, session_id: str, actor: str = "core") -> None:
    ms.revoke_session(session_id, actor)


__all__ = ["SessionRow", "issue_session", "new_token", "resolve_session", "revoke_session", "set_leader_pid"]
