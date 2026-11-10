"""How a confirmed plan's Repair mission reads in a conversation: plain-language status, a short timeline, and PE's own
verification (never the agent's account of itself). Pure functions over the mission and its stored records; the words are PE's, written from state
and counts. A check that could not be run is shown as such and is never counted as a pass."""
import json
import time
from dataclasses import dataclass
from typing import Any

from planet_express.console import plan as plan_mod
from planet_express.missions.models import KEPT_BY_OPERATOR_FLAG, NOTHING_CHECKPOINTED_FLAG, Mission
from planet_express.missions.verify.plan import CheckResult, held_after_failed_checks, parse_result

# ── The run phase (rc14.7.5, docs/designs/state-machines.md C2/C3/C6) ──────────────────────────────────────────────────────
# One table says, for each phase a confirmed plan's run can be in, what every surface shows and what the operator may do: the run's
# label, the conversation's rail line and verdict, the line posted into the thread, and the actions. The phase is worked out once
# (`run_phase`) from the Repair mission's status plus what the status alone does not say: PE's checks failed (held), the operator kept
# the changes, nothing PE snapshotted changed, an unfinished undo was dismissed, a rollback is running, the start is waiting.
# Before this, six places worded the run and four declared its actions, and they disagreed (a kept run read "Didn't work" under
# "Needs you"; a held run whose checks failed read "PE isn't sure what state this is in").


@dataclass(frozen=True)
class Phase:
    name: str
    label: str                       # the run's own status line
    level: str                       # ok | warn | high | crit | none
    rail: str | None                 # the conversation's status while this run is the latest; None: the run has no say in it
    rail_level: str
    group: str                       # the rail group: Working | Needs you | Recent
    verdict: tuple[str, str]
    thread: str | None               # the line posted into the conversation once when the run enters the phase; None posts nothing
    actions: tuple[str, ...]         # what the operator may do now (the supervisor still decides whether it can be done)
    final: bool                      # nothing more can happen to the run


_WORKING = ("Carrying out the confirmed plan", "PE is supervising it and will check it worked.")
_NOT_CLEAN = ("The run didn't finish cleanly", "Look at the run below for what you can do next.")
PHASES: dict[str, Phase] = {p.name: p for p in (
    Phase("starting", "Starting", "ok", "Working on the plan", "ok", "Working", _WORKING, None, (), False),
    Phase("waiting_to_start", "Waiting to start", "warn", "Waiting to start", "warn", "Working",
          ("Waiting to start", "PE will start it as soon as nothing is in the way; the run below says what is."), None, (), False),
    Phase("working", "Working", "ok", "Working on the plan", "ok", "Working", _WORKING,
          "PE took a checkpoint and is carrying out the plan.", (), False),
    Phase("checking", "Checking it worked", "ok", "Working on the plan", "ok", "Working", _WORKING,
          "The plan has been carried out. PE is checking it worked.", (), False),
    Phase("rolling_back", "Rolling back\u2026", "warn", "Rolling back", "warn", "Working",
          ("Rolling back to the checkpoint", "PE is putting the files and services back. The result shows here when it lands."), None, (), False),
    # Undo is offered on a done run while its checkpoint is kept (`can_undo`); that is checked per run, not by the phase.
    Phase("done", "Done and verified", "ok", "Done and verified", "ok", "Recent",
          ("The plan was carried out and verified", "PE's own checks passed."), "Done and verified by PE", (), False),
    # rollback_pending entered by PE (a grant that rolls back on a failed check, recovery): nothing rolls back until the operator asks.
    Phase("rollback_wanted", "Needs you: roll back to the checkpoint", "crit", "Run needs you", "crit", "Needs you", _NOT_CLEAN,
          "Needs you: roll back to the checkpoint", ("rollback",), False),
    Phase("held_failed_checks", "Needs you: PE's checks failed", "crit", "Run needs you", "crit", "Needs you",
          ("The run didn't pass PE's checks", "Check it again, keep the changes, or roll back to the checkpoint."),
          "Needs you: PE's checks failed", ("rollback", "recheck", "keep"), False),
    Phase("held_unknown", "Needs you: PE isn't sure what state this is in", "crit", "Run needs you", "crit", "Needs you", _NOT_CLEAN,
          "Needs you: PE isn't sure what state this is in", ("rollback",), False),
    # paused_tripwire: a row from before trust mode removed the tripwire; startup recovery moves it to recovery_required.
    Phase("held_legacy", "Needs you: PE isn't sure what state this is in", "crit", "Run needs you", "crit", "Needs you", _NOT_CLEAN,
          "Needs you: PE isn't sure what state this is in", (), False),
    # failed is closed: nothing can be done to it from here, so it is not "Needs you" (rc14.7.5).
    Phase("failed", "Didn't work", "high", "Didn't work", "high", "Recent",
          ("The run didn't pass PE's checks", "PE closed it. The run below shows what it did and what PE found."), "Didn't work", (), True),
    # A failed Repair closed because nothing its checkpoint covers changed: only that is known, never "nothing changed".
    Phase("failed_nothing_changed",
          "Didn't work \u2014 nothing PE snapshotted was changed; check the run's record for anything else it did", "high",
          "Didn't work", "high", "Recent",
          ("The run didn't pass PE's checks", "Nothing PE snapshotted was changed; check the run's record for anything else it did."),
          "Didn't work \u2014 nothing PE snapshotted was changed; check the run's record for anything else it did", (), True),
    # rc14.7: the operator kept what a held run changed and closed it.
    Phase("kept", "Kept as it was \u2014 PE's checks had not passed", "warn", "Kept as it was", "warn", "Recent",
          ("You kept what the run changed", "PE's checks had not passed when it was closed."),
          "Closed with the changes kept; PE's checks had not passed", (), True),
    Phase("rolled_back", "Rolled back", "warn", "Rolled back", "warn", "Recent",
          ("The run was rolled back", "The host is back at the checkpoint."), "Rolled back", (), True),
    Phase("cancelled", "Cancelled", "none", None, "none", "Recent", ("Cancelled", ""), "Cancelled", (), True),
    # rc14.4: a run that was done and verified, whose undo did not finish, closed at the operator's cancel without judging the host.
    Phase("dismissed", "Closed \u2014 it was done and verified; the unfinished undo was dismissed", "none", None, "none", "Recent",
          ("Closed", ""), "Closed \u2014 it was done and verified; the unfinished undo was dismissed", (), True),
    Phase("blocked", "Waiting", "warn", None, "warn", "Working", ("Waiting", ""), None, (), False),
)}
ACTIONS = ("rollback", "recheck", "keep")            # every action a run can offer; trust mode removed pause, resume and take-over
TERMINALISH = ("completed", "failed", "rolled_back", "cancelled")          # missions.state.TERMINAL
SETTLED = ("failed", "rolled_back", "cancelled")       # nothing more can happen to these: a rollback can start from `completed`
UNDO_STATES = ("completed",)
_BY_STATUS = {"draft": "starting", "queued": "starting", "investigating": "starting", "awaiting_decision": "starting",
              "executing": "working", "verifying": "checking", "completed": "done", "rollback_pending": "rollback_wanted",
              "needs_operator": "held_unknown", "recovery_required": "held_unknown", "paused_tripwire": "held_legacy",
              "failed": "failed", "rolled_back": "rolled_back", "cancelled": "cancelled", "blocked": "blocked"}


def run_phase(mission: Mission, rolling_back: bool = False, waiting: str | None = None) -> Phase:
    """The phase a run is in. `rolling_back`: a restore is running (the supervisor's worker has it, or the operator asked for one and
    it has not landed); `waiting`: why an authorised run has not started (`start_waiting`)."""
    status = mission.status
    flags = getattr(mission, "outcome_flags", None)
    if rolling_back and status not in SETTLED:
        return PHASES["rolling_back"]
    if waiting and status in ("awaiting_decision", "queued"):
        return PHASES["waiting_to_start"]
    if status == "failed" and flags == NOTHING_CHECKPOINTED_FLAG:
        return PHASES["failed_nothing_changed"]
    if status == "failed" and flags == KEPT_BY_OPERATOR_FLAG:
        return PHASES["kept"]
    if status == "cancelled" and getattr(mission, "outcome", None) == "dismissed":
        return PHASES["dismissed"]
    if status == "needs_operator" and _held(mission):
        return PHASES["held_failed_checks"]
    return PHASES[_BY_STATUS.get(status, "working")]


def _held(mission: Mission) -> bool:
    try:
        return held_after_failed_checks(mission)
    except AttributeError:                               # a partial record (a test double) carries no verification result
        return False


def repair_status(mission: Mission, rolling_back: bool = False) -> tuple[str, str]:
    phase = run_phase(mission, rolling_back)
    return phase.label, phase.level


_ROLLBACK_EVENTS = ("checkpoint.restore_started", "checkpoint.restore_refused", "checkpoint.restore_done", "mission.rollback_incomplete")


def rollback_view(ms: Any, mission: Mission, in_flight: bool) -> dict[str, Any] | None:
    """What the run's rollback is doing, from PE's own record (Core's and the supervisor's events, never the agent's):
    `running` while the supervisor's rollback worker has it; `refused` when PE refused to restore before writing anything (files
    changed since the checkpoint) and the run waits for an explicit "roll back anyway"; `incomplete` when a restore ran and
    did not finish cleanly, with why; `not_run` when the worker could not start it; `done` once rolled back. None when no
    rollback was ever asked for. A `done` or `incomplete` view carries `left_alone` when the restore left something as it is
    because this run had not changed it (rc14.3)."""
    if in_flight and mission.status not in SETTLED:
        return {"state": "running", "reason": ""}
    rows = _recent_trusted_notes(ms, mission)
    left_alone = _left_alone(rows)
    if mission.status == "rolled_back":
        return {"state": "done", "reason": "", **({"left_alone": left_alone} if left_alone else {})}
    view = _rollback_state(rows, mission)
    if view is not None and left_alone and view["state"] == "incomplete":
        view["left_alone"] = left_alone
    return view


def _recent_trusted_notes(ms: Any, mission: Mission) -> list[dict[str, Any]]:
    with ms.store._connect() as conn:                    # the newest trusted notes only: a feed poll must not read the whole log
        rows = conn.execute("SELECT body_json FROM mission_events WHERE mission_id=? AND kind IN ('note', 'decision') "
                            "AND (actor IN ('core', ?) OR actor LIKE 'operator:%') ORDER BY seq DESC LIMIT 100",
                            (mission.id, SUPERVISOR)).fetchall()
    out = []
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except ValueError:
            continue
        if isinstance(body, dict):
            out.append(body)
    return out


def _left_alone(bodies: list[dict[str, Any]]) -> list[str]:
    """rc14.3: what the newest finished restore left as it is because this run had not changed it (someone changed it since the run
    ended). Neutral information, not a failure: names only, bounded."""
    for body in bodies:
        if body.get("event") in ("checkpoint.restore_done", "checkpoint.restore_refused"):     # refused also records a failed restore
            items = body.get("left_alone") if isinstance(body.get("left_alone"), list) else []
            return [str(i.get("service") or i.get("path"))[:160] for i in items[:20]
                    if isinstance(i, dict) and (i.get("service") or i.get("path"))]
        if body.get("event") == "checkpoint.restore_started":
            return []
    return []


def _rollback_state(bodies: list[dict[str, Any]], mission: Mission) -> dict[str, Any] | None:
    for body in bodies:
        if "rollback_not_run" in body:
            return {"state": "not_run", "reason": str(body["rollback_not_run"])[:200]}
        event = body.get("event")
        if event not in _ROLLBACK_EVENTS:
            continue
        if event == "mission.rollback_incomplete":
            return {"state": "incomplete", "reason": str(body.get("reason") or "the restore did not complete cleanly")[:200]}
        if (event == "checkpoint.restore_refused" and body.get("status", "refused") == "refused" and not body.get("partial")
                and "rollback" in run_phase(mission).actions):
            return {"state": "refused", "reason": str(body.get("reason") or "files changed since the checkpoint")[:200]}
        return None                                     # started (and not running any more) or done: the state says the rest
    return None


def latest_state_event(ms: Any, mission_id: str) -> tuple[int, str | None]:
    """The mission's newest state change: (its event seq, its actor), or (0, None) before any."""
    with ms.store._connect() as conn:
        row = conn.execute("SELECT seq, actor FROM mission_events WHERE mission_id=? AND kind='state' ORDER BY seq DESC LIMIT 1",
                           (mission_id,)).fetchone()
    return (int(row["seq"]), row["actor"]) if row else (0, None)


def rollback_running(owner: Any, mission_id: str) -> bool:
    """A rollback of this mission is running, by the supervisor's own record (`rollback_in_flight`, which a rollback on the worker
    and one on a request both register in). `owner` is the supervisor or the approval flow; False where neither can say."""
    probe = getattr(owner, "rollback_in_flight", None)
    try:
        return bool(probe(mission_id)) if probe is not None else False
    except Exception:                                    # noqa: BLE001 - a status read never breaks a feed or the reporting
        return False


def not_covered_view(ms: Any, mission: Mission) -> dict[str, Any] | None:
    """rc14.5: what Undo will not put back, from PE's own record: the supervisor's note when the run ended (files the plan listed that
    could not be snapshotted, files the run was seen writing outside the checkpoint, and how many calls PE could not read for writes);
    before that, Core's note when the checkpoint was taken. None when there is nothing to say."""
    if not mission.checkpoint_id:
        return None
    for body in _recent_trusted_notes(ms, mission):
        if body.get("checkpoint") != mission.checkpoint_id:
            continue
        if body.get("event") == "checkpoint.not_covered":
            files, unread = body.get("files"), body.get("unread_calls")
        elif body.get("event") == "checkpoint.created":
            files, unread = body.get("not_covered"), 0
        else:
            continue
        items = [{"path": str(f.get("path"))[:300], "why": str(f.get("why") or "")[:200]}
                 for f in (files if isinstance(files, list) else [])[:40] if isinstance(f, dict) and f.get("path")]
        unread = unread if isinstance(unread, int) and not isinstance(unread, bool) and unread > 0 else 0
        return {"files": items, "unread_calls": unread} if items or unread else None
    return None


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
            "scan_note": (result.scan.error if result.scan is not None and result.scan.error else None),
            # rc14.7: new on the host as a whole after the run (a filesystem past its alert line): shown, not counted against the run
            "host_note": ([f.phrase() for f in result.scan.new_on_host[:5]]
                          if result.scan is not None and result.scan.new_on_host else None)}


def conversation_status(mission: Mission, rolling_back: bool = False, waiting: str | None = None) -> dict[str, Any] | None:
    """How a conversation reads while its latest confirmed run is going or needs attention, so an active repair is never hidden behind
    'Result ready'. None when the run has no say in the conversation's status."""
    phase = run_phase(mission, rolling_back, waiting)
    if phase.rail is None:
        return None
    return {"text": phase.rail, "level": phase.rail_level, "group": phase.group, "verdict": {"h": phase.verdict[0], "p": phase.verdict[1]}}


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


def can_undo(mission: Mission) -> bool:
    """A completed Repair with a checkpoint: the operator may undo it (the supervisor checks the checkpoint is still kept)."""
    return (mission.status in UNDO_STATES and getattr(mission, "tier", None) == "repair"
            and bool(getattr(mission, "checkpoint_id", None)))


def controls(mission: Mission, rolling_back: bool = False) -> dict[str, bool]:
    """Which actions apply now, from the run's phase: none while a rollback is running (the page shows that instead); a done run's
    undo is the rollback action while its checkpoint is kept."""
    phase = run_phase(mission, rolling_back)
    out = {name: name in phase.actions for name in ACTIONS}
    out["rollback"] = out["rollback"] or (phase.name == "done" and can_undo(mission))
    return out


NEXT_STEP_WORDS = {"rollback": "roll back", "recheck": "check it again", "keep": "keep the changes and close it"}


def thread_line(phase: Phase, mission: Mission, verification: dict[str, Any] | None, actor: str | None = None) -> str | None:
    """The line posted into the conversation when the run enters `phase`, naming only what the phase really offers. `actor` is who
    took the run into it (the kept line names the operator: it is the one line about it, wherever they pressed Keep)."""
    if phase.thread is None:
        return None
    if phase.name == "kept" and actor and actor.startswith("operator:"):
        return f"{actor.removeprefix('operator:')} kept what the run changed and closed it. PE's checks had not passed."
    if phase.name == "done":
        if verification:
            return f"Done and verified by PE: {verification['headline'].lower()}."
        return "The run finished, but PE has no verification result to show for this grant. Do not rely on it until it is checked."
    tail = f" {verification['headline']}." if verification else ""
    offered = [NEXT_STEP_WORDS[a] for a in phase.actions]
    if phase.name in ("held_unknown", "held_failed_checks"):
        offered.append("ask the agent to keep working on it")
    steps = (f" Look at the run above; you can {', '.join(offered[:-1])}{' or ' if len(offered) > 1 else ''}{offered[-1]}."
             if offered else "")
    return f"{phase.thread}.{tail}{steps}"


__all__ = ["ACTIONS", "PHASES", "Phase", "SETTLED", "run_phase", "thread_line", "rollback_running", "UNDO_STATES", "can_undo",
           "not_covered_view", "TERMINALISH", "check_state", "controls",
           "conversation_status", "repair_status", "rollback_view", "timeline", "verification_view"]
