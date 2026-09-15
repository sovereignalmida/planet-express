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
