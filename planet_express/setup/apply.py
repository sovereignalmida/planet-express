"""apply: run an approved plan, step by step, durably, and stop at the first thing that is not right.

Design and invariants: docs/designs/setup-apply.md. In short:

* **Bound, then re-checked.** The plan is rebuilt from fresh discovery and must have the same `plan_id`;
  if the host drifted since review, nothing runs. Each step then re-checks itself immediately before acting.
* **Journal first.** `step_started` is durable before any step can change anything, so a crash always
  leaves a record to reconcile from.
* **Outcome from the host.** After acting, the handler reads state back; an exit code is never the answer.
* **Stop on first failure.** Nothing after a failed step runs.
* **Resumable and idempotent.** Run again with the same plan: finished steps are skipped, a step that
  was in flight is reconciled from the host, a failed step is retried.

A `BaseException` (a simulated crash, a real `KeyboardInterrupt`) is deliberately not caught: it leaves
the journal exactly as a dead process would.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from planet_express.setup.handlers import HANDLERS, Context, Proceed, Refuse, Satisfied, StepFailure
from planet_express.setup.host import HostError, RealHost
from planet_express.setup.journal import Journal, make_redactor


class Busy(Exception):
    """Another apply holds the lock."""


@dataclass
class ApplyResult:
    status: str                      # done | stopped | refused | dry_run
    plan_id: str
    step: str | None = None
    reason: str | None = None
    checks: list[dict] = field(default_factory=list)


class _Lock:
    """An exclusive lock file in the (validated) journal root. Opened relative to the directory descriptor,
    never following a symlink, so a pre-planted file cannot redirect it."""

    def __init__(self, directory: Path, trusted_uids=frozenset({0})):
        self.path, self._trusted, self._fd = Path(directory) / "apply.lock", trusted_uids, None

    def __enter__(self):
        host = RealHost(self._trusted)
        directory = host.open_private_dir(str(self.path.parent), create=True)
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                self._fd = os.open("apply.lock", flags, 0o600, dir_fd=directory)
            except OSError as exc:
                raise HostError(f"cannot open {self.path}: {exc.strerror}") from exc
            st = os.fstat(self._fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid not in host.trusted_uids:
                os.close(self._fd)
                self._fd = None
                raise HostError(f"{self.path} is not a regular file owned by a trusted account")
        finally:
            os.close(directory)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise Busy(f"another apply holds {self.path}") from None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        return False


def _describe_drift(approved: dict, fresh: dict) -> str:
    def index(public):
        return {(s["kind"], s["target"]): json.dumps(s["params"], sort_keys=True) for s in public["steps"]}
    old, new = index(approved), index(fresh)
    notes = [f"new step: {k[0]} {k[1]}" for k in new if k not in old]
    notes += [f"step no longer planned: {k[0]} {k[1]}" for k in old if k not in new]
    notes += [f"changed: {k[0]} {k[1]}" for k in old if k in new and old[k] != new[k]]
    if not notes and approved.get("blocked") != fresh.get("blocked"):
        notes.append("the plan is blocked now: " + "; ".join(fresh.get("blocked", [])[:2]))
    return "; ".join(notes[:5]) or "the warnings or promises changed"


def apply(plan, *, host, journal_root: str | Path, replan, handlers=HANDLERS,
          evidence_ids: tuple[int, int] = (0, 0), evidence_dir: str | None = None,
          dry_run: bool = False, trusted_uids: frozenset[int] | set[int] = frozenset({0})) -> ApplyResult:
    """Run `plan`. `replan()` rebuilds a plan from fresh discovery and the same answers (the drift check).

    `evidence_dir` is where backups of replaced files go. It is on the host's filesystem (the host makes it),
    so it defaults to a directory beside the journal, which is the same filesystem in real use."""
    public = plan.to_public()
    plan_id = public["plan_id"]
    if not plan.applicable:
        return ApplyResult("refused", plan_id, reason="; ".join(plan.blocked))
    unsupported = sorted({s.kind for s in plan.steps if s.kind not in handlers})
    if unsupported and not dry_run:
        # Refused up front: a plan is never half-applied because the build cannot do its last step.
        return ApplyResult("refused", plan_id, reason=f"this build cannot apply: {', '.join(unsupported)}")

    if dry_run:
        # Read-only: no lock, no journal. The drift check is still done so the checks describe the plan reviewed.
        if replan().to_public()["plan_id"] != plan_id:
            return ApplyResult("refused", plan_id, reason="the host changed since the plan was reviewed; review the new plan")
        return _dry_run(plan, host, handlers, plan_id)

    root = Path(journal_root)
    try:
        with _Lock(root, trusted_uids):
            # Checked INSIDE the lock: two applies that start together must not both pass the check and then
            # take turns, the second running a plan that the first has already made stale.
            fresh_public = replan().to_public()
            if fresh_public["plan_id"] != plan_id:
                return ApplyResult("refused", plan_id,
                                   reason=f"the host changed since the plan was reviewed ({_describe_drift(public, fresh_public)}); "
                                          "review the new plan")
            return _run(plan, public, host, root, handlers, evidence_ids, evidence_dir, trusted_uids)
    except Busy as exc:
        return ApplyResult("refused", plan_id, reason=str(exc))
    except HostError as exc:
        return ApplyResult("refused", plan_id, reason=f"the journal directory is not trustworthy: {exc}")


def _dry_run(plan, host, handlers, plan_id) -> ApplyResult:
    """Run every step's read-only `check` and change nothing. Later steps may report that an earlier step
    must run first, which is expected: those earlier steps are not run here."""
    checks = []
    for step in plan.steps:
        if step.kind not in handlers:
            checks.append({"step": step.id, "kind": step.kind, "target": step.target,
                           "check": "not implemented", "detail": "this build cannot apply this kind of step yet"})
            continue
        ctx = Context(host, _NoJournal(), step, plan.secrets, "")
        outcome = handlers[step.kind].check(ctx)
        label = {Satisfied: "satisfied", Proceed: "would run", Refuse: "refused now"}[type(outcome)]
        checks.append({"step": step.id, "kind": step.kind, "target": step.target, "check": label,
                       "detail": getattr(outcome, "reason", "")})
    return ApplyResult("dry_run", plan_id, checks=checks)


class _NoJournal:
    """Checks are read-only and must not write anywhere; a handler's `check` that tries to is a bug."""

    def append(self, *args, **kwargs):
        raise AssertionError("a check must not write to the journal")

    def steps(self):
        return {}


def _run(plan, public, host, root, handlers, evidence_ids, evidence_dir, trusted_uids) -> ApplyResult:
    plan_id = public["plan_id"]
    journal = Journal(root, plan_id, redact=make_redactor(plan.secrets), trusted_uids=trusted_uids)
    journal.save_plan(public)
    # Backups of replaced files go here. It is created only when a backup is first needed, so a run in which
    # everything is already satisfied changes nothing on the host.
    evidence_dir = evidence_dir or f"{journal.directory}/evidence"
    journal.append("apply_started", steps=len(plan.steps))

    def stop(step_id, reason, effect="not_applied"):
        journal.append("step_failed", step=step_id, effect=effect, reason=reason)
        journal.append("stopped", step=step_id, reason=reason)
        return ApplyResult("stopped", plan_id, step=step_id, reason=reason)

    for step in plan.steps:
        record = journal.steps().get(step.id)
        if record is not None and record.status == "ok":
            continue                                           # finished in an earlier run
        handler = handlers[step.kind]
        ctx = Context(host, journal, step, plan.secrets, evidence_dir, evidence_ids)

        if record is not None and record.status == "started":
            # In flight when the last run died: ask the host, do not assume.
            try:
                effect = handler.reconcile(ctx)
            except (HostError, StepFailure) as exc:
                return stop(step.id, f"could not tell what happened before the crash: {exc}", "unknown")
            journal.append("reconciled", step=step.id, effect=effect)
            if effect == "applied":
                journal.append("step_ok", step=step.id, effect="applied", reconciled=True)
                continue
            if effect == "unknown":
                return stop(step.id, "the previous run was interrupted and the host is in a state this step "
                                     "cannot classify; inspect it before retrying", "unknown")

        journal.append("step_started", step=step.id, kind=step.kind, target=step.target)
        try:
            outcome = handler.check(ctx)
            if isinstance(outcome, Refuse):
                return stop(step.id, outcome.reason)
            if isinstance(outcome, Satisfied):
                journal.append("step_ok", step=step.id, effect="not_applied", satisfied=True, reason=outcome.reason)
                continue
            handler.act(ctx)
            effect = handler.verify(ctx)
        except StepFailure as exc:
            return stop(step.id, exc.reason, exc.effect)
        except HostError as exc:
            return stop(step.id, str(exc), "unknown")
        except Exception as exc:                               # noqa: BLE001 -- a bug is a stop, never a silent pass
            return stop(step.id, f"unexpected {type(exc).__name__}: {exc}", "unknown")
        if effect == "applied" or (effect == "not_applied" and step.risk == "R0"):
            journal.append("step_ok", step=step.id, effect=effect)
        else:
            return stop(step.id, f"the step ran but the host does not show the expected result ({effect})", effect)

    journal.append("done")
    return ApplyResult("done", plan_id)
