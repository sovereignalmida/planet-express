"""
scripts/web_access.py (landing 1c, T10): host-free plan checks and CLI behavior. No sudo,
no real accounts or ACLs; every lookup is injected.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import web_access

WEB = "planetexpress-web"
ENTRIES = ["venv", "data", "state", ".git", "logs", "casa_scruffy.py", "planet_express",
           ".mcp.json", "scpdump"]


def plan(**overrides):
    options = {"user_exists": lambda name: False, "group_exists": lambda name: False,
               "in_group": lambda user, group: False,
               "other_can_traverse": lambda path: path == Path("/home"),
               "entries": ENTRIES}
    options.update(overrides)
    return web_access.plan_web_access("/home/chris/pe", "chris", **options)


def _grants(commands):
    return [c[-1] for c in commands if f"u:{WEB}:rX" in c]


def _denies(commands):
    return [c[-1] for c in commands if f"u:{WEB}:---" in c]


def test_fresh_host_exact_plan():
    assert plan() == [
        ["groupadd", "--system", "planetexpress-rpc"],
        ["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent",
         "--shell", "/usr/sbin/nologin", "--user-group", WEB],
        ["usermod", "-aG", "planetexpress-rpc", "chris"],
        ["usermod", "-aG", "planetexpress-rpc", WEB],
        ["setfacl", "-m", f"u:{WEB}:x", "/home/chris"],
        ["setfacl", "-m", f"u:{WEB}:rx", "/home/chris/pe"],
        ["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/.git"],
        ["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/.mcp.json"],
        ["setfacl", "-R", "-m", f"u:{WEB}:rX", "/home/chris/pe/casa_scruffy.py"],
        ["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/data"],
        ["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/logs"],
        ["setfacl", "-R", "-m", f"u:{WEB}:rX", "/home/chris/pe/planet_express"],
        ["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/scpdump"],
        ["setfacl", "-R", "-m", f"u:{WEB}:rX", "/home/chris/pe/venv"],
        ["setfacl", "-R", "-m", f"u:{WEB}:rX", "/home/chris/pe/state"],
        ["setfacl", "-d", "-m", f"u:{WEB}:rX", "/home/chris/pe/state"],
        ["chmod", "-R", "o-rwx", "/home/chris/pe/state"],
    ]


def test_existing_accounts_and_membership_skip_account_setup():
    commands = plan(user_exists=lambda name: True, group_exists=lambda name: True,
                    in_group=lambda user, group: True)
    assert commands == plan()[4:]


def test_untracked_and_runtime_entries_are_explicitly_denied_never_granted():
    commands = plan()
    for name in (".mcp.json", "scpdump", "data", "logs", ".git"):
        assert f"/home/chris/pe/{name}" in _denies(commands)
        assert f"/home/chris/pe/{name}" not in _grants(commands)


def test_only_allowlisted_entries_are_readable():
    entries = ["casa_scruffy.py", "config.py", "planet_express", "templates", "static", "venv",
               "__pycache__", "docs", "tests", "README.md", "requirements.txt", ".venv"]
    commands = plan(entries=entries)
    readable = {Path(p).name for p in _grants(commands)} - {"state"}
    assert readable == {"casa_scruffy.py", "config.py", "planet_express", "templates", "static",
                        "venv", "__pycache__"}
    assert {Path(p).name for p in _denies(commands)} == {"docs", "tests", "README.md",
                                                         "requirements.txt", ".venv"}


def test_traversal_only_where_other_cannot():
    commands = plan(other_can_traverse=lambda path: False)
    assert [c[-1] for c in commands if f"u:{WEB}:x" in c] == ["/home", "/home/chris"]
    assert not any(f"u:{WEB}:x" in c for c in plan(other_can_traverse=lambda p: True))


def test_every_command_is_an_argv_list_of_strings():
    for command in plan(config_file="/etc/planetexpress/config.yaml"):
        assert isinstance(command, list) and all(isinstance(arg, str) for arg in command)


def test_external_config_gets_traverse_and_read():
    commands = plan(config_file="/etc/planetexpress/config.yaml", other_can_traverse=lambda p: False)
    assert commands[-3:] == [
        ["setfacl", "-m", f"u:{WEB}:x", "/etc"],
        ["setfacl", "-m", f"u:{WEB}:x", "/etc/planetexpress"],
        ["setfacl", "-m", f"u:{WEB}:r", "/etc/planetexpress/config.yaml"],
    ]


def test_top_level_config_in_clone_is_denied_then_granted_read_last():
    commands = plan(entries=ENTRIES + ["config.yaml"], config_file="/home/chris/pe/config.yaml")
    deny = commands.index(["setfacl", "-m", f"u:{WEB}:---", "/home/chris/pe/config.yaml"])
    grant = commands.index(["setfacl", "-m", f"u:{WEB}:r", "/home/chris/pe/config.yaml"])
    assert deny < grant == len(commands) - 1


@pytest.mark.parametrize("path", ["/home/chris/pe/settings/config.yaml",
                                  "/home/chris/pe/data/config.yaml",
                                  "/home/chris/pe/state/config.yaml"])
def test_nested_config_in_clone_is_rejected(path):
    with pytest.raises(ValueError, match="top level"):
        plan(config_file=path)


def test_relative_paths_rejected():
    with pytest.raises(ValueError):
        web_access.plan_web_access("pe", "chris", user_exists=bool, group_exists=bool,
                                   in_group=lambda u, g: True, other_can_traverse=bool, entries=[])
    with pytest.raises(ValueError):
        plan(config_file="config.yaml")


def test_main_refuses_root(monkeypatch):
    monkeypatch.setattr(web_access.os, "getuid", lambda: 0)
    run = Mock()
    monkeypatch.setattr(web_access.subprocess, "run", run)
    with pytest.raises(SystemExit, match="root"):
        web_access.main()
    run.assert_not_called()


def test_main_refuses_without_setfacl_before_any_command(monkeypatch):
    monkeypatch.setattr(web_access.os, "getuid", lambda: 1234)
    monkeypatch.setattr(web_access.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(web_access.shutil, "which", lambda name: None)
    run = Mock()
    monkeypatch.setattr(web_access.subprocess, "run", run)
    with pytest.raises(SystemExit, match="acl package"):
        web_access.main()
    run.assert_not_called()


def test_main_executes_sudo_argv(monkeypatch, capsys):
    monkeypatch.setattr(web_access.os, "getuid", lambda: 1234)
    monkeypatch.setattr(web_access.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(web_access.shutil, "which", lambda name: "/usr/bin/setfacl")
    monkeypatch.setattr(web_access.pwd, "getpwuid", lambda uid: Mock(pw_name="chris"))
    planner = Mock(return_value=[["setfacl", "-m", f"u:{WEB}:r", "/a path"]])
    monkeypatch.setattr(web_access, "plan_web_access", planner)
    run = Mock()
    monkeypatch.setattr(web_access.subprocess, "run", run)
    web_access.main()
    run.assert_called_once_with(["sudo", "setfacl", "-m", f"u:{WEB}:r", "/a path"], check=True)
    assert planner.call_args.args[1] == "chris"
    assert "entries" in planner.call_args.kwargs
    assert "/a path" in capsys.readouterr().out
