"""Pure provisioning changes and mocked privileged file installation."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_auth
from scripts import dashboard_operators as operators

SECRET = web_auth.new_totp_secret()


def add(values, name, phrase=None):
    return operators.apply_operator_change(values, "add", name,
                                           passphrase=phrase or f"passphrase for {name}", totp_secret=SECRET)


def test_env_roundtrip_and_changes():
    text = '# Panel credentials\nADGUARD_USERNAME="user name"\n\nTELEGRAM_BOT_USERNAME=pebot\n'
    values = operators.parse_env(text)
    assert operators.render_env(text, values) == text
    updated = add(values, "alice")
    rendered = operators.render_env(text, updated)
    assert rendered.startswith(text)
    assert operators.parse_env(rendered) == updated
    assert operators.list_operators(updated) == ["alice"]
    reset = operators.apply_operator_change(updated, "reset", "alice",
                                            passphrase="replacement phrase", totp_secret=web_auth.new_totp_secret())
    assert reset["PE_OPERATOR_ALICE_TOTP_SECRET"] != SECRET
    assert web_auth.verify_passphrase(reset["PE_OPERATOR_ALICE_PASSPHRASE_HASH"], "replacement phrase")
    removed = operators.apply_operator_change(reset, "remove", "alice")
    assert operators.list_operators(removed) == []
    assert "PE_OPERATOR_ALICE_TOTP_SECRET" not in removed
    assert operators.render_env(rendered, removed).startswith(text)


def test_limits_and_reuse():
    values = add(add(add({}, "alice"), "bob"), "charlie")
    with pytest.raises(ValueError, match="3"):
        add(values, "four")
    with pytest.raises(ValueError, match="already used"):
        operators.apply_operator_change(values, "reset", "bob",
                                        passphrase="passphrase for alice", totp_secret=SECRET)
    with pytest.raises(ValueError, match="already used"):
        add(add({}, "alice"), "bob", "passphrase for alice")


@pytest.mark.parametrize("name", ["", "Alice", "a b", "?", "a" * 33, "é", "x\n"])
def test_name_rules(name):
    with pytest.raises(ValueError, match="name"):
        add({}, name)


def test_collision_and_missing():
    with pytest.raises(ValueError):
        add(add({}, "alice-bob"), "alice.bob")
    with pytest.raises(ValueError, match="does not exist"):
        operators.apply_operator_change({}, "remove", "alice")


def test_root_refused(monkeypatch):
    monkeypatch.setattr(operators.os, "getuid", lambda: 0)
    with pytest.raises(SystemExit, match="root"):
        operators.main(["init"])


def test_main_install_and_idempotent_init(monkeypatch, capsys):
    monkeypatch.setattr(operators.os, "getuid", lambda: 1000)
    monkeypatch.setattr(operators.os, "geteuid", lambda: 1000)
    monkeypatch.setattr("builtins.input", lambda _: "alice")
    monkeypatch.setattr(operators.getpass, "getpass", lambda _: "a good long passphrase")
    monkeypatch.setattr(operators.shutil, "which", lambda _: None)
    state = {"text": "# Keep me\nADGUARD_USERNAME=admin\n"}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["sudo", "cat"]:
            return SimpleNamespace(returncode=0, stdout=state["text"])
        assert argv[:8] == ["sudo", "install", "-m", "600", "-o", "root", "-g", "root"]
        assert argv[-1] == operators.ENV_FILE
        from pathlib import Path
        state["text"] = Path(argv[-2]).read_text()
        assert Path(argv[-2]).stat().st_mode & 0o777 == 0o600
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(operators.subprocess, "run", run)
    operators.main(["init"])
    assert state["text"].startswith("# Keep me\nADGUARD_USERNAME=admin\n")
    assert len(operators.parse_env(state["text"])["PE_DASHBOARD_SECRET_KEY"]) >= 32
    output = capsys.readouterr().out
    assert output.count("otpauth://") == 1 and "scrypt:" not in output
    calls.clear()
    operators.main(["init"])
    assert calls == [["sudo", "cat", operators.ENV_FILE]]
    assert "otpauth://" not in capsys.readouterr().out
    operators.main(["list"])
    assert capsys.readouterr().out == "alice\n"


def test_changed_env_values_and_quoting():
    text = '# header\nA="old"\nB="keep $this # too"\n; footer\n'
    values = operators.parse_env(text)
    values["A"] = 'new "quoted" \\ path'
    rendered = operators.render_env(text, values)
    assert operators.parse_env(rendered) == values
    assert rendered.endswith('B="keep $this # too"\n; footer\n')


def test_missing_file_and_read_failure(monkeypatch):
    results = iter([SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
    monkeypatch.setattr(operators.subprocess, "run", lambda *a, **kw: next(results))
    assert operators._read_env() == ""
    results = iter([SimpleNamespace(returncode=1), SimpleNamespace(returncode=1)])
    with pytest.raises(SystemExit, match="Could not read"):
        operators._read_env()


# ── reset revokes trusted devices (T47) ──────────────────────────────────────────

def _reset_harness(monkeypatch, revoke):
    """`reset alice` with the environment file and the device store both stubbed."""
    monkeypatch.setattr(operators.os, "getuid", lambda: 1000)
    monkeypatch.setattr(operators.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(operators.getpass, "getpass", lambda _: "a good long passphrase")
    monkeypatch.setattr(operators.shutil, "which", lambda _: None)
    monkeypatch.setattr(operators.revoke_devices, "revoke", revoke)
    old_hash = web_auth.hash_passphrase("the passphrase being replaced")
    state = {"old_hash": old_hash,
             "text": f"PE_OPERATORS=alice\n"
                     f"PE_OPERATOR_ALICE_PASSPHRASE_HASH={old_hash}\n"
                     f"PE_OPERATOR_ALICE_TOTP_SECRET=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ\n"}

    def run(argv, **kwargs):
        state.setdefault("ran", []).append(argv)
        if argv[:2] == ["sudo", "cat"]:
            return SimpleNamespace(returncode=0, stdout=state["text"])
        if argv[:3] == ["sudo", "systemctl", "restart"]:
            if state.get("restart_fails"):
                raise operators.subprocess.CalledProcessError(1, argv)
            return SimpleNamespace(returncode=0)
        state["text"] = Path(argv[-2]).read_text()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(operators.subprocess, "run", run)
    return state


def test_reset_revokes_trusted_devices(monkeypatch, capsys):
    """A passphrase change that leaves the old devices signed in is a reset that does not end
    the sessions it was performed to end.

    Twice, around the restart. The dashboard holds the old passphrase until it restarts and
    mints tokens against a live epoch lookup, so a sign-in with the OLD passphrase in the gap
    would get a token carrying the NEW epoch and outlive the reset. Bump, change, restart,
    bump again.
    """
    revoked = []
    state = _reset_harness(monkeypatch, lambda name: revoked.append(name) or len(revoked))
    operators.main(["reset", "alice"])
    assert revoked == ["alice", "alice"], "the epoch was not bumped on both sides of the restart"
    assert state["old_hash"] not in state["text"], "the passphrase was not changed"

    order = [a[:3] for a in state["ran"] if a[:1] == ["sudo"]]
    assert ["sudo", "systemctl", "restart"] in order
    assert order.index(["sudo", "install", "-m"]) < order.index(["sudo", "systemctl", "restart"]), \
        "restarted before the new credentials were written"
    assert "Restarted casa-dashboard" in capsys.readouterr().out


def test_a_failed_restart_says_the_old_passphrase_still_works(monkeypatch):
    """The env is already written by this point, so this cannot be silent: until the unit
    restarts, the passphrase that was just replaced still signs people in."""
    state = _reset_harness(monkeypatch, lambda name: 1)
    state["restart_fails"] = True
    with pytest.raises(SystemExit) as exit_:
        operators.main(["reset", "alice"])
    message = str(exit_.value)
    assert "old passphrase still works" in message
    assert "systemctl restart casa-dashboard" in message
    assert "revoke_devices.py alice" in message


def test_a_failed_revoke_leaves_the_passphrase_alone(monkeypatch):
    """Revoking first, and stopping if it fails, is the whole point of the ordering: the one
    outcome worth avoiding is a changed passphrase whose old devices are still trusted.

    This is the case where the install user cannot reach core's database -- they are the same
    user on this host, and the script does not get to assume that.
    """
    def unreachable(_name):
        raise PermissionError("unable to open database file")

    state = _reset_harness(monkeypatch, unreachable)
    before = state["text"]
    with pytest.raises(SystemExit) as exit_:
        operators.main(["reset", "alice"])
    assert "NOT been changed" in str(exit_.value)
    assert "revoke_devices.py alice" in str(exit_.value)
    assert state["text"] == before, "the environment file was written despite the failure"


def test_a_mismatched_confirmation_signs_nobody_out(monkeypatch):
    """The revocation waits until the new passphrase is known to be good. Otherwise a typo
    signs every device out for a reset that then refuses to happen -- the worst of both."""
    revoked = []
    answers = iter(["one passphrase", "a different one"])
    state = _reset_harness(monkeypatch, lambda name: revoked.append(name) or 1)
    monkeypatch.setattr(operators.getpass, "getpass", lambda _: next(answers))
    before = state["text"]
    with pytest.raises(SystemExit, match="do not match"):
        operators.main(["reset", "alice"])
    assert revoked == [], "devices were signed out for a reset that did not happen"
    assert state["text"] == before


def test_a_short_passphrase_signs_nobody_out(monkeypatch):
    revoked = []
    state = _reset_harness(monkeypatch, lambda name: revoked.append(name) or 1)
    monkeypatch.setattr(operators.getpass, "getpass", lambda _: "short")
    with pytest.raises(SystemExit):
        operators.main(["reset", "alice"])
    assert revoked == []
    assert state["old_hash"] in state["text"]


def test_a_failed_second_revoke_names_the_recovery(monkeypatch):
    """By this point the passphrase is changed and the unit restarted, so this cannot raise a
    traceback: a session that signed in with the old passphrase during the restart may still
    be trusted, and the operator has to be told exactly what closes it."""
    calls = []

    def revoke(name):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("database is locked")
        return len(calls)

    _reset_harness(monkeypatch, revoke)
    with pytest.raises(SystemExit) as exit_:
        operators.main(["reset", "alice"])
    message = str(exit_.value)
    assert "OLD" in message and "may still be trusted" in message
    assert "revoke_devices.py alice" in message


def test_a_failed_restart_still_shows_the_new_totp_code(capsys, monkeypatch):
    """The new secret is live the moment the file is written. Exiting between there and the
    provisioning URI would lock the operator out by way of the recovery path -- an active
    secret they were never shown, and no way in to reset it again."""
    state = _reset_harness(monkeypatch, lambda name: 1)
    state["restart_fails"] = True
    with pytest.raises(SystemExit):
        operators.main(["reset", "alice"])
    assert "otpauth://" in capsys.readouterr().out


def test_a_failed_second_revoke_still_shows_the_new_totp_code(capsys, monkeypatch):
    calls = []

    def revoke(name):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("database is locked")
        return len(calls)

    _reset_harness(monkeypatch, revoke)
    with pytest.raises(SystemExit):
        operators.main(["reset", "alice"])
    assert "otpauth://" in capsys.readouterr().out
