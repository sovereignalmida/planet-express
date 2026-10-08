"""undo: put back what an `apply` changed, from the evidence it journaled, and refuse the moment anything has moved.

Newest step first. Each step's own `inverse` decides, from the journal and the host (never from memory), whether
what it changed is still exactly what it left: a file somebody edited since, a directory that is no longer empty,
an inode that is not the one setup made, all stop the undo with a reason and a list of what remains. Nothing is
ever reverted over work that is not setup's.

Steps with nothing to revert, or that cannot be reverted (accounts, the safety-net snapshot), are named in the
result and do not stop it. Undo is resumable: a step already undone is skipped, and every inverse is written to be
repeated safely after a crash.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from planet_express.setup.apply import Busy, _Lock
from planet_express.setup.handlers import HANDLERS, Context, Irreversible, StepFailure
from planet_express.setup.host import HostError
from planet_express.setup.journal import Journal


@dataclass
class UndoResult:
    status: str                                  # done | stopped | refused
    plan_id: str
    step: str | None = None
    reason: str | None = None
    undone: list[dict] = field(default_factory=list)       # {"step", "detail"}
    not_undone: list[dict] = field(default_factory=list)   # {"step", "reason"}: named, not a failure
    remaining: list[str] = field(default_factory=list)     # step ids still to undo after a stop


def undo(plan, *, host, journal_root: str | Path, handlers=HANDLERS, evidence_ids: tuple[int, int] = (0, 0),
         evidence_dir: str | None = None, trusted_uids=frozenset({0})) -> UndoResult:
    plan_id = plan.to_public()["plan_id"]
    try:
        with _Lock(Path(journal_root), trusted_uids):
            journal = Journal(Path(journal_root), plan_id, trusted_uids=trusted_uids)
            if not any(e["type"] == "apply_started" for e in journal.events()):
                return UndoResult("refused", plan_id, reason="this plan was never applied on this host, so there is nothing to undo")
            evidence_dir = evidence_dir or f"{journal.directory}/evidence"
            return _undo(plan, plan_id, host, journal, handlers, evidence_ids, evidence_dir)
    except Busy as exc:
        return UndoResult("refused", plan_id, reason=str(exc))
    except HostError as exc:
        return UndoResult("refused", plan_id, reason=f"the journal directory is not trustworthy: {exc}")


def _undo(plan, plan_id, host, journal, handlers, evidence_ids, evidence_dir) -> UndoResult:
    result = UndoResult("done", plan_id)
    journal.append("undo_started")
    records = journal.steps()
    pending = []
    for step in reversed(plan.steps):
        record = records.get(step.id)
        if record is None or record.undone or (record.status == "ok" and record.satisfied):
            continue                       # never ran, already dealt with, or setup found it already in place
        pending.append(step)
    for index, step in enumerate(pending):
        handler = handlers[step.kind]
        ctx = Context(host, journal, step, {}, evidence_dir, evidence_ids)
        try:
            if not hasattr(handler, "inverse"):
                raise Irreversible("this kind of step has no undo")
            detail = handler.inverse(ctx)
        except Irreversible as exc:
            journal.append("step_not_undone", step=step.id, reason=exc.reason)
            result.not_undone.append({"step": step.id, "reason": exc.reason})
            continue
        except (StepFailure, HostError) as exc:
            reason = exc.reason if isinstance(exc, StepFailure) else str(exc)
            journal.append("undo_refused", step=step.id, reason=reason)
            result.status, result.step, result.reason = "stopped", step.id, reason
            result.remaining = [s.id for s in pending[index:]]
            return result
        journal.append("step_undone", step=step.id, detail=detail)
        result.undone.append({"step": step.id, "detail": detail})
    journal.append("undo_done")
    return result
