"""A mission session's lifecycle (rc14.7.7, docs/designs/state-machines.md M7), one place: `launching` -> `live` -> `revoked` -> `reaped`.

`revoked`: its token and endpoints are dead (`revoked_at`); its processes may still be running until proven otherwise. `reaped`: its uid
was released, which happens only once its cgroup is empty and nothing runs as the uid (`MissionStore.release_session_uid`), the proof the
session is gone. Until rc14.7.7 the `state` column never left `live` and the end was implied by two timestamps, which is how the lease
exemption for a held mission came to test the column and never apply (rc14.7.4). Rows written before keep `live` with their timestamps;
`session_state` reads both, so they map onto the same four states."""
from typing import Any

LAUNCHING, LIVE, REVOKED, REAPED = "launching", "live", "revoked", "reaped"

# SQL over a `mission_sessions` row aliased `s`: the session may still be running. Not reaped; and a revoked session with no uid has no
# proof of death (nothing to release), so it counts as running. Every "could it still be running" question asks this.
MAY_RUN_SQL = "(s.revoked_at IS NULL OR s.session_uid IS NULL OR s.uid_released_at IS NULL)"


def session_state(row: Any) -> str:
    """The session's state from its row (a dict or sqlite3.Row), whatever its `state` column says. It may still be running exactly
    when this is not `reaped` (`MAY_RUN_SQL`; a test holds the two together)."""
    if row["revoked_at"] is None:
        return row["state"] if row["state"] in (LAUNCHING, LIVE) else LIVE
    if row["session_uid"] is not None and row["uid_released_at"] is not None:
        return REAPED
    return REVOKED


__all__ = ["LAUNCHING", "LIVE", "MAY_RUN_SQL", "REAPED", "REVOKED", "session_state"]
