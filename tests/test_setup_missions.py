"""Mission control's host setup inside the setup plan: the `missions` answer, the launcher steps in the plan, their
handlers (apply, resume, undo) and uninstall. Handler tests run on a throwaway tree, never as root."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_host import FakeHost
from pydantic import ValidationError
from test_launcher_setup_steps import ME, MY_GID, paths
from test_setup_apply import make_plan, step
from test_setup_discover import mos, ubuntu
from test_setup_plan import LIVE_FILES, answers, mos_answers, mos_report, systemd_report

from config_schema import MissionsConfig
from planet_express.setup.answers import SetupAnswers
from planet_express.setup.apply import apply as _apply
from planet_express.setup.discover import discover
from planet_express.setup.handlers import HANDLERS, LauncherRemove, LauncherStep
from planet_express.setup.plan import plan
from planet_express.setup.steps import CATALOGUE
from planet_express.setup.undo import undo as _undo

REPO = str(Path(__file__).resolve().parent.parent)
NAMES = ["session-uid-range", "launcher-helper", "launcher-sudoers", "cgroup-tree", "workspace-root", "socket-root"]
ON = {"enabled": True}


def mreport(**kw):
    """A systemd host whose service already keeps its config inside the install directory (so no service-owned
    /etc/planetexpress is made, which mission control cannot share with launch.conf)."""
    files = {"/etc/systemd/system/casa-planetexpress.service":
             "[Service]\nWorkingDirectory=/opt/pe\nEnvironment=CASA_CONFIG=/opt/pe/config.yaml\n",
             "/opt/pe/config.yaml": "", **kw.pop("files", {})}
    return discover(ubuntu(files=files, **kw), repo_root="/opt/pe")


def launcher(p):
    return [s for s in p.steps if s.kind == "launcher.step"]


def removals(p):
    return [s for s in p.steps if s.kind == "launcher.remove"]


# -- default behaviour is byte-identical -----------------------------------------------------------------------
def test_with_missions_off_the_plan_ids_are_exactly_what_they_were_before_the_answer_existed():
    # recorded from the build before `missions` was added; a drift here means default setups changed
    assert plan(systemd_report(), answers()).to_public()["plan_id"] == "5ecffaf2883ed5a2"
    assert plan(mos_report(), mos_answers()).to_public()["plan_id"] == "41ce7901aa48290b"
    full = answers(tier="full", sudo_units=[{"unit": "x.service"}])
    assert plan(systemd_report(), full).to_public()["plan_id"] == "7cbceb0f79fccb88"
    live = discover(ubuntu(files=LIVE_FILES), repo_root="/opt/pe")
    uninstall = plan(live, answers(story="uninstall", install_dir="/home/pe/apps/pe"))
    assert uninstall.applicable and uninstall.to_public()["plan_id"] == "62a6ede66c11a511"


def test_an_explicit_off_answer_changes_nothing():
    base = plan(systemd_report(), answers()).to_public()
    for off in (None, {}, {"enabled": False}, {"enabled": False, "session_uid_min": 70000, "session_uid_max": 70100}):
        assert plan(systemd_report(), answers(missions=off)).to_public() == base


def test_the_answers_default_is_off_and_takes_its_values_from_the_config_defaults():
    assert SetupAnswers.model_validate({"run_as": "pe", "install_dir": "/opt/pe", "stacks_root": "/s"}).missions is None
    chosen = answers(missions=ON).missions
    defaults = MissionsConfig()
    assert chosen.enabled is True
    assert (chosen.session_uid_min, chosen.session_uid_max) == (defaults.session_uid_min, defaults.session_uid_max)
    assert (chosen.sock_root, chosen.cgroup_root, chosen.workspace_root) == (defaults.sock_root, defaults.cgroup_root, None)


@pytest.mark.parametrize("bad", [
    {"enabled": True, "session_uid_min": 61000, "session_uid_max": 61001},          # no room (top uid is reserved)
    {"enabled": True, "session_uid_min": 0, "session_uid_max": 100},
    {"enabled": True, "sock_root": "relative/run"},
    {"enabled": True, "workspace_root": "/a/../b"},
    {"enabled": True, "cgroup_root": "/sys/fs/cgroup"},
    {"enabled": True, "sock_root": "/run/x y"},                                     # a space would reach a unit or script
    {"enabled": True, "surprise": 1},
])
def test_unsafe_mission_answers_are_rejected_before_a_plan_exists(bad):
    with pytest.raises(ValidationError):
        answers(missions=bad)


# -- the plan --------------------------------------------------------------------------------------------------
def test_on_systemd_the_plan_gains_the_six_named_reversible_root_steps_in_order():
    p = plan(mreport(), answers(missions=ON))
    assert p.applicable, p.blocked
    steps = launcher(p)
    assert [s.target for s in steps] == NAMES and [s.params["name"] for s in steps] == NAMES
    assert all(s.needs_root and s.reversible for s in steps)
    assert {s.risk for s in steps} <= {"R2", "R3"} and {s.target: s.risk for s in steps}["launcher-sudoers"] == "R3"
    order = [s.id for s in p.steps]
    for s in p.steps:                                            # dependencies are declared earlier
        assert all(order.index(d) < order.index(s.id) for d in s.depends_on)
    assert not removals(p)


def test_a_fresh_plan_that_makes_the_service_own_etc_planetexpress_cannot_also_hold_launch_conf_there():
    p = plan(systemd_report(), answers(missions=ON))
    assert not p.applicable and not p.steps
    assert "/etc/planetexpress/launch.conf" in p.blocked[0] and "root-owned" in p.blocked[0]


def test_each_launcher_file_step_carries_what_the_scan_saw_at_its_path():
    absent = {s.target: s.params for s in launcher(plan(mreport(), answers(missions=ON)))}
    assert absent["session-uid-range"]["expect_absent"] and absent["launcher-sudoers"]["expect_absent"]
    assert not absent["workspace-root"]["expect_absent"] and "expected_sha256" not in absent["socket-root"]
    held = mreport(files={"/etc/planetexpress/launch.conf": "old\n", "/usr/libexec/planetexpress/pe-launch": "#!x\n"})
    steps = {s.target: s.params for s in launcher(plan(held, answers(missions=ON)))}
    assert len(steps["session-uid-range"]["expected_sha256"]) == 64 and not steps["session-uid-range"]["expect_absent"]
    assert len(steps["launcher-helper"]["expected_sha256"]) == 64
    assert steps["launcher-sudoers"]["expect_absent"] and "expected_sha256" not in steps["launcher-sudoers"]


def test_a_path_the_scan_did_not_look_at_records_no_expectation_so_apply_replaces_nothing_there():
    report = mreport()
    del report["existing_pe"]["present"]["/etc/sudoers.d/planetexpress-launch"]
    sudoers = next(s for s in launcher(plan(report, answers(missions=ON))) if s.target == "launcher-sudoers")
    assert not sudoers.params["expect_absent"] and "expected_sha256" not in sudoers.params


def test_a_launcher_file_that_exists_but_could_not_be_read_blocks_the_plan():
    report = mreport(files={"/etc/planetexpress/launch.conf": "old\n"})
    del report["existing_pe"]["sha256"]["/etc/planetexpress/launch.conf"]
    p = plan(report, answers(missions=ON))
    assert not p.applicable and "launch.conf" in p.blocked[0] and "could not be read" in p.blocked[0]


def test_the_launcher_steps_are_the_only_difference_from_the_default_plan():
    base = [s for s in plan(mreport(), answers()).steps]
    withm = [s for s in plan(mreport(), answers(missions=ON)).steps if s.kind != "launcher.step"]
    assert [(s.kind, s.target, s.params) for s in withm] == [(s.kind, s.target, s.params) for s in base]


def test_a_plan_with_missions_is_deterministic_and_has_its_own_id():
    a, b = plan(mreport(), answers(missions=ON)), plan(mreport(), answers(missions=ON))
    assert a.to_public()["plan_id"] == b.to_public()["plan_id"] != plan(mreport(), answers()).to_public()["plan_id"]


def test_every_launcher_step_carries_the_config_values_so_core_and_the_helper_agree():
    p = plan(mreport(), answers(missions={**ON, "session_uid_min": 70000, "session_uid_max": 70999,
                                                  "sock_root": "/run/pe-socks", "workspace_root": "/srv/ws"}))
    for s in launcher(p):
        assert (s.params["uid_min"], s.params["uid_max"]) == (70000, 70999)
        assert (s.params["sock_root"], s.params["workspace_root"]) == ("/run/pe-socks", "/srv/ws")
        assert s.params["core_user"] == "pe" and s.params["flavour"] == "systemd"
        assert s.params["cgroup_root"] == MissionsConfig().cgroup_root


def test_the_workspace_root_defaults_to_beside_the_state_database():
    p = plan(mreport(), answers(missions=ON))
    assert {s.params["workspace_root"] for s in launcher(p)} == {"/opt/pe/state/mission-workspaces"}


def test_the_plan_preview_shows_the_generated_sudoers_text():
    sudoers = {s.target: s for s in launcher(plan(mreport(), answers(missions=ON)))}["launcher-sudoers"]
    text = sudoers.preview["content"]
    assert sudoers.preview["type"] == "file" and sudoers.preview["path"] == "/etc/sudoers.d/planetexpress-launch"
    assert 'pe ALL=(root) NOPASSWD: /usr/libexec/planetexpress/pe-launch ""' in text
    assert "env_reset, !setenv, !env_editor" in text and sudoers.preview["mode"] == "0440"


def test_every_launcher_preview_says_what_will_be_written_and_where():
    by = {s.target: s for s in launcher(plan(mreport(), answers(missions=ON)))}
    for s in by.values():
        assert s.preview["type"] in ("file", "text") and s.preview["path"]
        assert s.preview.get("content") or s.preview["text"]
    assert "61000-61999" in by["session-uid-range"].preview["text"]
    assert by["session-uid-range"].preview["path"] == "/etc/planetexpress/launch.conf"
    assert "Delegate=yes" in by["cgroup-tree"].preview["content"]
    assert by["workspace-root"].preview["type"] == "text" and "/opt/pe/state/mission-workspaces" in by["workspace-root"].preview["text"]


def test_on_mos_there_is_no_sudoers_and_the_cgroup_script_is_the_one_the_boot_hook_already_runs():
    p = plan(mos_report(), mos_answers(missions=ON))
    assert p.applicable, p.blocked
    steps = launcher(p)
    assert [s.target for s in steps] == [n for n in NAMES if n != "launcher-sudoers"]
    assert not any(s.kind == "sudoers.install" for s in p.steps)
    cgroup = {s.target: s for s in steps}["cgroup-tree"]
    assert cgroup.params["boot_script"] == "/mnt/data/pe/launcher-boot.sh" and cgroup.params["flavour"] == "mos"
    assert cgroup.preview["path"] == "/mnt/data/pe/launcher-boot.sh" and cgroup.preview["content"].startswith("#!/bin/sh")
    assert cgroup.risk == "R3"
    # the boot hook the plan installs is untouched, and it already calls $PE_HOME/launcher-boot.sh
    hook = {s.kind: s for s in p.steps}["boot_hook.install"].params["hooks"]["post-start.sh"]
    assert '"$PE_HOME/launcher-boot.sh"' in hook and "PE_HOME=/mnt/data/pe\n" in hook
    assert any("no sudo" in line.lower() for line in p.will_not_touch)


def test_the_mos_boot_script_follows_the_pool_home_not_an_assumed_path():
    p = plan(mos_report(), mos_answers(install_dir="/mnt/data/pool-home/planet-express", missions=ON))
    assert p.applicable, p.blocked
    cgroup = next(s for s in launcher(p) if s.target == "cgroup-tree")
    assert cgroup.params["boot_script"] == "/mnt/data/pool-home/launcher-boot.sh"


def test_the_plan_says_the_config_must_match_and_that_the_service_needs_a_restart():
    p = plan(mreport(), answers(missions=ON))
    text = " ".join(p.warnings)
    assert "missions.enabled" in text and "61000" in text and "restart" in text.lower()
    assert any("sudo rule" in line for line in p.will_not_touch)


def test_the_cli_plans_with_missions_and_still_never_imports_config(tmp_path):
    (tmp_path / "answers.json").write_text(json.dumps({"run_as": "pe", "install_dir": "/opt/pe",
                                                       "stacks_root": "/home/pe/stacks", "missions": ON}))
    (tmp_path / "report.json").write_text(json.dumps(mreport()))
    code = ("import sys; from planet_express.setup.__main__ import main; "
            f"main(['plan', '--answers', r'{tmp_path / 'answers.json'}', '--discovery', r'{tmp_path / 'report.json'}']); "
            "sys.stderr.write('CONFIG_IMPORTED' if 'config' in sys.modules else 'clean')")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, check=True)
    assert out.stderr.strip() == "clean"
    assert [s["target"] for s in json.loads(out.stdout)["steps"] if s["kind"] == "launcher.step"] == NAMES


def test_the_catalogue_and_the_handlers_know_both_kinds():
    for kind in ("launcher.step", "launcher.remove"):
        assert kind in CATALOGUE and kind in HANDLERS
        assert CATALOGUE[kind].needs_root and CATALOGUE[kind].reversible


def test_a_mos_launcher_step_must_name_its_boot_script_and_a_range_must_have_room():
    base = {"name": "cgroup-tree", "core_user": "root", "uid_min": 61000, "uid_max": 61999, "sock_root": "/run/s",
                "cgroup_root": "/sys/fs/cgroup/pe/sessions", "workspace_root": "/w"}
    with pytest.raises(ValidationError):
        CATALOGUE["launcher.step"].model(flavour="mos", **base)
    with pytest.raises(ValidationError):
        CATALOGUE["launcher.step"].model(flavour="systemd", **{**base, "uid_max": 61001})
    with pytest.raises(ValidationError):
        CATALOGUE["launcher.step"].model(flavour="systemd", **{**base, "name": "run-anything"})


# -- handlers, on a throwaway tree ------------------------------------------------------------------------------
def params_for(p):
    return dict(core_user=p.core_user, flavour=p.flavour, uid_min=p.uid_min, uid_max=p.uid_max, sock_root=p.sock_root,
                cgroup_root=p.cgroup_root, workspace_root=p.workspace_root,
                **({"boot_script": p.boot_script} if p.flavour == "mos" else {}))


def names_for(p):
    return [n for n in NAMES if not (n == "launcher-sudoers" and p.flavour != "systemd")]


def install_plan(p, kind="launcher.step", names=None):
    names = names or names_for(p)
    return make_plan([step(i, kind, n, name=n, **params_for(p)) for i, n in enumerate(names, 1)])


def handlers_for(p):
    def factory(ctx):                            # the throwaway tree's Paths instead of the host's
        return p
    return {**HANDLERS, "launcher.step": LauncherStep(factory), "launcher.remove": LauncherRemove(factory)}


def run(op, plan_, p, tmp_path, name="j"):
    kw = {"host": FakeHost(), "journal_root": tmp_path / name, "handlers": handlers_for(p), "trusted_uids": frozenset({0, ME})}
    if op == "apply":
        return _apply(plan_, replan=lambda: plan_, **kw)
    return _undo(plan_, **kw)


def snapshot(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.name in ("j",) or "j" in path.relative_to(root).parts[:1]:
            continue
        info = path.lstat()
        out[str(path.relative_to(root))] = ("dir" if path.is_dir() else path.read_bytes(), oct(info.st_mode & 0o7777))
    return out


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_apply_makes_every_launcher_step_true_and_a_second_apply_changes_nothing(tree, tmp_path):
    p = paths(tree)
    plan_ = install_plan(p)
    result = run("apply", plan_, p, tmp_path)
    assert result.status == "done", result.reason
    assert Path(p.conf).read_text() == p.launch_conf().render() and oct(Path(p.helper).stat().st_mode & 0o777) == "0o755"
    assert "pe ALL=(root) NOPASSWD:" in Path(p.sudoers).read_text() and "Delegate=yes" in Path(p.unit_dropin).read_text()
    assert oct(os.lstat(p.sock_root).st_mode & 0o7777) == "0o711" and os.path.isdir(p.workspace_root)
    before = snapshot(tree)
    again = run("apply", plan_, p, tmp_path, name="j2")
    assert again.status == "done" and snapshot(tree) == before


def test_apply_refuses_to_replace_a_file_that_is_there_and_was_not_what_the_plan_saw(tree, tmp_path):
    p = paths(tree)
    Path(p.conf).parent.mkdir(parents=True, exist_ok=True)
    Path(p.conf).parent.chmod(0o755)
    Path(p.conf).write_text("the operator's own\n")
    result = run("apply", install_plan(p), p, tmp_path)
    assert result.status == "stopped" and result.step == "s01" and "changed since the plan" in result.reason
    assert Path(p.conf).read_text() == "the operator's own\n" and not Path(p.helper).exists()


def test_a_file_that_is_what_the_plan_saw_is_replaced_and_undo_puts_it_back(tree, tmp_path):
    import hashlib
    p = paths(tree)
    Path(p.conf).parent.mkdir(parents=True, exist_ok=True)
    Path(p.conf).parent.chmod(0o755)
    Path(p.conf).write_text("old launch rules\n")
    seen = hashlib.sha256(b"old launch rules\n").hexdigest()
    steps = [step(1, "launcher.step", "session-uid-range", name="session-uid-range", expected_sha256=seen, **params_for(p))]
    plan_ = make_plan(steps)
    assert run("apply", plan_, p, tmp_path).status == "done" and Path(p.conf).read_text() == p.launch_conf().render()
    assert run("undo", plan_, p, tmp_path).status == "done" and Path(p.conf).read_text() == "old launch rules\n"


def test_a_file_created_after_the_plan_expected_it_absent_is_not_overwritten(tree, tmp_path):
    p = paths(tree)
    Path(p.conf).parent.mkdir(parents=True, exist_ok=True)
    Path(p.conf).parent.chmod(0o755)
    Path(p.conf).write_text("appeared later\n")
    steps = [step(1, "launcher.step", "session-uid-range", name="session-uid-range", expect_absent=True, **params_for(p))]
    result = run("apply", make_plan(steps), p, tmp_path)
    assert result.status == "stopped" and "approved to create it" in result.reason
    assert Path(p.conf).read_text() == "appeared later\n"


def test_the_directory_launch_conf_sits_in_is_never_chowned_or_chmodded_by_setup(tree, tmp_path):
    p = paths(tree)
    directory = Path(p.conf).parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o770)                               # not an acceptable parent: group-writable
    result = run("apply", install_plan(p), p, tmp_path)
    assert result.status == "stopped" and result.step == "s01" and not Path(p.conf).exists()
    assert oct(directory.stat().st_mode & 0o7777) == "0o770"


def test_undoing_a_removal_does_not_reset_a_parent_directory_that_changed_meanwhile(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    gone = install_plan(p, kind="launcher.remove", names=["session-uid-range"])
    assert run("apply", gone, p, tmp_path, name="remove").status == "done"
    directory = Path(p.conf).parent
    directory.chmod(0o750)                               # somebody tightened it while the file was away
    assert run("undo", gone, p, tmp_path, name="remove").status == "done"
    assert oct(directory.stat().st_mode & 0o7777) == "0o750" and Path(p.conf).exists()


def test_dry_run_style_checks_change_nothing_and_say_would_run(tree, tmp_path):
    p = paths(tree)
    before = snapshot(tree)
    result = _apply(install_plan(p), host=FakeHost(), journal_root=tmp_path / "j", replan=lambda: install_plan(p),
                    handlers=handlers_for(p), dry_run=True, trusted_uids=frozenset({0, ME}))
    assert result.status == "dry_run" and {c["check"] for c in result.checks} == {"would run"}
    assert snapshot(tree) == before


def test_undo_returns_the_tree_to_what_it_was(tree, tmp_path):
    p = paths(tree)
    before = snapshot(tree)
    plan_ = install_plan(p)
    assert run("apply", plan_, p, tmp_path).status == "done" and snapshot(tree) != before
    result = run("undo", plan_, p, tmp_path)
    assert result.status == "done", (result.reason, result.step)
    assert snapshot(tree) == before


def test_undo_refuses_over_a_file_somebody_edited_after_setup_wrote_it(tree, tmp_path):
    p = paths(tree)
    plan_ = install_plan(p)
    run("apply", plan_, p, tmp_path)
    Path(p.sudoers).chmod(0o640)
    Path(p.sudoers).write_text(Path(p.sudoers).read_text() + "pe ALL=(ALL) NOPASSWD: ALL\n")
    result = run("undo", plan_, p, tmp_path)
    assert result.status == "stopped" and result.step == "s03" and "changed after setup wrote it" in result.reason
    assert Path(p.sudoers).exists()                       # nothing reverted over it
    assert "s03" in result.remaining


def test_a_step_that_was_already_in_place_is_satisfied_and_undo_leaves_it(tree, tmp_path):
    p = paths(tree)
    plan_ = install_plan(p)
    run("apply", plan_, p, tmp_path, name="first")
    assert run("apply", plan_, p, tmp_path, name="second").status == "done"
    result = run("undo", plan_, p, tmp_path, name="second")
    assert result.status == "done" and result.undone == []
    assert Path(p.helper).exists()


def test_a_blocked_step_stops_the_apply_with_the_reason_and_writes_nothing_after_it(tree, tmp_path):
    p = paths(tree)
    Path(p.passwd).write_text(Path(p.passwd).read_text() + "stray:x:61005:61005::/:/bin/false\n")
    result = run("apply", install_plan(p), p, tmp_path)
    assert result.status == "stopped" and result.step == "s01" and "61000-61999" in result.reason
    assert not Path(p.conf).exists() and not Path(p.helper).exists()


def test_a_visudo_rejection_stops_the_apply_and_installs_no_sudoers(tree, tmp_path):
    from test_launcher_setup_steps import visudo_bad
    p = paths(tree, run=visudo_bad)
    result = run("apply", install_plan(p), p, tmp_path)
    assert result.status == "stopped" and result.step == "s03" and "visudo" in result.reason
    assert not Path(p.sudoers).exists()


def test_a_missing_core_account_is_refused_by_the_default_factory_not_guessed():
    handler = LauncherStep()
    ctx = type("C", (), {"host": FakeHost(), "step": make_plan([step(1, "launcher.step", "workspace-root", name="workspace-root",
                                                                    **params_for(Paths_stub()))]).steps[0]})()
    outcome = handler.check(ctx)
    assert type(outcome).__name__ == "Refuse" and "ghost" in outcome.reason


def Paths_stub():
    from planet_express.missions.launcher.setup_steps import Paths
    return Paths(core_uid=1, core_gid=1, core_user="ghost", workspace_root="/w")


def cgroup_mounts(root: Path) -> str:
    """A mount table that makes the temp tree's cgroup root sit below a cgroup2 mount."""
    path = root.parent / (root.name + "-mountinfo")
    path.write_text(f"30 25 0:26 / {root} rw - cgroup2 cgroup2 rw\n")
    return str(path)


def test_a_cgroup_root_not_on_cgroup2_stops_before_any_launcher_file_is_written(tree, tmp_path):
    p = paths(tree, check_delegation=True)                    # the real mount table: the temp tree is not cgroup2
    result = run("apply", install_plan(p), p, tmp_path)
    assert result.status == "stopped" and result.step == "s01" and "cgroup v2" in result.reason
    assert not Path(p.conf).exists() and not Path(p.helper).exists()


def test_undoing_a_removal_refuses_to_restore_into_a_directory_others_can_now_write(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    gone = install_plan(p, kind="launcher.remove", names=["launcher-helper"])
    assert run("apply", gone, p, tmp_path, name="remove").status == "done"
    Path(p.helper).parent.chmod(0o777)
    result = run("undo", gone, p, tmp_path, name="remove")
    assert result.status == "stopped" and "not restoring" in result.reason and not Path(p.helper).exists()


def test_removal_keeps_a_directory_whose_group_changed(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    other = next((g for g in os.getgroups() if g != MY_GID), None)
    if other is None:
        pytest.skip("needs a second group")
    os.chown(p.sock_root, -1, other)
    gone = install_plan(p, kind="launcher.remove", names=["socket-root"])
    assert run("apply", gone, p, tmp_path, name="remove").status == "done" and Path(p.sock_root).is_dir()


def test_the_cgroup_step_is_applied_once_written_even_before_the_service_restarts(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    p = paths(root, check_delegation=True, mountinfo=cgroup_mounts(root))                 # the delegated cgroup does not exist yet
    only = install_plan(p, names=["cgroup-tree"])
    result = run("apply", only, p, tmp_path)
    assert result.status == "done", result.reason
    assert "Delegate=yes" in Path(p.unit_dropin).read_text()


def test_a_crash_after_the_helper_was_written_resumes_and_undo_still_cleans_up(tree, tmp_path):
    from fake_host import Crash
    p = paths(tree)
    plan_ = install_plan(p)
    handlers = handlers_for(p)
    real = handlers["launcher.step"].verify
    calls = []

    def dying(ctx):
        calls.append(ctx.step.id)
        if ctx.step.id == "s02":
            raise Crash()
        return real(ctx)
    handlers["launcher.step"].verify = dying
    kw = {"host": FakeHost(), "journal_root": tmp_path / "j", "handlers": handlers, "trusted_uids": frozenset({0, ME})}
    with pytest.raises(Crash):
        _apply(plan_, replan=lambda: plan_, **kw)
    handlers["launcher.step"].verify = real
    assert _apply(plan_, replan=lambda: plan_, **kw).status == "done"
    assert _undo(plan_, **kw).status == "done"
    assert not Path(p.helper).exists() and not Path(p.conf).exists()


def test_a_mission_directory_that_already_exists_with_other_ownership_or_mode_is_not_changed(tree, tmp_path):
    p = paths(tree)
    Path(p.workspace_root).mkdir(parents=True)
    Path(p.workspace_root).chmod(0o700)
    (Path(p.workspace_root) / "somebody-elses-data").write_text("x")
    names = ["workspace-root"]
    result = run("apply", install_plan(p, names=names), p, tmp_path)
    assert result.status == "stopped" and "does not change a directory it did not make" in result.reason
    assert oct(Path(p.workspace_root).stat().st_mode & 0o7777) == "0o700"


def test_a_drop_in_written_but_never_reloaded_is_not_done_until_systemd_has_reloaded(tree, tmp_path):
    state = {"reloaded": False, "reload_works": False}

    def systemctl(argv, **kw):
        if "daemon-reload" in argv:
            state["reloaded"] = state["reload_works"]
            return subprocess.CompletedProcess(argv, 0 if state["reload_works"] else 1, b"", b"")
        pending = b"yes\n" if Path(p.unit_dropin).exists() and not state["reloaded"] else b"no\n"
        return subprocess.CompletedProcess(argv, 0, pending, b"")

    p = paths(tree, check_delegation=True, run=systemctl, mountinfo=cgroup_mounts(tree))
    only = install_plan(p, names=["cgroup-tree"])
    first = run("apply", only, p, tmp_path)
    assert first.status == "stopped" and "daemon-reload" in first.reason and Path(p.unit_dropin).exists()
    state["reload_works"] = True
    second = run("apply", only, p, tmp_path)
    assert second.status == "done" and state["reloaded"]


def test_directories_made_before_a_crash_are_recorded_and_undo_removes_them(tree, tmp_path, monkeypatch):
    from fake_host import Crash

    from planet_express.missions.launcher import setup_steps
    p = paths(tree)
    plan_ = install_plan(p, names=["workspace-root"])
    real = setup_steps.ensure_dir

    def crash_after_making(*args, **kw):
        real(*args, **kw)
        raise Crash()
    monkeypatch.setattr(setup_steps, "ensure_dir", crash_after_making)
    kw = {"host": FakeHost(), "journal_root": tmp_path / "j", "handlers": handlers_for(p), "trusted_uids": frozenset({0, ME})}
    with pytest.raises(Crash):
        _apply(plan_, replan=lambda: plan_, **kw)
    monkeypatch.setattr(setup_steps, "ensure_dir", real)
    assert Path(p.workspace_root).is_dir()
    assert _apply(plan_, replan=lambda: plan_, **kw).status == "done"
    assert _undo(plan_, **kw).status == "done"
    assert not Path(p.workspace_root).exists() and not Path(p.workspace_root).parent.exists()


# -- removal (uninstall) ---------------------------------------------------------------------------------------
def test_removal_takes_away_exactly_what_setup_wrote_and_undoing_it_puts_it_back(tree, tmp_path):
    p = paths(tree)
    assert run("apply", install_plan(p), p, tmp_path, name="install").status == "done"
    installed = snapshot(tree)
    gone = install_plan(p, kind="launcher.remove", names=list(reversed(names_for(p))))
    result = run("apply", gone, p, tmp_path, name="remove")
    assert result.status == "done", result.reason
    assert not any(Path(x).exists() for x in (p.conf, p.helper, p.sudoers, p.unit_dropin, p.sock_root, p.workspace_root))
    assert run("undo", gone, p, tmp_path, name="remove").status == "done"
    assert snapshot(tree) == installed


def test_removal_leaves_a_file_that_is_not_what_setup_would_write(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    Path(p.helper).write_text("#!/bin/sh\n# the operator's own\n")
    gone = install_plan(p, kind="launcher.remove", names=["launcher-helper"])
    assert run("apply", gone, p, tmp_path, name="remove").status == "done"
    assert Path(p.helper).read_text().endswith("operator's own\n")


def test_removal_keeps_a_directory_that_has_content(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    (Path(p.workspace_root) / "session-1").mkdir()
    gone = install_plan(p, kind="launcher.remove", names=["workspace-root", "socket-root"])
    assert run("apply", gone, p, tmp_path, name="remove").status == "done"
    assert Path(p.workspace_root, "session-1").exists() and not Path(p.sock_root).exists()


def test_removal_of_something_never_set_up_is_satisfied_and_changes_nothing(tree, tmp_path):
    p = paths(tree)
    before = snapshot(tree)
    gone = install_plan(p, kind="launcher.remove", names=list(reversed(names_for(p))))
    assert run("apply", gone, p, tmp_path).status == "done" and snapshot(tree) == before


def test_removal_will_not_follow_a_symlink_where_a_file_should_be(tree, tmp_path):
    p = paths(tree)
    run("apply", install_plan(p), p, tmp_path, name="install")
    target = tree / "elsewhere"
    target.write_text("keep me")
    os.unlink(p.helper)
    os.symlink(target, p.helper)
    gone = install_plan(p, kind="launcher.remove", names=["launcher-helper"])
    result = run("apply", gone, p, tmp_path, name="remove")
    assert result.status == "stopped" and target.read_text() == "keep me"


# -- uninstall plans -------------------------------------------------------------------------------------------
def live_uninstall(**over):
    live = discover(ubuntu(files=LIVE_FILES), repo_root="/opt/pe")
    return plan(live, answers(story="uninstall", install_dir="/home/pe/apps/pe", **over))


def test_uninstall_with_missions_on_removes_the_launcher_steps_after_the_services_are_stopped():
    p = live_uninstall(missions=ON)
    assert p.applicable, p.blocked
    assert [s.target for s in removals(p)] == list(reversed(NAMES))
    assert all(s.needs_root and s.reversible for s in removals(p))
    ids = [s.id for s in p.steps]
    stops = [s.id for s in p.steps if s.kind == "service.disable"]
    assert stops and max(ids.index(i) for i in stops) < min(ids.index(s.id) for s in removals(p))
    assert any("workspace" in line.lower() for line in p.will_not_touch)


def test_uninstall_without_the_answer_is_exactly_as_before():
    assert not removals(live_uninstall())
    assert live_uninstall().to_public()["plan_id"] == "62a6ede66c11a511"


def test_a_mos_uninstall_has_no_sudoers_removal_and_removes_the_boot_script():
    files = {"/boot/optional/scripts/post-start.sh": "#!/bin/sh\n# BEGIN planetexpress (x)\nfoo\n# END planetexpress\n",
             "/boot/optional/scripts/shutdown.sh": "#!/bin/sh\n# BEGIN planetexpress (x)\nbar\n# END planetexpress\n",
             "/etc/init.d/casa-planetexpress": "#!/bin/sh\n", "/etc/init.d/casa-dashboard": "#!/bin/sh\n",
             "/mnt/data/pe/config.yaml": "{}\n"}
    live = discover(mos(files=files), repo_root="/mnt/data/pe/planet-express")
    p = plan(live, mos_answers(story="uninstall", missions=ON))
    assert p.applicable, p.blocked
    assert [s.target for s in removals(p)] == [n for n in reversed(NAMES) if n != "launcher-sudoers"]
    assert next(s for s in removals(p) if s.target == "cgroup-tree").params["boot_script"] == "/mnt/data/pe/launcher-boot.sh"
