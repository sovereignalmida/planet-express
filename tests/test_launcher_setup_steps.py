"""The launcher's setup steps (contract 22.5): probe, repair, undo and journal, on a throwaway tree."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from launcher_support import load_helper

from planet_express.missions.launcher.config import CONF_PATH, LaunchConf, mos_conf_path
from planet_express.missions.launcher.setup_steps import (
    LauncherSudoers,
    MemoryJournal,
    Paths,
    StepError,
    launcher_steps,
    probe_all,
    repair_all,
    undo_all,
)
from planet_express.setup.journal import Journal

ME, MY_GID = os.geteuid(), os.getegid()


def paths(tmp_path, **over):
    tmp_path.chmod(0o755)
    base = dict(core_uid=ME, core_gid=MY_GID, core_user="pe", uid_min=61000, uid_max=61999,
                cgroup_root=str(tmp_path / "cg" / "sessions"), workspace_root=str(tmp_path / "var" / "ws"),
                sock_root=str(tmp_path / "run" / "socks"), helper=str(tmp_path / "usr" / "libexec" / "pe" / "pe-launch"),
                conf=str(tmp_path / "etc" / "pe" / "launch.conf"), sudoers=str(tmp_path / "etc" / "sudoers.d" / "pe"),
                unit_dropin=str(tmp_path / "etc" / "systemd" / "launcher.conf"),
                boot_script=str(tmp_path / "data" / "launcher-boot.sh"), interpreter="/usr/bin/python3",
                owner_uid=ME, owner_gid=MY_GID, below=str(tmp_path), passwd=str(tmp_path / "passwd"), check_delegation=False,
                group=str(tmp_path / "group"), run=visudo_ok)
    base.update(over)
    (tmp_path / "etc" / "sudoers.d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc" / "sudoers.d").chmod(0o755)
    (tmp_path / "etc").chmod(0o755)
    (tmp_path / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\npe:x:1000:1000::/home/pe:/bin/sh\n")
    (tmp_path / "group").write_text("root:x:0:\npe:x:1000:\n")
    return Paths(**base)


def visudo_ok(argv, **kw):
    visudo_ok.calls.append(argv)
    return subprocess.CompletedProcess(argv, 0, b"", b"")


visudo_ok.calls = []


def visudo_bad(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, b"", b"parse error")


def by_name(steps):
    return {s.name: s for s in steps}


# ── which steps there are ───────────────────────────────────────────────────

def test_the_steps_are_named_in_dependency_order(tmp_path):
    names = [s.name for s in launcher_steps(paths(tmp_path))]
    assert names == ["session-uid-range", "launcher-helper", "launcher-sudoers", "cgroup-tree", "workspace-root",
                     "socket-root"]


def test_mos_has_no_sudoers_step(tmp_path):
    names = [s.name for s in launcher_steps(paths(tmp_path, flavour="mos"))]
    assert "launcher-sudoers" not in names and "cgroup-tree" in names


# ── probe, repair, probe ────────────────────────────────────────────────────

def test_a_fresh_host_needs_every_step_and_none_is_blocked(tmp_path):
    states = probe_all(launcher_steps(paths(tmp_path)))
    assert not any(s.ok for s in states.values()) and not any(s.blocked for s in states.values())


def test_repair_makes_every_step_satisfied_and_a_second_run_changes_nothing(tmp_path):
    steps = launcher_steps(paths(tmp_path))
    journal = MemoryJournal()
    first = repair_all(steps, journal)
    assert set(first.values()) == {"repaired"}, first
    assert all(s.ok for s in probe_all(steps).values())
    again = repair_all(steps, MemoryJournal())
    assert set(again.values()) == {"already in place"}


def test_conf_helper_and_sudoers_have_the_documented_content_and_modes(tmp_path):
    p = paths(tmp_path)
    repair_all(launcher_steps(p), MemoryJournal())
    conf = Path(p.conf)
    assert conf.read_text() == p.launch_conf().render() and oct(conf.stat().st_mode & 0o777) == "0o600"
    helper = Path(p.helper)
    text = helper.read_text()
    assert text.startswith("#!/usr/bin/python3 -IS\n") and oct(helper.stat().st_mode & 0o777) == "0o755"
    source = Path(__file__).resolve().parents[1] / "planet_express/missions/launcher/pe_launch.py"
    expected = source.read_text().split("\n", 1)[1].replace(f'CONF_PATH = "{CONF_PATH}"', f'CONF_PATH = "{p.conf}"')
    assert text.split("\n", 1)[1] == expected      # only the interpreter line and (off the default) the CONF_PATH line differ
    sudoers = Path(p.sudoers).read_text()
    assert f"Defaults!{p.helper} env_reset, !setenv, !env_editor" in sudoers
    assert f'pe ALL=(root) NOPASSWD: {p.helper} ""' in sudoers
    assert oct(Path(p.sudoers).stat().st_mode & 0o777) == "0o440"
    assert [s for s in Path(p.sudoers).parent.iterdir() if s.name.startswith(".")] == []     # no staging file left


def test_the_conf_setup_writes_is_the_conf_the_helper_accepts(tmp_path):
    p = paths(tmp_path)
    repair_all(launcher_steps(p), MemoryJournal())
    conf = load_helper().load_conf(p.conf, trusted_uid=ME, below=str(tmp_path))
    assert (conf["uid_min"], conf["uid_max"], conf["core_uid"]) == (61000, 61999, ME)
    assert conf["cgroup_root"] == p.cgroup_root and conf["workspace_root"] == p.workspace_root
    assert LaunchConf.parse(Path(p.conf).read_text()) == p.launch_conf()


def test_directories_are_owned_by_core_with_the_documented_modes(tmp_path):
    p = paths(tmp_path)
    repair_all(launcher_steps(p), MemoryJournal())
    for path in (p.workspace_root, p.sock_root):
        info = os.lstat(path)
        assert oct(info.st_mode & 0o7777) == "0o711" and info.st_uid == ME


def test_the_cgroup_tree_step_is_a_delegate_dropin_on_systemd_and_a_boot_script_on_mos(tmp_path):
    p = paths(tmp_path)
    repair_all(launcher_steps(p), MemoryJournal())
    assert "Delegate=yes" in Path(p.unit_dropin).read_text()
    mos = paths(tmp_path / "mos", flavour="mos") if (tmp_path / "mos").mkdir() is None else None
    repair_all(launcher_steps(mos), MemoryJournal())
    script = Path(mos.boot_script).read_text()
    assert script.startswith("#!/bin/sh\n") and "+pids" in script and mos.cgroup_root in script
    assert oct(Path(mos.boot_script).stat().st_mode & 0o777) == "0o700"


# ── refusing to build on an unsafe host ─────────────────────────────────────

def test_a_group_writable_helper_directory_blocks_the_helper_step_and_writes_nothing(tmp_path):
    p = paths(tmp_path)
    parent = Path(p.helper).parent
    parent.mkdir(parents=True)
    parent.chmod(0o775)
    for d in parent.parents:
        if d == tmp_path:
            break
        d.chmod(0o755)
    step = by_name(launcher_steps(p))["launcher-helper"]
    state = step.probe()
    assert state.blocked and "untrusted" in state.detail
    with pytest.raises(StepError):
        step.repair(MemoryJournal())
    assert not Path(p.helper).exists()


def test_a_symlinked_helper_directory_blocks_the_helper_step(tmp_path):
    p = paths(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    real.chmod(0o755)
    parent = Path(p.helper).parent
    parent.parent.mkdir(parents=True)
    parent.parent.chmod(0o755)
    parent.symlink_to(real)
    state = by_name(launcher_steps(p))["launcher-helper"].probe()
    assert state.blocked and "symlink" in state.detail


def test_a_symlinked_helper_file_is_never_followed(tmp_path):
    p = paths(tmp_path)
    parent = Path(p.helper).parent
    parent.mkdir(parents=True)
    for d in (parent, parent.parent):
        d.chmod(0o755)
    (tmp_path / "victim").write_text("x")
    Path(p.helper).symlink_to(tmp_path / "victim")
    step = by_name(launcher_steps(p))["launcher-helper"]
    assert step.probe().blocked
    with pytest.raises(StepError):
        step.repair(MemoryJournal())
    assert (tmp_path / "victim").read_text() == "x"


def test_a_changed_helper_is_flagged_and_repaired(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    repair_all(steps, MemoryJournal())
    Path(p.helper).write_text("#!/bin/sh\necho evil\n")
    state = by_name(steps)["launcher-helper"].probe()
    assert not state.ok and not state.blocked and "differs" in state.detail
    repair_all(steps, MemoryJournal())
    assert "evil" not in Path(p.helper).read_text()


def test_a_helper_with_the_wrong_mode_is_not_satisfied(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    repair_all(steps, MemoryJournal())
    Path(p.helper).chmod(0o775)
    assert not by_name(steps)["launcher-helper"].probe().ok


def test_the_uid_range_refuses_one_that_collides_with_an_account(tmp_path):
    p = paths(tmp_path)
    Path(p.passwd).write_text("root:x:0:0::/:/bin/sh\nsvc:x:61500:61500::/:/bin/sh\n")
    state = by_name(launcher_steps(p))["session-uid-range"].probe()
    assert state.blocked and "svc" in state.detail
    Path(p.passwd).write_text("root:x:0:0::/:/bin/sh\n")
    Path(p.group).write_text("root:x:0:\nodd:x:61001:\n")
    assert "odd" in by_name(launcher_steps(p))["session-uid-range"].probe().detail


def test_an_account_outside_the_range_whose_primary_group_is_inside_it_is_a_collision(tmp_path):
    p = paths(tmp_path)
    Path(p.passwd).write_text("root:x:0:0::/:/bin/sh\nsvc:x:2000:61001::/:/bin/sh\n")
    state = by_name(launcher_steps(p))["session-uid-range"].probe()
    assert state.blocked and "svc" in state.detail


def test_an_impossible_range_is_blocked_not_written(tmp_path):
    p = paths(tmp_path, uid_min=61000, uid_max=61000)
    state = by_name(launcher_steps(p))["session-uid-range"].probe()
    assert not state.ok
    p2 = paths(tmp_path, core_uid=61500)
    assert by_name(launcher_steps(p2))["session-uid-range"].probe().blocked


def test_a_workspace_root_that_is_a_symlink_is_never_replaced(tmp_path):
    p = paths(tmp_path)
    (tmp_path / "var").mkdir()
    (tmp_path / "target").mkdir()
    Path(p.workspace_root).symlink_to(tmp_path / "target")
    step = by_name(launcher_steps(p))["workspace-root"]
    assert step.probe().blocked
    with pytest.raises(StepError):
        step.repair(MemoryJournal())


# ── sudoers ─────────────────────────────────────────────────────────────────

def test_visudo_rejecting_the_fragment_installs_nothing_and_leaves_no_staging_file(tmp_path):
    p = paths(tmp_path, run=visudo_bad)
    step = LauncherSudoers(p)
    with pytest.raises(StepError, match="visudo"):
        step.repair(MemoryJournal())
    assert list(Path(p.sudoers).parent.iterdir()) == []


def test_visudo_is_run_on_the_candidate_and_on_the_installed_file(tmp_path):
    visudo_ok.calls.clear()
    p = paths(tmp_path)
    LauncherSudoers(p).repair(MemoryJournal())
    targets = [c[-1] for c in visudo_ok.calls]
    assert any(t.endswith(".check") for t in targets) and p.sudoers in targets
    assert all(c[:3] == [p.visudo, "-c", "-f"] for c in visudo_ok.calls)


def test_the_sudoers_step_does_not_care_whether_a_legacy_sudoers_file_is_preserved(tmp_path):
    p = paths(tmp_path)
    legacy = Path(p.sudoers).parent / "casa-planetexpress"
    legacy.write_text("# the 2.0 grant, preserved\n")
    repair_all(launcher_steps(p), MemoryJournal())
    assert Path(p.sudoers).exists() and legacy.read_text() == "# the 2.0 grant, preserved\n"


def test_the_sudoers_rule_allows_the_helper_with_no_arguments_only(tmp_path):
    p = paths(tmp_path)
    text = LauncherSudoers(p).wanted().decode()
    rule = [l for l in text.splitlines() if "NOPASSWD" in l]
    assert rule == [f'pe ALL=(root) NOPASSWD: {p.helper} ""']
    assert "ALL=(ALL)" not in text and "*" not in text


# ── journal and undo ────────────────────────────────────────────────────────

def test_evidence_is_journaled_before_each_write(tmp_path):
    p = paths(tmp_path)
    journal = MemoryJournal()
    repair_all(launcher_steps(p), journal)
    kinds = [e["type"] for e in journal.events if e.get("step") == "launcher-helper"]
    assert kinds[0] == "step_started" and kinds[-1] == "step_ok" and kinds[1:-1] and set(kinds[1:-1]) == {"evidence"}
    assert journal.evidence("launcher-helper")["sha256"]


def test_undo_removes_what_setup_made_newest_first_and_only_that(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    journal = MemoryJournal()
    repair_all(steps, journal)
    result = undo_all(steps, journal)
    assert not any(v.startswith("REFUSED") for v in result.values()), result
    for gone in (p.conf, p.helper, p.sudoers, p.unit_dropin, p.workspace_root, p.sock_root):
        assert not os.path.lexists(gone), gone
    assert Path(p.sudoers).parent.exists() and tmp_path.exists()


def test_undo_refuses_over_a_file_changed_after_setup_wrote_it(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    journal = MemoryJournal()
    repair_all(steps, journal)
    Path(p.helper).write_text(Path(p.helper).read_text() + "\n# local edit\n")
    result = undo_all(steps, journal)
    assert result["launcher-helper"].startswith("REFUSED") and Path(p.helper).exists()
    assert any(e["type"] == "undo_refused" for e in journal.events)


def test_undo_restores_a_file_setup_replaced(tmp_path):
    p = paths(tmp_path)
    Path(p.conf).parent.mkdir(parents=True)
    Path(p.conf).parent.chmod(0o755)
    Path(p.conf).write_text("old conf\n")
    Path(p.conf).chmod(0o600)
    steps = launcher_steps(p)
    journal = MemoryJournal()
    repair_all(steps, journal)
    assert Path(p.conf).read_text() != "old conf\n"
    undo_all(steps, journal)
    assert Path(p.conf).read_text() == "old conf\n"


def test_undo_is_repeatable_after_a_crash(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    journal = MemoryJournal()
    repair_all(steps, journal)
    undo_all(steps, journal)
    again = undo_all(steps, journal)
    assert not any(v.startswith("REFUSED") for v in again.values())


def test_undo_leaves_a_directory_it_did_not_create(tmp_path):
    p = paths(tmp_path)
    Path(p.sock_root).mkdir(parents=True)
    Path(p.sock_root).chmod(0o711)
    steps = launcher_steps(p)
    journal = MemoryJournal()
    repair_all(steps, journal)
    undo_all(steps, journal)
    assert Path(p.sock_root).is_dir()


def test_repair_stops_at_the_first_step_that_cannot_proceed(tmp_path):
    p = paths(tmp_path)
    Path(p.passwd).write_text("svc:x:61500:61500::/:/bin/sh\n")
    result = repair_all(launcher_steps(p), MemoryJournal())
    assert list(result) == ["session-uid-range"] and result["session-uid-range"].startswith("FAILED")
    assert not Path(p.helper).exists()


class SetupJournalShape:
    """The surface of planet_express.setup.journal.Journal the steps rely on: append(), and steps() folding the
    evidence events into per-step records. (The real one insists on a root-owned directory chain, which a test
    under /tmp cannot have; test_the_journal_surface_matches pins the two together.)"""

    def __init__(self):
        self.events = []

    def append(self, type_, **data):
        self.events.append({"type": type_, **data})

    def steps(self):
        out = {}
        for e in self.events:
            if e.get("step") and e["type"] == "evidence":
                out.setdefault(e["step"], SimpleNamespace(evidence=[])).evidence.append(e["data"])
        return out


def test_the_steps_work_with_a_journal_that_only_has_append_and_steps(tmp_path):
    p = paths(tmp_path)
    steps = launcher_steps(p)
    journal = SetupJournalShape()
    assert set(repair_all(steps, journal).values()) == {"repaired"}
    result = undo_all(steps, journal)
    assert not any(v.startswith("REFUSED") for v in result.values()), result
    assert not Path(p.helper).exists()


def test_the_journal_surface_matches():
    import inspect
    assert list(inspect.signature(Journal.append).parameters)[:2] == ["self", "type_"]
    assert callable(Journal.steps) and "evidence" in inspect.getsource(Journal.steps)


# ── activation: a written drop-in is not a delegated cgroup (Codex) ─────────────────────────────

def test_the_systemd_step_runs_daemon_reload_and_is_pending_until_the_cgroup_is_delegated_to_core(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    p = paths(tmp_path, check_delegation=True, run=run, cgroup_root=str(tmp_path / "svc" / "sessions"))
    steps = launcher_steps(p)
    result = repair_all(steps, MemoryJournal())
    assert result["cgroup-tree"].startswith("repaired, activation pending") and "restart" in result["cgroup-tree"]
    assert [p.systemctl, "daemon-reload"] in calls
    assert not by_name(steps)["cgroup-tree"].probe().ok            # NOT satisfied yet: the service has not restarted
    (tmp_path / "svc").mkdir()                                     # systemd made and chowned it at (re)start
    assert by_name(steps)["cgroup-tree"].probe().ok
    assert repair_all(steps, MemoryJournal())["cgroup-tree"] == "already in place"


def test_a_delegated_cgroup_owned_by_someone_else_is_not_active(tmp_path):
    p = paths(tmp_path, check_delegation=True, core_uid=ME + 1, cgroup_root=str(tmp_path / "svc" / "sessions"))
    (tmp_path / "svc").mkdir()
    step = by_name(launcher_steps(p))["cgroup-tree"]
    step.repair(MemoryJournal())
    state = step.probe()
    assert not state.ok and "not the Core user" in state.detail


def test_a_failed_daemon_reload_fails_the_step(tmp_path):
    p = paths(tmp_path, run=visudo_bad)
    with pytest.raises(StepError, match="daemon-reload"):
        by_name(launcher_steps(p))["cgroup-tree"].repair(MemoryJournal())


def test_mos_never_runs_daemon_reload_and_waits_for_the_boot_script(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    p = paths(tmp_path, flavour="mos", check_delegation=True, run=run, cgroup_root=str(tmp_path / "pe" / "sessions"))
    result = repair_all(launcher_steps(p), MemoryJournal())
    assert "boot" in result["cgroup-tree"] and not any("daemon-reload" in c for c in calls)


# ── the MOS boot script is shell root runs: configured paths are checked and quoted (Codex) ─────

@pytest.mark.parametrize("evil", ["/sys/fs/cgroup/x;touch /tmp/pwned", "/sys/fs/cgroup/$(id)/s", "/sys/fs/cgroup/a b/s",
                                  "/sys/fs/cgroup/`id`/s", "/sys/fs/cgroup/x\nrm -rf /", "/sys/fs/cgroup/a|b/s",
                                  "/sys/fs/cgroup/a'b/s", '/sys/fs/cgroup/a"b/s', "/sys/fs/cgroup/a&b/s"])
def test_a_cgroup_root_with_shell_metacharacters_blocks_the_mos_step_and_nothing_is_written(tmp_path, evil):
    p = paths(tmp_path, flavour="mos", cgroup_root=evil)
    step = by_name(launcher_steps(p))["cgroup-tree"]
    state = step.probe()
    assert state.blocked and "boot script" in state.detail
    with pytest.raises(StepError):
        step.repair(MemoryJournal())
    assert not Path(p.boot_script).exists()


def test_a_safe_cgroup_root_is_quoted_in_the_script_anyway(tmp_path):
    p = paths(tmp_path, flavour="mos", cgroup_root="/sys/fs/cgroup/pe-x/sessions")
    script = by_name(launcher_steps(p))["cgroup-tree"].wanted()[1].decode()
    assert "mkdir -p /sys/fs/cgroup/pe-x /sys/fs/cgroup/pe-x/sessions" in script
    assert script.count("'") == 0                    # shlex.quote leaves a plain safe word unquoted


def test_the_boot_script_is_the_only_shell_the_launcher_package_generates():
    text = (Path(__file__).resolve().parents[1] / "planet_express/missions/launcher/setup_steps.py").read_text()
    assert text.count("shlex.quote") >= 3 and "shell=True" not in text


@pytest.mark.parametrize("user", ["root ALL=(ALL) NOPASSWD: ALL", "a\nb", "a b", "A", "", "x" * 40, "a,b", "%wheel", "a:b"])
def test_a_core_user_that_is_not_a_plain_name_blocks_the_sudoers_step(tmp_path, user):
    p = paths(tmp_path, core_user=user)
    step = LauncherSudoers(p)
    assert step.probe().blocked
    with pytest.raises(StepError):
        step.repair(MemoryJournal())
    assert list(Path(p.sudoers).parent.iterdir()) == []


@pytest.mark.parametrize("helper", ["/usr/libexec/pe launch", "/usr/libexec/pe\nlaunch", "/opt/*/pe-launch",
                                    "/opt/a,b/pe-launch", "/opt/a:b"])
def test_a_helper_path_with_sudoers_metacharacters_blocks_the_sudoers_step(tmp_path, helper):
    p = paths(tmp_path, helper=helper)
    assert LauncherSudoers(p).probe().blocked


def test_the_helper_and_core_copies_of_the_conf_path_agree_and_sit_in_a_directory_of_their_own():
    assert load_helper().CONF_PATH == CONF_PATH == "/etc/planetexpress-launch/launch.conf"
    assert os.path.dirname(CONF_PATH) != "/etc/planetexpress" and os.path.dirname(CONF_PATH) != "/etc"
    from config_schema import MissionsConfig
    assert MissionsConfig().launch_conf == CONF_PATH


def test_the_default_helper_text_is_the_source_but_for_the_interpreter_line(tmp_path):
    p = paths(tmp_path, conf=CONF_PATH)
    source = (Path(__file__).resolve().parents[1] / "planet_express/missions/launcher/pe_launch.py").read_text()
    assert launcher_steps(p)[1].wanted().decode().split("\n", 1)[1] == source.split("\n", 1)[1]


def test_a_moved_conf_is_written_into_the_helper_text_and_the_helper_still_checks_the_whole_chain(tmp_path):
    p = paths(tmp_path, flavour="mos", conf=mos_conf_path(str(tmp_path / "data" / "pe")))
    (tmp_path / "data").mkdir()
    (tmp_path / "data").chmod(0o755)
    repair_all(launcher_steps(p), MemoryJournal())
    text = Path(p.helper).read_text()
    assert text.count(f'CONF_PATH = "{p.conf}"') == 1 and CONF_PATH not in text.replace(f'"{p.conf}"', "")
    assert p.conf.endswith("/launch/launch.conf") and oct(os.lstat(os.path.dirname(p.conf)).st_mode & 0o777) == "0o755"
    # a group-writable ancestor (the data pool) is refused by the same walk the helper runs
    os.chmod(tmp_path / "data" / "pe", 0o775)
    assert launcher_steps(p)[0].probe().blocked


def test_a_conf_path_the_helper_text_cannot_carry_is_refused(tmp_path):
    p = paths(tmp_path, flavour="mos", conf='/x/"; import os #/launch.conf')
    with pytest.raises(StepError):
        launcher_steps(p)[1].wanted()
