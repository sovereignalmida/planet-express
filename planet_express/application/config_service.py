"""Draft validation and config activation under the host-mutation lock."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config_io import validate_config_text, write_config_text


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[dict]


@dataclass(frozen=True)
class ApplyResult:
    status: str  # invalid | busy | activating | write_failed
    errors: list[dict]
    reason: str


class ConfigService:
    def __init__(self, state, *, config_path: Path, activate: Callable[[], None]):
        self._state = state
        self._config_path = config_path
        self._activate = activate

    def validate(self, text: str) -> ValidationResult:
        _, errors = validate_config_text(text)
        return ValidationResult(not errors, errors)

    def apply(self, text: str) -> ApplyResult:
        result = self.validate(text)
        if not result.ok:
            return ApplyResult("invalid", result.errors, "")
        if not self._state.try_begin_mutation("config-apply", require_idle=True):
            return ApplyResult("busy", [], self._state.busy_reason)
        try:
            try:
                write_config_text(text, self._config_path)
            except OSError as exc:
                return ApplyResult("write_failed", [], str(exc))
            self._activate()
            return ApplyResult("activating", [], "")
        finally:
            self._state.end_mutation("config-apply")
