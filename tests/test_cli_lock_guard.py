"""
CLI lock guard (landing 1b): casa_stackctl up/down and a real casa_zoidberg update pass run
outside casa-planetexpress's in-process mutation lock, so they refuse while that service
is active unless --force is passed. Read-only verbs and --dry-run are always allowed.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_bender as bender
import casa_stackctl as stackctl
import casa_zoidberg as zoidberg


@pytest.mark.parametrize(
    "rc, expected",
    [
        (0, True),     # active
        (3, False),    # inactive / failed
        (4, False),    # no such unit (e.g. a dev machine without the service)
        (1, True),     # unexpected: fail closed
        (124, True),   # run_argv timeout: fail closed
        (127, True),   # systemctl missing: fail closed
    ],
)
def test_core_service_active_asks_systemctl(monkeypatch, rc, expected):
    calls = []

    def fake(argv, timeout):
        calls.append(argv)
        return rc, "", ""

    monkeypatch.setattr(bender, "run_argv", fake)
    assert bender.core_service_active() is expected
    assert calls == [["systemctl", "is-active", "--quiet", "casa-planetexpress.service"]]


# ── casa_stackctl ───────────────────────────────────────────────────────────────
def _stackctl(monkeypatch, argv, active):
    monkeypatch.setattr(sys, "argv", ["casa_stackctl.py", *argv])
    monkeypatch.setattr(stackctl.bender, "core_service_active", lambda: active)


@pytest.mark.parametrize("verb", ["up", "down"])
def test_stackctl_mutation_refuses_while_core_is_active(monkeypatch, capsys, verb):
    _stackctl(monkeypatch, [verb, "media"], active=True)
    monkeypatch.setattr(stackctl, f"cmd_{verb}", lambda *a: pytest.fail("ran while core active"))
    assert stackctl.main() == 2
    assert "--force" in capsys.readouterr().err


def test_stackctl_force_proceeds_while_core_is_active(monkeypatch):
    _stackctl(monkeypatch, ["up", "media", "--force"], active=True)
    ran = []
    monkeypatch.setattr(stackctl, "cmd_up", lambda stack, all_: ran.append((stack, all_)) or 0)
    assert stackctl.main() == 0
    assert ran == [("media", False)]


def test_stackctl_runs_normally_when_core_is_not_active(monkeypatch):
    _stackctl(monkeypatch, ["down", "--all"], active=False)
    ran = []
    monkeypatch.setattr(stackctl, "cmd_down", lambda stack, all_: ran.append((stack, all_)) or 0)
    assert stackctl.main() == 0
    assert ran == [(None, True)]


def test_stackctl_read_only_verbs_never_check_the_service(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["casa_stackctl.py", "list"])
    monkeypatch.setattr(stackctl.bender, "core_service_active", lambda: pytest.fail("checked for a read"))
    monkeypatch.setattr(stackctl, "cmd_list", lambda: 0)
    assert stackctl.main() == 0


# ── casa_zoidberg ───────────────────────────────────────────────────────────────
def test_zoidberg_real_pass_refuses_while_core_is_active(monkeypatch, capsys):
    monkeypatch.setattr(zoidberg.bender, "core_service_active", lambda: True)
    monkeypatch.setattr(zoidberg, "run_update_pass", lambda **kw: pytest.fail("updated while core active"))
    assert zoidberg.main([]) == 2
    assert "--force" in capsys.readouterr().err


def test_zoidberg_dry_run_is_allowed_while_core_is_active(monkeypatch):
    monkeypatch.setattr(zoidberg.bender, "core_service_active", lambda: pytest.fail("dry run must not check"))
    calls = []
    monkeypatch.setattr(zoidberg, "run_update_pass", lambda **kw: calls.append(kw) or [])
    monkeypatch.setattr(zoidberg.config, "ensure_dirs", lambda: None)
    assert zoidberg.main(["--dry-run"]) == 0
    assert calls == [{"tg": None, "dry_run": True, "commands": None}]  # a dry run touches nothing


def test_zoidberg_force_runs_a_real_pass_while_core_is_active(monkeypatch):
    monkeypatch.setattr(zoidberg.bender, "core_service_active", lambda: True)
    calls = []
    monkeypatch.setattr(zoidberg, "run_update_pass", lambda **kw: calls.append(kw) or [])
    monkeypatch.setattr(zoidberg.config, "ensure_dirs", lambda: None)
    assert zoidberg.main(["--force"]) == 0
    # a real pass runs on the engine, so it gets a command service of its own (slice 5b-3)
    assert [(c["tg"], c["dry_run"]) for c in calls] == [(None, False)]
    assert calls[0]["commands"] is not None
