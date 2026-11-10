"""Why a mission is held for the operator (`needs_operator`), from its own record (rc14.7.5, docs/designs/state-machines.md M4).

`needs_operator` is one status for several situations that want different things from the operator: PE's checks failed (check again,
keep the changes, or roll back), PE could not check, a restore was refused or did not finish, a cancel is held because the run's changes
are still there, the run did not finish. The edge that enters `needs_operator` says which, in its state event (`why`, written by
`transition`); `hold_reason` reads it back. Before, each surface guessed from columns (`held_after_failed_checks`), and a failed rollback
of a failed run passed that guess, so "Keep the changes" was offered on a half-restored host. Stored values and the schema are
unchanged: a hold recorded before this has no `why`, and the old rule decides it."""
import json
from typing import Any

from .verify.plan import held_after_failed_checks

FAILED_CHECKS = "failed_checks"         # PE's checks ran for this grant and did not pass
UNVERIFIED = "unverified"               # PE could not run its checks (no verifier, or it broke)
RESTORE_REFUSED = "restore_refused"     # PE refused to restore before writing anything (files changed since the checkpoint)
RESTORE_FAILED = "restore_failed"       # a restore started and did not finish cleanly
CANCEL_HELD = "cancel_held"             # cancelled, but what the run changed still differs from the checkpoint
RUN_UNFINISHED = "run_unfinished"       # the session ended without a result
RECOVERED = "recovered"                 # PE restarted mid-run and handed what it found to the operator
UNKNOWN = "unknown"
REASONS = (FAILED_CHECKS, UNVERIFIED, RESTORE_REFUSED, RESTORE_FAILED, CANCEL_HELD, RUN_UNFINISHED, RECOVERED, UNKNOWN)


def hold_reason(store: Any, mission: Any) -> str | None:
    """Why `mission` is held, or None when it is not in `needs_operator`. `failed_checks` also needs the stored result to be a failure
    for the mission's current grant (what Check again and Keep act on), whatever the event says."""
    if getattr(mission, "status", None) != "needs_operator":
        return None
    with store._connect() as conn:
        row = conn.execute(
            "SELECT body_json FROM mission_events WHERE mission_id=? AND kind='state' AND json_extract(body_json, '$.refused') IS NULL "
            "AND json_extract(body_json, '$.to') = 'needs_operator' ORDER BY seq DESC LIMIT 1", (mission.id,)).fetchone()
    body: dict = {}
    if row is not None:
        try:
            body = json.loads(row["body_json"]) or {}
        except ValueError:
            body = {}
    why = body.get("why") if isinstance(body, dict) else None
    if why is None:
        # A hold recorded before reasons were (or by a writer that gives none). Only a hold that came out of `verifying` can be one
        # for failed checks: one out of a rollback or a recovery keeps the failed result of the run it undid, and must not offer Keep.
        came_from = body.get("from") if isinstance(body, dict) else None
        return FAILED_CHECKS if came_from == "verifying" and held(mission) else UNKNOWN
    if why == FAILED_CHECKS:
        return FAILED_CHECKS if held(mission) else UNKNOWN
    return why if why in REASONS else UNKNOWN


def held(mission: Any) -> bool:
    """The stored result is a failure of PE's checks for the mission's current grant (the column rule, for a record with no reason)."""
    try:
        return held_after_failed_checks(mission)
    except AttributeError:                              # a partial record (a test double) carries no verification result
        return False


RECHECKABLE = (FAILED_CHECKS, UNVERIFIED)   # Check again applies: PE's checks failed, or PE could not run them
KEEPABLE = (FAILED_CHECKS,)                 # Keep applies only after PE's checks ran and failed

__all__ = ["CANCEL_HELD", "FAILED_CHECKS", "KEEPABLE", "REASONS", "RECHECKABLE", "RECOVERED", "RESTORE_FAILED", "RESTORE_REFUSED",
           "RUN_UNFINISHED", "UNKNOWN", "UNVERIFIED", "held", "hold_reason"]
