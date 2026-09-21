"""Policy-checked config editing and activation under the host-mutation lock."""

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config_io import ConfigConflict, validate_config_text, write_config_text

log = logging.getLogger("planetexpress.config")

MAX_CONFIG_BYTES = 256 * 1024
EDITABLE_FIELDS = frozenset({"paused_containers", "exclude_services", "backup_jobs"})
SENSITIVE_FIELDS = frozenset({"sudo_allowlist", "forbidden_stacks", "autonomy"})
_MUTATION_OWNER = "config-apply"
REPLY_WAIT_SECONDS = 30
NOTIFY_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[dict]
    changed_fields: list[str]
    locked_fields: list[str]


@dataclass(frozen=True)
class ApplyResult:
    status: str
    errors: list[dict]
    reason: str
    changed_fields: list[str]
    locked_fields: list[str]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "errors": self.errors,
            "reason": self.reason,
            "changed_fields": self.changed_fields,
            "locked_fields": self.locked_fields,
        }


def _draft_bytes(text: str) -> tuple[bytes | None, list[dict]]:
    if not isinstance(text, str):
        return None, [{"loc": "", "msg": "Config text must be a string"}]
    if "\0" in text:
        return None, [{"loc": "", "msg": "Config text must not contain NUL bytes"}]
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        return None, [{"loc": "", "msg": "Config text must be valid UTF-8"}]
    if len(data) > MAX_CONFIG_BYTES:
        return None, [{"loc": "", "msg": "Config text exceeds the 256 KiB limit"}]
    return data, []


def _read_config(path: Path) -> tuple[str, bytes]:
    with path.open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("Live config exceeds the 256 KiB limit")
    if b"\0" in data:
        raise ValueError("Live config contains NUL bytes")
    try:
        return data.decode("utf-8"), data
    except UnicodeError as exc:
        raise ValueError("Live config is not valid UTF-8") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ConfigService:
    def __init__(
        self,
        state,
        *,
        config_path: Path,
        activate: Callable[[], None],
        store,
        sensitive_edits_enabled: bool = False,
        notify: Callable[[str], None] | None = None,
        loaded_sha256: str | None = None,
    ):
        self._state = state
        self._config_path = config_path
        self._activate = activate
        self._store = store
        self._sensitive_edits_enabled = sensitive_edits_enabled
        self._notify = notify
        # SHA-256 of the config bytes this process actually loaded. The file's own sha changes
        # at write time, before the re-exec, so only this says whether a change is active yet.
        self._loaded_sha256 = loaded_sha256

    @property
    def sensitive_edits_enabled(self) -> bool:
        return self._sensitive_edits_enabled

    def get(self) -> dict:
        text, data = _read_config(self._config_path)
        return {
            "text": text,
            "sha256": _sha256(data),
            "loaded_sha256": self._loaded_sha256,
            "path": str(self._config_path),
            "sensitive_edits_enabled": self._sensitive_edits_enabled,
            "editable_fields": sorted(EDITABLE_FIELDS),
            "sensitive_fields": sorted(SENSITIVE_FIELDS),
        }

    def _validate_against(self, text: str, live_text: str) -> ValidationResult:
        _data, errors = _draft_bytes(text)
        if errors:
            return ValidationResult(False, errors, [], [])
        draft, errors = validate_config_text(text)
        if errors:
            return ValidationResult(False, errors, [], [])
        live, live_errors = validate_config_text(live_text)
        if live_errors:
            return ValidationResult(
                False, [{"loc": "", "msg": "Live config is invalid"}], [], []
            )
        draft_values = draft.model_dump(mode="json")
        live_values = live.model_dump(mode="json")
        changed = sorted(name for name in draft_values if draft_values[name] != live_values[name])
        locked = sorted(
            name for name in changed
            if name not in EDITABLE_FIELDS
            and not (name in SENSITIVE_FIELDS and self._sensitive_edits_enabled)
        )
        return ValidationResult(not locked, [], changed, locked)

    def validate(self, text: str) -> ValidationResult:
        _data, errors = _draft_bytes(text)
        if errors:
            return ValidationResult(False, errors, [], [])
        try:
            live_text, _ = _read_config(self._config_path)
        except (OSError, ValueError) as exc:
            return ValidationResult(False, [{"loc": "", "msg": str(exc)}], [], [])
        return self._validate_against(text, live_text)

    @staticmethod
    def _locked_reason(locked_fields: list[str]) -> str:
        if any(name not in SENSITIVE_FIELDS for name in locked_fields):
            return "These fields change paths or host wiring; edit on the host."
        return "Sensitive config edits require PE_ALLOW_SENSITIVE_CONFIG_EDITS=1 at core startup."

    def _refused(
        self,
        operator: str,
        status: str,
        *,
        errors: list[dict] | None = None,
        reason: str = "",
        changed_fields: list[str] | None = None,
        locked_fields: list[str] | None = None,
    ) -> ApplyResult:
        changed_fields = changed_fields or []
        locked_fields = locked_fields or []
        self._store.record_event(
            "config.apply_refused",
            operator=operator,
            status=status,
            changed_fields=changed_fields,
            locked_fields=locked_fields,
        )
        return ApplyResult(status, errors or [], reason, changed_fields, locked_fields)

    def apply(self, text: str, *, base_sha256: str, operator: str) -> ApplyResult:
        draft_data, errors = _draft_bytes(text)
        if errors:
            return self._refused(operator, "invalid", errors=errors)
        _draft, errors = validate_config_text(text)
        if errors:
            return self._refused(operator, "invalid", errors=errors)
        if not self._state.try_begin_mutation(_MUTATION_OWNER, require_idle=True):
            return self._refused(operator, "busy", reason=self._state.busy_reason)

        keep_lock = False
        try:
            try:
                live_text, live_data = _read_config(self._config_path)
            except ValueError as exc:
                return self._refused(
                    operator, "invalid", errors=[{"loc": "", "msg": str(exc)}]
                )
            except OSError as exc:
                return self._refused(operator, "write_failed", reason=str(exc))

            validation = self._validate_against(text, live_text)
            if validation.errors:
                return self._refused(operator, "invalid", errors=validation.errors)
            before_sha256 = _sha256(live_data)
            if before_sha256 != base_sha256:
                return self._refused(
                    operator,
                    "conflict",
                    reason="Config changed since it was loaded.",
                    changed_fields=validation.changed_fields,
                    locked_fields=validation.locked_fields,
                )
            if validation.locked_fields:
                return self._refused(
                    operator,
                    "locked",
                    reason=self._locked_reason(validation.locked_fields),
                    changed_fields=validation.changed_fields,
                    locked_fields=validation.locked_fields,
                )
            if live_data == draft_data:
                return self._refused(operator, "unchanged")
            try:
                write_config_text(text, self._config_path, expected_sha256=before_sha256)
            except ConfigConflict:
                return self._refused(
                    operator,
                    "conflict",
                    reason="Config changed since it was loaded.",
                    changed_fields=validation.changed_fields,
                )
            except OSError as exc:
                # The error may come after the rename (e.g. the directory fsync): if the draft is
                # already the file on disk, it must still be activated, or core would enforce a
                # policy the file no longer states (Codex review round 4, T35).
                if not self._file_is(draft_data):
                    return self._refused(
                        operator,
                        "write_failed",
                        reason=str(exc),
                        changed_fields=validation.changed_fields,
                    )
                log.warning("Config replaced but not fully synced; activating anyway")

            after_sha256 = _sha256(draft_data)
            # The new file is already on disk: from here on, nothing may stop activation, or
            # core would keep running on a config that no longer matches it (Codex review, T35).
            try:
                self._store.record_event(
                    "config.applied",
                    operator=operator,
                    changed_fields=validation.changed_fields,
                    before_sha256=before_sha256,
                    after_sha256=after_sha256,
                )
            except Exception:  # noqa: BLE001 -- audit is best-effort once the write happened
                log.warning("Failed to record config.applied; activating anyway")
            reply_written = threading.Event()
            try:
                thread = threading.Thread(
                    target=self._activate_after_reply,
                    args=(reply_written, operator, validation.changed_fields, before_sha256, after_sha256),
                    daemon=True,
                    name="config-activation",
                )
                thread.start()
            except Exception:  # noqa: BLE001 -- e.g. thread exhaustion
                # Nothing will activate the new file, so put the old one back: disk must match
                # the policy core is actually enforcing (Codex review round 2, T35).
                return self._restore_after_failed_start(
                    live_text, operator, validation.changed_fields, before_sha256, after_sha256
                )
            keep_lock = True
            result = ApplyResult("activating", [], "", validation.changed_fields, [])
            # The RPC adapter consumes this callback; implementation details never cross the socket.
            object.__setattr__(result, "_post_reply", reply_written.set)
            return result
        finally:
            if not keep_lock:
                self._state.end_mutation(_MUTATION_OWNER)

    def _file_is(self, data: bytes) -> bool:
        try:
            return self._config_path.read_bytes() == data
        except OSError:
            return False

    def _restore_after_failed_start(
        self, live_text: str, operator: str, changed_fields: list[str],
        before_sha256: str, after_sha256: str,
    ) -> ApplyResult:
        try:
            write_config_text(live_text, self._config_path)
            reason = "Could not start activation; the previous config was restored. Nothing changed."
        except Exception:  # noqa: BLE001
            reason = ("Could not start activation or restore the previous config: the new config is "
                      "on disk but not active. Restart core to apply it.")
        log.warning(reason)
        try:
            self._store.record_event(
                "config.activation_failed", operator=operator, changed_fields=changed_fields,
                before_sha256=before_sha256, after_sha256=after_sha256, reason=reason,
            )
        except Exception:  # noqa: BLE001 -- the result below still tells the operator
            log.warning("Failed to record config.activation_failed")
        return ApplyResult("activation_failed", [], reason, changed_fields, [])

    def _activate_after_reply(
        self,
        reply_written: threading.Event,
        operator: str,
        changed_fields: list[str],
        before_sha256: str,
        after_sha256: str,
    ) -> None:
        # Bounded: if the caller never signals (an adapter error between apply() returning
        # and the reply being written), activate anyway rather than hold the lock forever.
        if not reply_written.wait(timeout=REPLY_WAIT_SECONDS):
            log.warning("Config activation did not see the reply written; activating anyway")
        try:
            self._notify_best_effort(operator, changed_fields)
            self._activate()
        except Exception:  # noqa: BLE001 -- re-exec failure must leave the core available
            log.warning("Config activation failed")
            self._store.record_event(
                "config.activation_failed",
                operator=operator,
                changed_fields=changed_fields,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                reason="The new config is on disk; core is still using the old in-memory config.",
            )
        finally:
            self._state.end_mutation(_MUTATION_OWNER)

    def _notify_best_effort(self, operator: str, changed_fields: list[str]) -> None:
        if self._notify is None:
            return
        fields = ", ".join(changed_fields) if changed_fields else "formatting only"
        notice = f"⚙️ Config changed by {operator}: {fields}. Core is restarting to apply it."
        try:
            notification = threading.Thread(
                target=self._send_notification,
                args=(notice,),
                daemon=True,
                name="config-notification",
            )
            notification.start()
        except Exception:  # noqa: BLE001 -- a notice must never block activation
            log.warning("Could not start the config activation notification")
            return
        notification.join(timeout=NOTIFY_TIMEOUT_SECONDS)
        if notification.is_alive():
            log.warning("Config activation notification timed out")

    def _send_notification(self, notice: str) -> None:
        try:
            self._notify(notice)
        except Exception:  # noqa: BLE001 -- exception text can contain the Telegram bot token
            log.warning("Failed to send config activation notification")
