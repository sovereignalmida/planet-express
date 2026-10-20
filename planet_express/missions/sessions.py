"""Worker session identity (contract §6): a 256-bit token whose sha256 is the only thing stored, bound to
the session's cgroup AND uid. The token is returned once, to be delivered on an inherited file
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


def issue_session(ms: MissionStore, mission_id: str, session_id: str, cgroup_path: str, *, session_ttl_s: int,
                  lease_ttl_s: int, uid_range: tuple[int, int] | None, actor: str = "core",
                  expected_grant_hash: str | None = None) -> tuple[ContainedSession, str]:
    """Start the session for `mission_id` (status CAS, approval predicate, leases, the one-live-session index and
    the unique-uid allocation are all enforced by `MissionStore.start_session`, in one transaction). The row is
    committed in state `launching` holding its uid BEFORE any launcher runs (contract 22.8.3).
    Returns (the session's identity, token). Only sha256(token) is stored; the caller must deliver the token."""
    token = new_token()
    sid = ms.start_session(mission_id, kind="local_cgroup", token_hash=MissionStore.hash_token(token),
                           session_ttl_s=session_ttl_s, lease_ttl_s=lease_ttl_s, cgroup_path=cgroup_path,
                           actor=actor, session_id=session_id, expected_grant_hash=expected_grant_hash,
                           state="launching", uid_range=uid_range)
    row = ms.get_session(sid)
    return ContainedSession(sid, "local_cgroup", cgroup_path, row["session_uid"]), token


def set_leader_pid(store: Store, session_id: str, pid: int) -> None:
    with store._write() as conn:
        conn.execute("UPDATE mission_sessions SET leader_pid=? WHERE id=?", (pid, session_id))


def resolve_session(store: Store, token_hash: str, peer_cgroup: str | None = None,
                    peer_uid: int | None = None) -> SessionRow | None:
    """The live session this token belongs to, or None. A token alone is never enough: the caller's
    cgroup (or uid, for the per-uid fallback) must equal the one the session is bound to. Revoked and
    expired sessions never resolve."""
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM mission_sessions WHERE token_hash=?", (token_hash,)).fetchone()
    return _bound(store, row, peer_cgroup, peer_uid)


def resolve_session_by_id(store: Store, session_id: str, peer_cgroup: str | None = None,
                          peer_uid: int | None = None) -> SessionRow | None:
    """The live session with this id if the caller's cgroup AND uid are the ones it is bound to. For a socket that is the
    session's own (0600, owned by its unique uid) the identity is the credential; no token is needed (contract 6.1, 22.1)."""
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM mission_sessions WHERE id=?", (session_id,)).fetchone()
    return _bound(store, row, peer_cgroup, peer_uid)


def _bound(store: Store, row, peer_cgroup: str | None, peer_uid: int | None) -> SessionRow | None:
    now = int(store._clock())
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
        return None
    # `launching` rows authenticate like live ones: the cgroup and the uid they are bound to only exist once the
    # launcher has run, and the agent may connect the instant it is exec'd, before Core has seen the status line.
    if row["cgroup_path"] is None and row["session_uid"] is None:
        return None                        # an unbound session cannot be proven to be the caller's
    # Every identity the session is bound to must match (contract 22.1: cgroup AND uid, plus the token the
    # caller already presented). A real session is bound to both.
    if row["cgroup_path"] is not None and (peer_cgroup is None or peer_cgroup != row["cgroup_path"]):
        return None
    if row["session_uid"] is not None and (peer_uid is None or peer_uid != row["session_uid"]):
        return None
    return SessionRow(
        id=row["id"], mission_id=row["mission_id"], kind=row["kind"], cgroup_path=row["cgroup_path"],
        leader_pid=row["leader_pid"], session_uid=row["session_uid"], issued_at=row["issued_at"],
        expires_at=row["expires_at"], fences=json.loads(row["fence_json"]))


def revoke_session(ms: MissionStore, session_id: str, actor: str = "core") -> None:
    ms.revoke_session(session_id, actor)


__all__ = ["SessionRow", "issue_session", "new_token", "resolve_session", "resolve_session_by_id", "revoke_session",
           "set_leader_pid"]
