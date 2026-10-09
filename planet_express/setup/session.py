"""What the setup server remembers between requests: the discovery report, the answers so far, the reviewed plan.

One session per run, in memory. Answers are validated as a whole by `SetupAnswers` before they are kept, so
the stored set is always one `plan()` accepts; a rejected update changes nothing. Secrets are held here and
nowhere else; every view that leaves this module (`public_answers`, the plan) is masked.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from planet_express.setup.answers import SetupAnswers

# The only keys a browser may set. Anything else is refused by name, never merged.
SETTABLE = frozenset({"story", "run_as", "run_group", "accept_root_service", "install_dir", "stacks_root",
                      "ignored_stacks", "tier", "sudo_units", "dashboard_port", "telegram", "llm", "operator",
                      "start_services"})
SECRET_KEYS = ("telegram", "llm", "operator")


class Conflict(Exception):
    """The request is valid but not possible right now (a run is in flight, the plan is stale, ...)."""


class SetupSession:
    def __init__(self, *, discover_fn: Callable[[], dict], plan_fn: Callable, repo_root: str | None = None,
                 apply_fn: Callable | None = None, undo_fn: Callable | None = None,
                 journal_fn: Callable | None = None, cannot_apply: str | None = None):
        """`apply_fn(plan, replan)` and `undo_fn(plan)` run the real thing and return an ApplyResult / UndoResult;
        `journal_fn(plan_id)` opens that plan's journal for reading. `cannot_apply` is why this process may not
        change the host (not root), shown instead of an Install button."""
        self._discover, self._plan, self.repo_root = discover_fn, plan_fn, repo_root
        self._apply_fn, self._undo_fn, self._journal_fn = apply_fn, undo_fn, journal_fn
        self.cannot_apply = cannot_apply if apply_fn is not None else (cannot_apply or "this server was started without an executor")
        self.phase = "idle"                       # idle | applying | done | stopped | refused | undoing | undone | undo_stopped
        self.outcome: dict | None = None          # the last apply or undo result, public fields only
        self.applied_plan = None                  # the plan object a run used (kept for retry and undo)
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.discovery: dict | None = None
        self.answers: dict = {}
        self.reviewed = None                      # the Plan object the person was shown (secrets inside)

    # -- discovery ---------------------------------------------------------------------------------------------
    def run_discover(self) -> dict:
        with self._lock:
            self.discovery = self._discover()
            self.reviewed = None
            for key, value in self._suggest(self.discovery).items():
                self.answers.setdefault(key, value)
            return self.discovery

    def _suggest(self, found: dict) -> dict:
        existing = found.get("existing_pe") or {}
        roots = found.get("stacks_roots") or []
        install = existing.get("install_dir") or self.repo_root or str(Path(__file__).resolve().parents[2])
        run_as = os.environ.get("SUDO_USER") or ("root" if os.geteuid() == 0 else None) or os.environ.get("USER") or "root"
        stacks = roots[0]["path"] if roots else f"{install}/stacks"
        return {"story": "adopt" if existing.get("installed") or roots else "fresh", "run_as": run_as,
                "install_dir": install, "stacks_root": stacks, "tier": "observe"}

    # -- answers -----------------------------------------------------------------------------------------------
    def set_answers(self, partial: dict) -> dict:
        """Merge `partial` if the result is valid. Returns {"ok", "errors", "answers"}; never echoes a secret."""
        unknown = sorted(set(partial) - SETTABLE)
        if unknown:
            return {"ok": False, "errors": [{"field": k, "message": "not a setting"} for k in unknown],
                    "answers": self.public_answers()}
        with self._lock:
            if self.discovery is None:
                self.run_discover()                               # the defaults for required answers come from it
            candidate = {**self.answers, **{k: v for k, v in partial.items() if v is not None}}
            for key in partial:                                   # an explicit null clears an optional answer
                if partial[key] is None:
                    candidate.pop(key, None)
            try:
                SetupAnswers.model_validate(candidate)
            except ValidationError as exc:
                errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                          for e in exc.errors(include_input=False, include_url=False, include_context=False)]
                return {"ok": False, "errors": errors, "answers": self.public_answers()}
            self.answers = candidate
            self.reviewed = None                                  # a changed answer invalidates what was reviewed
            return {"ok": True, "errors": [], "answers": self.public_answers()}

    def public_answers(self) -> dict:
        view = {k: v for k, v in self.answers.items() if k not in SECRET_KEYS}
        view["telegram_set"] = "telegram" in self.answers
        view["llm"] = {"provider": self.answers["llm"].get("provider"), "key_set": bool(self.answers["llm"].get("api_key"))} \
            if "llm" in self.answers else None
        view["operator_set"] = "operator" in self.answers
        return view

    # -- plan --------------------------------------------------------------------------------------------------
    def build_plan(self) -> dict:
        """The public plan for the current answers (secrets masked), or the reason there is none."""
        with self._lock:
            if self.discovery is None:
                self.run_discover()
            try:
                answers = SetupAnswers.model_validate(self.answers)
            except ValidationError as exc:
                fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors(include_input=False)})
                return {"error": f"the answers are incomplete or invalid: {', '.join(fields)}"}
            self.reviewed = self._plan(self.discovery, answers, repo_root=self.repo_root)
            return self.reviewed.to_public()


    # -- running ---------------------------------------------------------------------------------------------------
    def _replan(self):
        """A fresh plan from fresh discovery and the current answers: what `apply` compares ids against."""
        return self._plan(self._discover(), SetupAnswers.model_validate(self.answers), repo_root=self.repo_root)

    def _begin(self, phase: str, target) -> None:
        self.phase, self.outcome = phase, None
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def approve(self, plan_id: str) -> None:
        """Start applying the plan the person was shown. Refused unless it is still exactly that plan."""
        with self._lock:
            if self.cannot_apply:
                raise Conflict(self.cannot_apply)
            if self.phase in ("applying", "undoing"):
                raise Conflict("a run is already in progress")
            if self.phase in ("done", "undone"):
                raise Conflict("this plan has already been applied")
            if self.reviewed is None:
                raise Conflict("review the plan again: the answers changed since it was built")
            if self.reviewed.to_public()["plan_id"] != plan_id:
                raise Conflict("that is not the plan you were shown; review the current one")
            if not self.reviewed.applicable:
                raise Conflict("the plan is blocked: " + "; ".join(self.reviewed.blocked))
            self.applied_plan = self.reviewed
            plan = self.applied_plan
            self._begin("applying", lambda: self._finish("applying", lambda: self._apply_fn(plan, self._replan)))

    def retry(self, step: str | None) -> None:
        """Run the same plan again: finished steps are skipped, the one that failed is checked and retried."""
        with self._lock:
            if self.phase not in ("stopped", "refused"):
                raise Conflict("there is nothing to retry")
            failed = (self.outcome or {}).get("step")
            if step and failed and step != failed:
                raise Conflict(f"the step that stopped is {failed}")
            plan = self.applied_plan
            self._begin("applying", lambda: self._finish("applying", lambda: self._apply_fn(plan, self._replan)))

    def undo(self) -> None:
        with self._lock:
            if self.cannot_apply or self._undo_fn is None:
                raise Conflict(self.cannot_apply or "undo is not available")
            if self.phase in ("applying", "undoing"):
                raise Conflict("a run is already in progress")
            if self.applied_plan is None:
                raise Conflict("nothing has been applied")
            plan = self.applied_plan
            self._begin("undoing", lambda: self._finish("undoing", lambda: self._undo_fn(plan)))

    def _finish(self, kind: str, run) -> None:
        try:
            result = run()
            status, detail = result.status, {k: getattr(result, k) for k in
                                             ("status", "plan_id", "step", "reason", "undone", "not_undone", "remaining")
                                             if hasattr(result, k)}
        except Exception as exc:                                      # noqa: BLE001 -- a bug is a stop, never a hang
            status, detail = "stopped", {"status": "stopped", "reason": f"unexpected {type(exc).__name__}"}
        with self._lock:
            if kind == "applying":
                self.phase = status if status in ("done", "stopped", "refused") else "stopped"
            else:
                self.phase = "undone" if status == "done" else "undo_stopped"
            self.outcome = detail

    def wait(self, timeout: float = 10.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def progress(self, after: int = 0) -> dict:
        """Where the run is, rebuilt from the journal (so a reload or a restarted browser sees the truth)."""
        with self._lock:
            phase, outcome, plan = self.phase, self.outcome, self.applied_plan
        if plan is None or self._journal_fn is None:
            return {"phase": phase, "outcome": outcome, "steps": [], "events": [], "seq": after}
        public = plan.to_public()
        journal = self._journal_fn(public["plan_id"])
        records = journal.steps()
        steps = []
        for step in public["steps"]:
            record = records.get(step["id"])
            steps.append({"id": step["id"], "title": step["title"], "target": step["target"], "risk": step["risk"],
                          "status": ("undone" if record and record.undone else record.status) if record else "pending",
                          "effect": record.effect if record else None, "reason": record.reason if record else None,
                          "satisfied": bool(record and record.satisfied), "undo_note": record.undo_note if record else None})
        snapshot = journal.events(after)                    # read once: events and seq must come from the same view
        events = [e for e in snapshot if e["type"] in ("log", "step_started", "step_ok", "step_failed",
                                                                    "stopped", "done", "step_undone", "step_not_undone",
                                                                    "undo_refused", "undo_done")]
        seq = max([after] + [e["seq"] for e in snapshot])
        return {"phase": phase, "outcome": outcome, "plan_id": public["plan_id"], "steps": steps, "events": events, "seq": seq}
