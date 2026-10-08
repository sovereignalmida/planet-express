"""undo (A3): every handler's inverse, the refusals, and a crash injected at every operation of an undo."""
from functools import partial

import pytest

from fake_host import Crash, FaultyHost
from planet_express.setup.apply import apply as _apply
from planet_express.setup.host import RunResult
from planet_express.setup.journal import Journal
from planet_express.setup.undo import undo as _undo
from test_setup_apply import SECRET, fresh_host, make_plan, replace_plan, sha, standard_plan, step
from test_setup_handlers_services import (BODY, HOOKS, INIT, UNIT, hook_host, init_host, init_plan, systemd_host, systemctl,
                                          unit_plan)

apply = partial(_apply, evidence_dir="/journal/evidence")
undo = partial(_undo, evidence_dir="/journal/evidence")


def run_apply(plan, host, tmp_path, name="j"):
    return apply(plan, host=host, journal_root=tmp_path / name, replan=lambda: plan)


def run_undo(plan, host, tmp_path, name="j"):
    return undo(plan, host=host, journal_root=tmp_path / name)


def tree(host):
    """The host without the journal's own evidence directory, which undo deliberately leaves as the record."""
    return {k: v for k, v in host.tree().items() if not k.startswith("/journal/")}


# -- files and directories -------------------------------------------------------------------------------------
def test_applying_then_undoing_a_fresh_install_leaves_the_host_exactly_as_it_was(tmp_path):
    plan, host = standard_plan(), fresh_host()
    before = tree(host)
    assert run_apply(plan, host, tmp_path).status == "done"
    assert tree(host) != before
    result = run_undo(plan, host, tmp_path)
    assert result.status == "done", result.reason
    assert tree(host) == before


def test_a_replaced_file_comes_back_with_its_old_content_mode_and_owner(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"old\n", mode=0o640, uid=1000, gid=1000)
    plan = replace_plan("old\n")
    before = tree(host)
    assert run_apply(plan, host, tmp_path).status == "done" and host.nodes["/etc/app.conf"].data == b"new\n"
    assert run_undo(plan, host, tmp_path).status == "done"
    assert tree(host) == before


def test_a_file_edited_after_setup_wrote_it_is_not_reverted_over(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"old\n")
    plan = replace_plan("old\n")
    run_apply(plan, host, tmp_path)
    host.add_file("/etc/app.conf", b"new\nplus the operator's own line\n")
    result = run_undo(plan, host, tmp_path)
    assert result.status == "stopped" and result.step == "s01" and "left as it is" in result.reason
    assert host.nodes["/etc/app.conf"].data.endswith(b"own line\n") and result.remaining == ["s01"]


def test_a_created_file_replaced_by_something_else_is_not_deleted(tmp_path):
    plan, host = standard_plan(), fresh_host()
    run_apply(plan, host, tmp_path)
    host.add_file("/etc/pe/config.yaml", b"forbidden: []\n", mode=0o640, uid=1000, gid=1000)   # same bytes, new inode
    result = run_undo(plan, host, tmp_path)
    assert result.status == "stopped" and result.step == "s03" or result.step == "s02"
    assert "/etc/pe/config.yaml" in host.nodes


def test_a_directory_that_has_gained_files_is_not_removed(tmp_path):
    plan, host = standard_plan(), fresh_host()
    run_apply(plan, host, tmp_path)
    run_undo(plan, host, tmp_path)               # clean first
    host2 = fresh_host()
    run_apply(plan, host2, tmp_path, "j2")
    host2.add_file("/etc/pe/operators-notes.txt", b"mine")
    result = run_undo(plan, host2, tmp_path, "j2")
    assert result.status == "stopped" and result.step == "s01" and "not empty" in result.reason
    assert "/etc/pe/operators-notes.txt" in host2.nodes


def test_undo_twice_changes_nothing_the_second_time(tmp_path):
    plan, host = standard_plan(), fresh_host()
    run_apply(plan, host, tmp_path)
    assert run_undo(plan, host, tmp_path).status == "done"
    after_first = tree(host)
    again = run_undo(plan, host, tmp_path)
    assert again.status == "done" and again.undone == [] and tree(host) == after_first


def test_a_plan_that_was_never_applied_has_nothing_to_undo(tmp_path):
    result = run_undo(standard_plan(), fresh_host(), tmp_path)
    assert result.status == "refused" and "never applied" in result.reason


def test_a_step_that_found_its_work_already_done_is_not_undone(tmp_path):
    host = fresh_host()
    host.add_dir("/etc/pe", mode=0o750, uid=1000, gid=1000)          # already there
    plan = standard_plan()
    run_apply(plan, host, tmp_path)
    run_undo(plan, host, tmp_path)
    assert "/etc/pe" in host.nodes and "/etc/pe/config.yaml" not in host.nodes


def test_apply_after_undo_works_again(tmp_path):
    plan, host = standard_plan(), fresh_host()
    run_apply(plan, host, tmp_path)
    run_undo(plan, host, tmp_path)
    assert run_apply(plan, host, tmp_path).status == "done" and "/etc/pe/config.yaml" in host.nodes


def test_no_secret_reaches_the_journal_through_undo(tmp_path):
    plan, host = standard_plan(), fresh_host()
    run_apply(plan, host, tmp_path)
    run_undo(plan, host, tmp_path)
    assert SECRET not in (tmp_path / "j" / plan.to_public()["plan_id"] / "events.jsonl").read_text()


# -- the sweep -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("after", [False, True])
def test_a_crash_at_any_operation_of_an_undo_resumes_to_the_clean_result(tmp_path, after):
    plan = standard_plan()

    def applied():
        host = fresh_host()
        host.add_file("/etc/app.conf", b"old\n")
        return host
    both = make_plan(list(plan.steps) + [replace_plan("old\n").steps[0].__class__(
        "s04", "file.write", "x", "/etc/app.conf", "R1", True, False, (), replace_plan("old\n").steps[0].params, {})],
        plan.secrets)
    start = applied()
    before = tree(start)
    counter = FaultyHost(start)
    assert run_apply(both, start, tmp_path, "clean").status == "done"
    assert run_undo(both, counter, tmp_path, "clean").status == "done" and tree(start) == before
    for crash_at in range(1, counter.ops + 1):
        host, root = applied(), f"u{crash_at}"
        assert run_apply(both, host, tmp_path, root).status == "done"
        with pytest.raises(Crash):
            undo(both, host=FaultyHost(host, crash_at=crash_at, after=after), journal_root=tmp_path / root,
                 evidence_dir="/journal/evidence")
        assert run_undo(both, host, tmp_path, root).status == "done", (crash_at, after)
        assert tree(host) == before, (crash_at, after)


# -- services, hooks, the rest ---------------------------------------------------------------------------------
def test_a_unit_is_removed_and_systemd_told(tmp_path):
    host, plan = systemd_host(), unit_plan()
    before = tree(host)
    run_apply(plan, host, tmp_path)
    host.commands.clear()
    assert run_undo(plan, host, tmp_path).status == "done" and tree(host) == before
    assert ["systemctl", "daemon-reload"] in host.commands


def test_an_init_script_and_its_defaults_both_go_back(tmp_path):
    host = init_host(defaults=b"stale\n")
    plan = init_plan(defaults_if_exists="replace", defaults_expected_sha256=sha("stale\n"))
    before = tree(host)
    run_apply(plan, host, tmp_path)
    assert host.nodes[INIT].data != b"old\n"
    assert run_undo(plan, host, tmp_path).status == "done" and tree(host) == before


def test_boot_hooks_lose_the_block_and_keep_the_operators_commands(tmp_path):
    mine = "#!/bin/sh\nmodprobe nct6775\n"
    host = hook_host(**{"post-start.sh": mine})
    from test_setup_handlers_services import hook_plan
    plan = hook_plan(hooks={"post-start.sh": BODY, "shutdown.sh": BODY}, expect_absent=["shutdown.sh"],
                     expected_sha256={"post-start.sh": sha(mine)})
    before = tree(host)
    run_apply(plan, host, tmp_path)
    assert b"pe_post_start" in host.nodes[f"{HOOKS}/post-start.sh"].data
    assert run_undo(plan, host, tmp_path).status == "done"
    assert tree(host) == before and host.nodes[f"{HOOKS}/post-start.sh"].data.decode() == mine


def test_a_hook_the_operator_edited_after_setup_is_not_reverted(tmp_path):
    mine = "#!/bin/sh\nmodprobe nct6775\n"
    host = hook_host(**{"post-start.sh": mine})
    from test_setup_handlers_services import hook_plan
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha(mine)})
    run_apply(plan, host, tmp_path)
    host.nodes[f"{HOOKS}/post-start.sh"].data += b"echo added later\n"
    host.nodes[f"{HOOKS}/post-start.sh"].ino += 1000
    result = run_undo(plan, host, tmp_path)
    assert result.status == "stopped" and b"added later" in host.nodes[f"{HOOKS}/post-start.sh"].data


def test_enable_and_start_are_undone_but_a_service_that_was_already_running_is_left(tmp_path):
    host, state = systemd_host(), {"enabled": False, "active": False}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)

    def stop(h, argv):
        state["active"] = False
        return RunResult(0, "")

    def disable(h, argv):
        state["enabled"] = False
        return RunResult(0, "")
    host.on(lambda a: a[:2] == ["systemctl", "stop"], stop)
    host.on(lambda a: a[:2] == ["systemctl", "disable"], disable)
    from test_setup_handlers_services import enable_plan
    plan = enable_plan(start=True)
    run_apply(plan, host, tmp_path)
    assert state == {"enabled": True, "active": True}
    assert run_undo(plan, host, tmp_path).status == "done" and state == {"enabled": False, "active": False}
    # already enabled and running before setup: nothing to undo, and nothing is stopped
    state.update(enabled=True, active=True)
    host.commands.clear()
    plan2 = enable_plan(start=True)
    run_apply(plan2, host, tmp_path, "k")
    result = run_undo(plan2, host, tmp_path, "k")
    assert result.status == "done" and result.undone == [] and state == {"enabled": True, "active": True}
    assert not [c for c in host.commands if c[1] in ("stop", "disable")]


def test_accounts_and_the_snapshot_are_named_as_not_undone_not_hidden(tmp_path):
    from test_setup_handlers_privilege import access_host, access_plan
    host, plan = access_host(), access_plan()
    run_apply(plan, host, tmp_path)
    result = run_undo(plan, host, tmp_path)
    assert result.status == "done" and result.not_undone and "left in place" in result.not_undone[0]["reason"]


def test_a_virtualenv_setup_created_is_removed_and_one_it_did_not_create_is_not(tmp_path):
    from test_setup_handlers_env import PY, VENV, env_plan, healthy_checks, host_with_checkout, venv_cmd
    host = host_with_checkout()
    healthy_checks(host)
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    state = {"made": False}
    host.on(lambda a: a[:2] == [PY, "-c"], lambda h, a: RunResult(0 if state["made"] else 1, ""))
    host.on(lambda a: a[:3] == [PY, "-m", "pip"], lambda h, a: (state.update(made=True), RunResult(0, "pip"))[1])
    plan = env_plan()
    assert run_apply(plan, host, tmp_path).status == "done" and VENV in host.nodes
    assert run_undo(plan, host, tmp_path).status == "done" and VENV not in host.nodes
    pre = host_with_checkout()
    pre.add_dir(VENV)
    pre.add_file(PY, b"", mode=0o755)
    healthy_checks(pre)
    result_apply = run_apply(plan, pre, tmp_path, "k")
    assert result_apply.status == "done"
    assert run_undo(plan, pre, tmp_path, "k").status == "done" and VENV in pre.nodes


def test_a_tightened_directory_gets_its_old_mode_back_unless_it_was_changed_since(tmp_path):
    from test_setup_handlers_privilege import tighten_plan
    host = fresh_host()
    host.add_dir("/pe", mode=0o755)
    host.add_dir("/pe/data", mode=0o755, uid=1000, gid=1000)
    plan = tighten_plan()
    run_apply(plan, host, tmp_path)
    assert run_undo(plan, host, tmp_path).status == "done" and host.nodes["/pe/data"].mode == 0o755
    run_apply(plan, host, tmp_path, "k")
    host.nodes["/pe/data"].mode = 0o750
    assert run_undo(plan, host, tmp_path, "k").status == "stopped"


# -- the whole pipeline ----------------------------------------------------------------------------------------
def pipeline():
    from planet_express.setup.plan import Step
    mine = "#!/bin/sh\nmodprobe nct6775\n"
    host = systemd_host()
    host.add_dir("/etc/sudoers.d", mode=0o750)
    host.add_dir(HOOKS)
    host.add_file(f"{HOOKS}/post-start.sh", mine.encode(), mode=0o755)
    host.add_file("/etc/app.conf", b"old\n")
    state = {"enabled": False, "active": False}
    systemctl(host, state)

    def remove(h, argv):
        state[{"stop": "active", "disable": "enabled"}[argv[1]]] = False
        return RunResult(0, "")
    host.on(lambda a: a[:2] in (["systemctl", "stop"], ["systemctl", "disable"]), remove)
    host.on(lambda a: a[:2] == ["systemctl", "show"],
            lambda h, a: RunResult(0, f"LoadState=loaded\nNeedDaemonReload={'yes' if h.reload_needed['yes'] else 'no'}\n"))
    host.on(lambda a: a[:3] == ["visudo", "-c", "-f"] or a == ["visudo", "-c"], RunResult(0, ""))
    # the unit being written makes systemd want a reload; daemon-reload clears it
    host.on(lambda a: a[:2] == ["systemctl", "daemon-reload"], lambda h, a: (h.reload_needed.update(yes=False), RunResult(0, ""))[1])
    steps = [
        step(1, "dir.ensure", "/etc/pe", path="/etc/pe", mode="0750", owner="svc", group="svc"),
        step(2, "file.write", "/etc/pe/config.yaml", path="/etc/pe/config.yaml", content="a: 1\n", mode="0640",
             owner="svc", group="svc", expect_absent=True),
        step(3, "file.write", "/etc/app.conf", path="/etc/app.conf", content="new\n", mode="0644", owner="root",
             group="root", if_exists="replace", expected_sha256=sha("old\n")),
        step(4, "sudoers.install", "/etc/sudoers.d/pe", path="/etc/sudoers.d/pe", content="pe ALL=(root) NOPASSWD: /bin/true\n",
             run_user="svc", expect_absent=True),
        step(5, "service.install", UNIT, flavour="systemd", name="casa-dashboard", path=UNIT, content="[Unit]\n", expect_absent=True),
        step(6, "boot_hook.install", HOOKS, dest_dir=HOOKS, hooks={"post-start.sh": BODY, "shutdown.sh": BODY},
             expect_absent=["shutdown.sh"], expected_sha256={"post-start.sh": sha(mine)}),
        step(7, "service.enable", "casa-dashboard", flavour="systemd", name="casa-dashboard", start=True),
    ]
    return make_plan(steps), host, state


def test_the_whole_pipeline_applies_and_undoes_cleanly(tmp_path):
    plan, host, state = pipeline()
    before = tree(host)
    assert run_apply(plan, host, tmp_path).status == "done" and state == {"enabled": True, "active": True}
    result = run_undo(plan, host, tmp_path)
    assert result.status == "done", (result.step, result.reason)
    assert tree(host) == before and state == {"enabled": False, "active": False}


@pytest.mark.parametrize("after", [False, True])
def test_a_crash_at_any_operation_of_the_whole_pipeline_resumes_and_still_undoes(tmp_path, after):
    plan, clean_host, _ = pipeline()
    before = tree(clean_host)
    counter = FaultyHost(clean_host)
    assert apply(plan, host=counter, journal_root=tmp_path / "clean", replan=lambda: plan,
                 evidence_dir="/journal/evidence").status == "done"
    expected = tree(clean_host)
    for crash_at in range(1, counter.ops + 1):
        _, host, state = pipeline()
        root = f"p{crash_at}"
        with pytest.raises(Crash):
            apply(plan, host=FaultyHost(host, crash_at=crash_at, after=after), journal_root=tmp_path / root,
                  replan=lambda: plan, evidence_dir="/journal/evidence")
        resumed = run_apply(plan, host, tmp_path, root)
        assert resumed.status == "done", (crash_at, after, resumed.step, resumed.reason)
        assert tree(host) == expected, (crash_at, after)
        undone = run_undo(plan, host, tmp_path, root)
        assert undone.status == "done", (crash_at, after, undone.step, undone.reason)
        leftover = {k for k in tree(host) if k not in before}
        # The only thing allowed to remain is a directory whose creation was never proven to be setup's.
        assert leftover <= {"/etc/pe"}, (crash_at, after, leftover)
        assert {k: v for k, v in tree(host).items() if k not in leftover} == before, (crash_at, after)


def test_a_venv_directory_that_already_existed_is_never_deleted_by_undo(tmp_path):
    from test_setup_handlers_env import PY, VENV, env_plan, healthy_checks, host_with_checkout, venv_cmd
    host = host_with_checkout()
    host.add_dir(VENV)                                  # exists, empty, not ours
    host.add_file(f"{VENV}/operators-notes", b"mine")
    healthy_checks(host)
    state = {"made": False}
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    host.on(lambda a: a[:2] == [PY, "-c"], lambda h, a: RunResult(0 if state["made"] else 1, ""))
    host.on(lambda a: a[:3] == [PY, "-m", "pip"], lambda h, a: (state.update(made=True), RunResult(0, "pip"))[1])
    plan = env_plan()
    assert run_apply(plan, host, tmp_path).status == "done"
    assert run_undo(plan, host, tmp_path).status == "done"
    assert f"{VENV}/operators-notes" in host.nodes


def test_things_added_inside_a_created_venv_stop_the_undo(tmp_path):
    from test_setup_handlers_env import PY, VENV, env_plan, healthy_checks, host_with_checkout, venv_cmd
    host = host_with_checkout()
    healthy_checks(host)
    state = {"made": False}
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    host.on(lambda a: a[:2] == [PY, "-c"], lambda h, a: RunResult(0 if state["made"] else 1, ""))
    host.on(lambda a: a[:3] == [PY, "-m", "pip"], lambda h, a: (state.update(made=True), RunResult(0, "pip"))[1])
    plan = env_plan()
    run_apply(plan, host, tmp_path)
    host.add_file(f"{VENV}/my-data", b"precious")
    result = run_undo(plan, host, tmp_path)
    assert result.status == "stopped" and "my-data" in result.reason and f"{VENV}/my-data" in host.nodes


def test_a_write_that_failed_later_is_still_undone_after_a_satisfied_retry(tmp_path):
    host = sudoers_with_late_failure()
    from test_setup_handlers_privilege import sudoers_plan, SUDOERS
    plan = sudoers_plan()
    assert run_apply(plan, host, tmp_path).status == "stopped"       # committed, then `visudo -c` failed
    assert SUDOERS in host.nodes
    host.visudo_ok["ok"] = True
    assert run_apply(plan, host, tmp_path).status == "done"          # retry: satisfied, nothing written
    assert run_undo(plan, host, tmp_path).status == "done"
    assert SUDOERS not in host.nodes


def sudoers_with_late_failure():
    from test_setup_handlers_privilege import sudoers_host
    host = sudoers_host()
    host.visudo_ok = {"ok": False}

    def visudo(h, argv):
        if "-f" in argv:
            return RunResult(0, "")
        return RunResult(0 if h.visudo_ok["ok"] else 1, "", "" if h.visudo_ok["ok"] else "whole-system check failed")
    host.on(lambda a: a[:2] == ["visudo", "-c"], visudo)
    return host


def test_a_service_started_by_someone_else_after_a_failed_start_is_not_stopped(tmp_path):
    host, state = systemd_host(), {"enabled": True, "active": False}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)
    host.on(lambda a: a[:3] == ["systemctl", "start", "casa-dashboard"], RunResult(1, "", "Job failed"))
    from test_setup_handlers_services import enable_plan
    plan = enable_plan(start=True)
    assert run_apply(plan, host, tmp_path).status == "stopped"
    state["active"] = True                                            # the operator starts it by hand
    host.commands.clear()
    assert run_undo(plan, host, tmp_path).status == "done"
    assert not [c for c in host.commands if c[1] in ("stop", "disable")] and state["active"]
