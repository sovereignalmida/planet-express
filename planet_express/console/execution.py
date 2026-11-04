"""How a confirmed plan's Repair mission reads in a conversation: plain-language status, a short timeline, and PE's own
verification (never the agent's account of itself). Pure functions over the mission and its stored records; the words are PE's, written from state
and counts. A check that could not be run is shown as such and is never counted as a pass."""
import json
from typing import Any

from planet_express.console import plan as plan_mod
from planet_express.missions.models import NOTHING_CHECKPOINTED_FLAG, Mission
from planet_express.missions.verify.plan import CheckResult, parse_result

# internal state -> (what the operator sees, level). The design brief's vocabulary; internal names never reach the page.
REPAIR_STATUS = {
    "awaiting_decision": ("Starting", "ok"), "queued": ("Starting", "ok"), "investigating": ("Starting", "ok"),
    "executing": ("Working", "ok"), "verifying": ("Checking it worked", "ok"), "completed": ("Done and verified", "ok"),
    # paused_tripwire: a row from before trust mode removed the tripwire; startup recovery moves it to recovery_required.
    "paused_tripwire": ("Needs you: PE isn't sure what state this is in", "crit"), "rollback_pending": ("Rolling back", "warn"),
    "rolled_back": ("Rolled back", "warn"), "failed": ("Didn't work", "high"), "cancelled": ("Scrapped", "none"),
    "needs_operator": ("Needs you: PE isn't sure what state this is in", "crit"),
    "recovery_required": ("Needs you: PE isn't sure what state this is in", "crit"), "blocked": ("Waiting", "warn"),
}
CONTROLS = {                      # which operator actions each state offers (the supervisor still decides whether it can)
    # Trust mode: no pause, resume or take-over; cancel is the stop. rollback_pending is where a restore PE refused (drift) waits, so the
    # operator's explicit "roll back anyway" has a button.
    "rollback": ("needs_operator", "recovery_required", "rollback_pending"),
}
ACTIONS = tuple(CONTROLS)
TERMINALISH = ("completed", "failed", "rolled_back", "cancelled")


# A failed Repair closed because nothing its checkpoint covers changed: only that is known, never "nothing changed".
NOTHING_SNAPSHOTTED_STATUS = ("Didn't work \u2014 nothing PE snapshotted was changed; check the run's record for anything else it did", "high")


def _closed_nothing_checkpointed(mission: Mission) -> bool:
    return mission.status == "failed" and getattr(mission, "outcome_flags", None) == NOTHING_CHECKPOINTED_FLAG


def repair_status(mission: Mission) -> tuple[str, str]:
    if _closed_nothing_checkpointed(mission):
        return NOTHING_SNAPSHOTTED_STATUS
    return REPAIR_STATUS.get(mission.status, ("Working", "ok"))


def check_state(result: CheckResult) -> str:
    """pass | fail | could_not_check. A check that never ran (an unknown kind or bad parameters), timed out or errored inside is NOT a failure of the
    thing it checks and NOT a pass: it is shown as could-not-check. Either way it fails the gate (`ok` is False), exactly as before."""
    if result.ok:
        return "pass"
    if result.attempts == 0 or result.detail == "timed out" or result.detail.startswith("error ("):
        return "could_not_check"
    return "fail"


def verification_view(mission: Mission) -> dict[str, Any] | None:
    """PE's checks, one line each, from the stored result for THIS grant. None until a result exists."""
    result = parse_result(mission.verification_result_json)
    if result is None or result.grant_hash != mission.grant_hash:
        return None
    labels: dict[str, str] = {}
    try:
        for item in (json.loads(mission.verification_plan_json or "{}").get("checks") or []):
            labels[item["id"]] = plan_mod.check_phrase(item)
    except (ValueError, KeyError, TypeError, AttributeError):
        labels = {}
    checks = [{"state": check_state(c), "label": labels.get(c.id, c.id), "evidence": c.detail} for c in result.checks]
    total = len(checks)
    failed = [c for c in checks if c["state"] == "fail"]
    unknown = [c for c in checks if c["state"] == "could_not_check"]
    if result.status == "passed" and not failed and not unknown:
        headline, level = f"All {total} check{'s' if total != 1 else ''} passed", "ok"
    elif failed:
        headline, level = f"{len(failed)} of {total} failed: {failed[0]['label']}", "high"
    elif unknown:
        headline, level = f"{len(unknown)} of {total} check{'s' if total != 1 else ''} could not be run", "warn"
    else:
        headline, level = result.reason or "Verification did not pass", "high"
    return {"headline": headline, "level": level, "checks": checks, "by_pe": True,
            "scan_note": (result.scan.error if result.scan is not None and result.scan.error else None)}


def conversation_status(mission: Mission, live: bool) -> dict[str, Any] | None:
    """How a conversation reads while a confirmed plan is running or needs attention: derived from the run, so an active repair is never hidden
    behind 'Result ready'. None when the run needs no say in the conversation's status."""
    status = mission.status
    if live and status in ("awaiting_decision", "queued", "investigating", "executing", "verifying", "rollback_pending"):
        return {"text": "Working on the plan", "level": "ok", "group": "Working",
                "verdict": {"h": "Carrying out the confirmed plan", "p": "PE is supervising it and will check it worked."}}
    if status in ("needs_operator", "recovery_required", "paused_tripwire", "failed"):
        return {"text": "Run needs you", "level": "crit" if status != "failed" else "high", "group": "Needs you",
                "verdict": {"h": "The run didn't finish cleanly" if status != "failed" else "The run didn't pass PE's checks",
                            "p": ("Nothing PE snapshotted was changed; check the run's record for anything else it did."
                                  if _closed_nothing_checkpointed(mission) else "Look at the run below for what you can do next.")}}
    if status == "completed":
        return {"text": "Done and verified", "level": "ok", "group": "Recent",
                "verdict": {"h": "The plan was carried out and verified", "p": "PE's own checks passed."}}
    if status == "rolled_back":
        return {"text": "Rolled back", "level": "warn", "group": "Recent",
                "verdict": {"h": "The run was rolled back", "p": "The host is back at the checkpoint."}}
    return None


SUPERVISOR = "supervisor"           # missions.supervisor.ACTOR: only its own notes say why a start is waiting
_WAIT_KEYS = ("start_waiting", "start_failed", "checkpoint_refused", "checkpoint_taken")


def start_waiting(ms: Any, mission: Mission) -> str | None:
    """Why an authorised Repair has not started yet, in words, from the supervisor's latest start note; None when nothing is in the
    way (or the run has moved on). Without it the timeline showed "Checkpoint taken" in progress for a start that was blocked."""
    if mission.status != "awaiting_decision":
        return None
    with ms.store._connect() as conn:                    # the newest supervisor notes only: a feed poll must not read the whole log
        rows = conn.execute("SELECT body_json FROM mission_events WHERE mission_id=? AND kind='note' AND actor=? "
                            "ORDER BY seq DESC LIMIT 50", (mission.id, SUPERVISOR)).fetchall()
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except ValueError:
            continue
        key = next((k for k in _WAIT_KEYS if k in body), None)
        if key is None:
            continue
        if key == "checkpoint_taken":
            return None
        if key == "start_waiting":
            return str(body["start_waiting"])[:300]
        if key == "start_failed":
            return f"the last start attempt failed ({str(body['start_failed'])[:80]}); PE tries again shortly"
        return f"no checkpoint could be taken: {str(body.get('why') or body['checkpoint_refused'])[:240]}"
    return None


def timeline(mission: Mission, authorised_by: str | None, waiting: str | None = None) -> list[dict[str, str]]:
    status = mission.status
    bad = status in ("failed", "rolled_back", "rollback_pending", "needs_operator", "recovery_required", "paused_tripwire", "cancelled")
    # A failed verification ends in needs_operator, rollback_pending or rolled_back (on_verify_fail), not only in `failed`.
    checked_and_failed = bool(mission.verification_result_json) and status in ("failed", "needs_operator", "rollback_pending", "rolled_back", "recovery_required")
    checkpoint = "done" if mission.checkpoint_id else ("now" if status in ("awaiting_decision", "queued") else "todo")
    if status == "executing":
        run = "now"
    elif status in ("verifying", "completed"):
        run = "done"
    elif checked_and_failed:
        run = "done"                                       # the plan was carried out; it is the check that failed
    elif bad:
        run = "bad"
    else:
        run = "todo"
    check = ("now" if status == "verifying" else "done" if status == "completed" else "bad" if checked_and_failed else "todo")
    return [{"label": "Authorised", "state": "done", "meta": (authorised_by or "").removeprefix("operator:")},
            ({"label": "Waiting to start", "state": "now", "meta": waiting} if waiting and not mission.checkpoint_id else
             {"label": "Checkpoint taken", "state": checkpoint, "meta": "so it can be rolled back"}),
            # A start that fails after its checkpoint (the launch) waits at the next step instead.
            ({"label": "Waiting to start", "state": "now", "meta": waiting} if waiting and mission.checkpoint_id else
             {"label": "Carrying out the plan", "state": run, "meta": ""}),
            {"label": "PE checks it worked", "state": check, "meta": "PE's own checks, not the agent's say-so"}]


def controls(mission: Mission) -> dict[str, bool]:
    return {name: mission.status in states for name, states in CONTROLS.items()}


__all__ = ["ACTIONS", "CONTROLS", "NOTHING_SNAPSHOTTED_STATUS", "REPAIR_STATUS", "TERMINALISH", "check_state", "controls", "conversation_status", "repair_status", "timeline", "verification_view"]
