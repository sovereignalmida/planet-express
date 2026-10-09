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


class SetupSession:
    def __init__(self, *, discover_fn: Callable[[], dict], plan_fn: Callable, repo_root: str | None = None):
        self._discover, self._plan, self.repo_root = discover_fn, plan_fn, repo_root
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
