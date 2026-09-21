import hashlib
import sys
import threading
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.application import config_service

ConfigService = config_service.ConfigService


BASE = {
    "stacks_root": "/srv/stacks",
    "forbidden_stacks": ["infra"],
    "paused_containers": [],
    "backup_jobs": ["weekly"],
    "mounts": {"data.mount": "/data"},
    "exclude_services": [],
    "lan_only_domain": "casalan.com",
    "autonomy": {"direct_request_risks": ["R1"], "forbidden_risks": ["R4"],
                 "cooldown_seconds": 1800, "max_attempts_per_day": 3},
    "sudo_allowlist": {"units": [], "globs": []},
}


class State:
    busy_reason = "scan running"

    def __init__(self, busy=False):
        self.busy = busy
        self.owner = None
        self.attempts = []

    def try_begin_mutation(self, owner, *, require_idle):
        self.attempts.append((owner, require_idle))
        if self.busy or self.owner is not None:
            return False
        self.owner = owner
        return True

    def end_mutation(self, owner):
        assert self.owner == owner
        self.owner = None


class Store:
    def __init__(self):
        self.events = []

    def record_event(self, kind, **payload):
        self.events.append((kind, payload))


def dump(value=BASE):
    return yaml.safe_dump(value, sort_keys=False)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def make_service(tmp_path, *, sensitive=False, busy=False, activate=None, notify=None):
    path = tmp_path / "config.yaml"
    live = dump()
    path.write_text(live)
    state = State(busy=busy)
    store = Store()
    activated = threading.Event()

    def default_activate():
        activated.set()

    service = ConfigService(
        state, config_path=path, activate=activate or default_activate, store=store,
        sensitive_edits_enabled=sensitive, notify=notify,
    )
    return service, path, state, store, activated, live


def apply_and_activate(service, text, live, operator="alice"):
    result = service.apply(text, base_sha256=digest(live), operator=operator)
    if result.status == "activating":
        result._post_reply()
    return result


@pytest.mark.parametrize(("field", "value"), [
    ("paused_containers", ["sleepy"]),
    ("exclude_services", [{"stack": "media", "service": "worker"}]),
    ("backup_jobs", ["daily"]),
])
def test_editable_field_applies(field, value, tmp_path):
    service, path, state, store, activated, live = make_service(tmp_path)
    draft = BASE | {field: value}
    result = apply_and_activate(service, dump(draft), live)
    assert result.status == "activating"
    assert result.changed_fields == [field]
    assert activated.wait(1)
    assert state.owner is None
    assert yaml.safe_load(path.read_text())[field] == value
    assert store.events[0][0] == "config.applied"


@pytest.mark.parametrize(("field", "value"), [
    ("sudo_allowlist", {"units": [{"unit": "data.mount", "actions": ["start"]}], "globs": []}),
    ("forbidden_stacks", ["infra", "secrets"]),
    ("autonomy", BASE["autonomy"] | {"cooldown_seconds": 60}),
])
def test_sensitive_fields_require_switch(field, value, tmp_path):
    draft = dump(BASE | {field: value})
    service, path, _, _, _, live = make_service(tmp_path)
    result = service.apply(draft, base_sha256=digest(live), operator="alice")
    assert result.status == "locked" and result.locked_fields == [field]
    assert path.read_text() == live

    service, path, _, _, activated, live = make_service(tmp_path, sensitive=True)
    result = apply_and_activate(service, draft, live)
    assert result.status == "activating" and activated.wait(1)
    assert path.read_text() == draft


@pytest.mark.parametrize(("field", "value"), [
    ("stacks_root", "/elsewhere"), ("mounts", {"other.mount": "/other"}),
    ("lan_only_domain", "internal.example"),
])
@pytest.mark.parametrize("sensitive", [False, True])
def test_host_wiring_is_always_locked(field, value, sensitive, tmp_path):
    service, path, _, _, _, live = make_service(tmp_path, sensitive=sensitive)
    result = service.apply(dump(BASE | {field: value}), base_sha256=digest(live), operator="alice")
    assert result.status == "locked" and result.locked_fields == [field]
    assert "edit on the host" in result.reason
    assert path.read_text() == live


def test_mixed_editable_and_locked_is_atomic(tmp_path):
    service, path, _, _, _, live = make_service(tmp_path)
    draft = dump(BASE | {"paused_containers": ["sleepy"], "mounts": {}})
    result = service.apply(draft, base_sha256=digest(live), operator="alice")
    assert result.status == "locked"
    assert result.changed_fields == ["mounts", "paused_containers"]
    assert path.read_text() == live


def test_comment_only_applies_and_identical_is_unchanged(tmp_path):
    service, path, _, _store, activated, live = make_service(tmp_path)
    comment = "# operator note\n" + live
    result = apply_and_activate(service, comment, live)
    assert result.status == "activating" and result.changed_fields == [] and activated.wait(1)
    assert path.read_text() == comment

    service, path, state, _store, activated, live = make_service(tmp_path)
    result = service.apply(live, base_sha256=digest(live), operator="alice")
    assert result.status == "unchanged"
    assert state.owner is None and not activated.is_set() and path.read_text() == live


def test_conflict_classifies_against_reread_file(tmp_path):
    service, path, _, _, _, original = make_service(tmp_path)
    current = dump(BASE | {"paused_containers": ["current"]})
    path.write_text(current)
    draft = dump(BASE | {"paused_containers": ["current"], "backup_jobs": ["daily"]})
    result = service.apply(draft, base_sha256=digest(original), operator="alice")
    assert result.status == "conflict"
    assert result.changed_fields == ["backup_jobs"]
    assert path.read_text() == current


@pytest.mark.parametrize("draft", ["x" * (256 * 1024 + 1), "stacks_root: /srv\0x", "\udcff"])
def test_invalid_bounds_encoding_and_nul_leave_file_unchanged(tmp_path, draft):
    service, path, state, _, _, live = make_service(tmp_path)
    result = service.apply(draft, base_sha256=digest(live), operator="alice")
    assert result.status == "invalid" and result.errors
    assert state.attempts == [] and path.read_text() == live


def test_busy_and_write_failure_release_lock(tmp_path, monkeypatch):
    service, path, state, _, _, live = make_service(tmp_path, busy=True)
    result = service.apply(dump(BASE | {"backup_jobs": ["daily"]}),
                           base_sha256=digest(live), operator="alice")
    assert result.status == "busy" and path.read_text() == live

    service, path, state, _, _, live = make_service(tmp_path)
    monkeypatch.setattr(config_service, "write_config_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    result = service.apply(dump(BASE | {"backup_jobs": ["daily"]}),
                           base_sha256=digest(live), operator="alice")
    assert result.status == "write_failed" and result.reason == "disk full"
    assert state.owner is None and path.read_text() == live


def test_lock_is_held_until_reply_then_activation_failure_is_audited(tmp_path):
    attempted = threading.Event()

    def fail_activate():
        attempted.set()
        raise RuntimeError("token=secret")

    notices = []
    service, path, state, store, _, live = make_service(
        tmp_path, activate=fail_activate, notify=notices.append
    )
    draft = dump(BASE | {"backup_jobs": ["daily"]})
    result = service.apply(draft, base_sha256=digest(live), operator="alice")
    assert result.status == "activating" and state.owner == "config-apply"
    assert not attempted.wait(0.05)
    result._post_reply()
    assert attempted.wait(1)
    for _ in range(100):
        if state.owner is None:
            break
        threading.Event().wait(0.01)
    assert state.owner is None
    failure = next(payload for kind, payload in store.events if kind == "config.activation_failed")
    assert "new config is on disk" in failure["reason"].lower()
    assert notices == ["⚙️ Config changed by alice: backup_jobs. Core is restarting to apply it."]
    assert path.read_text() == draft


def test_events_never_include_config_text(tmp_path):
    service, _, _, store, activated, live = make_service(tmp_path)
    draft = "# SECRET_SENTINEL\n" + dump(BASE | {"backup_jobs": ["daily"]})
    apply_and_activate(service, draft, live)
    assert activated.wait(1)
    service.apply("[]", base_sha256=digest(draft), operator="alice")
    assert {kind for kind, _ in store.events} >= {"config.applied", "config.apply_refused"}
    assert "SECRET_SENTINEL" not in repr(store.events)


def _wait_released(state):
    for _ in range(200):
        if state.owner is None:
            return True
        threading.Event().wait(0.01)
    return False


def test_audit_failure_after_write_still_activates(tmp_path):
    # Codex review, T35: the file is already replaced, so a failing audit write must not
    # leave core running on a config that no longer matches the one on disk.
    service, path, state, store, activated, live = make_service(tmp_path)

    def broken(kind, **payload):
        if kind == "config.applied":
            raise RuntimeError("database is locked")
        store.events.append((kind, payload))

    store.record_event = broken
    draft = dump(BASE | {"backup_jobs": ["daily"]})
    result = apply_and_activate(service, draft, live)
    assert result.status == "activating"
    assert activated.wait(1)
    assert _wait_released(state)
    assert path.read_text() == draft


def test_notification_setup_failure_still_activates_and_releases(tmp_path, monkeypatch):
    # Codex review, T35: a notice that cannot even start must not skip activation or keep
    # the mutation lock.
    service, _, state, _, activated, live = make_service(tmp_path, notify=lambda text: None)
    real_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "config-notification":
            raise RuntimeError("can't start new thread")
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(config_service.threading, "Thread", thread_factory)
    result = apply_and_activate(service, dump(BASE | {"backup_jobs": ["daily"]}), live)
    assert result.status == "activating"
    assert activated.wait(1)
    assert _wait_released(state)


def test_activation_proceeds_if_reply_is_never_signalled(tmp_path, monkeypatch):
    # An adapter failure between apply() returning and the reply being written must not
    # hold the mutation lock forever.
    monkeypatch.setattr(config_service, "REPLY_WAIT_SECONDS", 0.05)
    service, _, state, _, activated, live = make_service(tmp_path)
    result = service.apply(
        dump(BASE | {"backup_jobs": ["daily"]}), base_sha256=digest(live), operator="alice"
    )
    assert result.status == "activating"
    assert activated.wait(1)
    assert _wait_released(state)


def test_get_reports_loaded_sha_separately_from_the_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(dump())
    service = ConfigService(
        State(), config_path=path, activate=lambda: None, store=Store(), loaded_sha256="a" * 64,
    )
    result = service.get()
    assert result["loaded_sha256"] == "a" * 64
    assert result["sha256"] == digest(dump())


def test_activation_thread_start_failure_restores_previous_file(tmp_path, monkeypatch):
    # Codex review round 2, T35: if nothing can activate the new file, disk must go back to
    # matching the policy core is enforcing, and the lock must be released.
    service, path, state, store, activated, live = make_service(tmp_path)
    real_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "config-activation":
            raise RuntimeError("can't start new thread")
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(config_service.threading, "Thread", thread_factory)
    result = service.apply(
        dump(BASE | {"backup_jobs": ["daily"]}), base_sha256=digest(live), operator="alice"
    )
    assert result.status == "activation_failed"
    assert "previous config was restored" in result.reason
    assert path.read_text() == live
    assert state.owner is None
    assert not activated.is_set()
    assert any(kind == "config.activation_failed" for kind, _ in store.events)


def test_error_after_replace_still_activates(tmp_path, monkeypatch):
    # Codex review round 4, T35: a failure after os.replace (directory fsync) must not report
    # write_failed and skip activation while the new file is already on disk.
    service, path, state, _, activated, live = make_service(tmp_path)
    real_write = config_service.write_config_text

    def replaced_then_fsync_failed(text, target, **kwargs):
        real_write(text, target, **kwargs)
        raise OSError("fsync of directory failed")

    monkeypatch.setattr(config_service, "write_config_text", replaced_then_fsync_failed)
    draft = dump(BASE | {"backup_jobs": ["daily"]})
    result = apply_and_activate(service, draft, live)
    assert result.status == "activating"
    assert activated.wait(1)
    assert _wait_released(state)
    assert path.read_text() == draft
