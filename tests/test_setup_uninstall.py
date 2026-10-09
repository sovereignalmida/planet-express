"""Uninstall (and repair): the removal steps, the uninstall plan, and undoing an uninstall."""
from functools import partial

import pytest

from fake_host import Crash, FaultyHost
from planet_express.setup.apply import apply as _apply
from planet_express.setup.discover import discover
from planet_express.setup.plan import plan
from planet_express.setup.steps import CATALOGUE
from planet_express.setup.undo import undo as _undo
from test_setup_apply import fresh_host, make_plan, sha, step
from test_setup_discover import mos, ubuntu
from test_setup_handlers_services import BODY, HOOKS, UNIT, hook_host, systemctl, systemd_host
from test_setup_plan import LIVE_FILES, answers, mos_answers

apply = partial(_apply, evidence_dir="/journal/evidence")
undo = partial(_undo, evidence_dir="/journal/evidence")


def run_apply(p, host, tmp_path, name="j"):
    return apply(p, host=host, journal_root=tmp_path / name, replan=lambda: p)


def run_undo(p, host, tmp_path, name="j"):
    return undo(p, host=host, journal_root=tmp_path / name)


def tree(host):
    return {k: v for k, v in host.tree().items() if not k.startswith("/journal/")}


# -- file.remove -----------------------------------------------------------------------------------------------
def remove_plan(path="/etc/x.conf", content="data\n", **over):
    return make_plan([step(1, "file.remove", path, path=path, expected_sha256=sha(content), **over)])


def test_a_file_that_is_still_what_the_plan_saw_is_removed_and_comes_back_on_undo(tmp_path):
    host = fresh_host()
    host.add_file("/etc/x.conf", b"data\n", mode=0o640, uid=1000, gid=1000)
    p, before = remove_plan(), tree(host)
    assert run_apply(p, host, tmp_path).status == "done" and "/etc/x.conf" not in host.nodes
    assert run_undo(p, host, tmp_path).status == "done" and tree(host) == before


def test_a_file_changed_since_the_plan_is_not_removed(tmp_path):
    host = fresh_host()
    host.add_file("/etc/x.conf", b"someone edited this\n")
    result = run_apply(remove_plan(), host, tmp_path)
    assert result.status == "stopped" and "changed since the plan" in result.reason and "/etc/x.conf" in host.nodes


def test_a_file_already_gone_is_satisfied_and_not_restored_on_undo(tmp_path):
    host, p = fresh_host(), remove_plan()
    assert run_apply(p, host, tmp_path).status == "done"
    assert run_undo(p, host, tmp_path).status == "done" and "/etc/x.conf" not in host.nodes


def test_undo_will_not_put_a_file_back_over_something_new(tmp_path):
    host = fresh_host()
    host.add_file("/etc/x.conf", b"data\n")
    p = remove_plan()
    run_apply(p, host, tmp_path)
    host.add_file("/etc/x.conf", b"a new file\n")
    result = run_undo(p, host, tmp_path)
    assert result.status == "stopped" and "something else is at" in result.reason
    assert host.nodes["/etc/x.conf"].data == b"a new file\n"


def test_a_removed_unit_makes_systemd_reload_both_ways(tmp_path):
    host = systemd_host()
    host.add_file(UNIT, b"[Unit]\n")
    p = remove_plan(UNIT, "[Unit]\n", reload_systemd=True)
    run_apply(p, host, tmp_path)
    assert ["systemctl", "daemon-reload"] in host.commands
    host.commands.clear()
    run_undo(p, host, tmp_path)
    assert ["systemctl", "daemon-reload"] in host.commands and UNIT in host.nodes


# -- service.disable -------------------------------------------------------------------------------------------
def disable_plan(flavour="systemd"):
    return make_plan([step(1, "service.disable", "casa-dashboard", flavour=flavour, name="casa-dashboard")])


def systemd_with_state(enabled, active):
    host, state = systemd_host(), {"enabled": enabled, "active": active}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)

    def flip(h, argv):
        state[{"stop": "active", "disable": "enabled"}[argv[1]]] = False
        return __import__("planet_express.setup.host", fromlist=["RunResult"]).RunResult(0, "")

    def turn_on(h, argv):
        state[{"start": "active", "enable": "enabled"}[argv[1]]] = True
        return __import__("planet_express.setup.host", fromlist=["RunResult"]).RunResult(0, "")
    host.on(lambda a: a[:2] == ["systemctl", "daemon-reload"], __import__("planet_express.setup.host", fromlist=["RunResult"]).RunResult(0, ""))
    host.on(lambda a: a[:2] in (["systemctl", "stop"], ["systemctl", "disable"]), flip)
    host.on(lambda a: a[:2] in (["systemctl", "start"], ["systemctl", "enable"]), turn_on)
    return host, state


def test_a_running_enabled_service_is_stopped_and_disabled_then_restored_by_undo(tmp_path):
    host, state = systemd_with_state(True, True)
    p = disable_plan()
    assert run_apply(p, host, tmp_path).status == "done" and state == {"enabled": False, "active": False}
    assert run_undo(p, host, tmp_path).status == "done" and state == {"enabled": True, "active": True}


def test_a_service_that_was_already_off_is_left_off_by_undo(tmp_path):
    host, state = systemd_with_state(False, False)
    p = disable_plan()
    assert run_apply(p, host, tmp_path).status == "done"
    assert run_undo(p, host, tmp_path).status == "done" and state == {"enabled": False, "active": False}


def test_only_what_the_step_turned_off_is_turned_back_on(tmp_path):
    host, state = systemd_with_state(True, False)          # enabled but not running
    p = disable_plan()
    run_apply(p, host, tmp_path)
    run_undo(p, host, tmp_path)
    assert state == {"enabled": True, "active": False}
    assert ["systemctl", "start", "casa-dashboard"] not in host.commands


# -- boot_hook.remove ------------------------------------------------------------------------------------------
def install_hooks(host, tmp_path, mine=None, name="install"):
    from test_setup_handlers_services import hook_plan
    names = {"post-start.sh": BODY, "shutdown.sh": BODY}
    expected = {"post-start.sh": sha(mine)} if mine else {}
    p = hook_plan(hooks=names, expect_absent=[n for n in names if n not in expected], expected_sha256=expected)
    assert run_apply(p, host, tmp_path, name).status == "done"
    return {n: sha(host.nodes[f"{HOOKS}/{n}"].data.decode()) for n in names}


def hook_remove_plan(hashes):
    return make_plan([step(1, "boot_hook.remove", HOOKS, dest_dir=HOOKS, hooks=hashes)])


def test_the_block_is_taken_out_and_the_operators_own_commands_stay(tmp_path):
    mine = "#!/bin/sh\nmodprobe nct6775\n"
    host = hook_host(**{"post-start.sh": mine})
    hashes = install_hooks(host, tmp_path, mine)
    before = tree(host)
    p = hook_remove_plan(hashes)
    assert run_apply(p, host, tmp_path).status == "done"
    assert host.nodes[f"{HOOKS}/post-start.sh"].data.decode() == mine            # only the block went
    assert f"{HOOKS}/shutdown.sh" not in host.nodes                              # held only our block: removed
    assert run_undo(p, host, tmp_path).status == "done" and tree(host) == before


def test_a_hook_edited_since_the_plan_is_not_touched(tmp_path):
    host = hook_host()
    hashes = install_hooks(host, tmp_path)
    host.nodes[f"{HOOKS}/post-start.sh"].data += b"echo added later\n"
    result = run_apply(hook_remove_plan(hashes), host, tmp_path)
    assert result.status == "stopped" and "changed since the plan" in result.reason
    assert b"added later" in host.nodes[f"{HOOKS}/post-start.sh"].data


def test_hooks_without_the_block_are_satisfied(tmp_path):
    host = hook_host(**{"post-start.sh": "#!/bin/sh\necho hi\n"})
    hashes = {"post-start.sh": sha("#!/bin/sh\necho hi\n")}
    assert run_apply(hook_remove_plan(hashes), host, tmp_path).status == "done"
    assert host.nodes[f"{HOOKS}/post-start.sh"].data == b"#!/bin/sh\necho hi\n"


@pytest.mark.parametrize("after", [False, True])
def test_a_crash_anywhere_in_a_hook_removal_resumes_and_still_undoes(tmp_path, after):
    mine = "#!/bin/sh\nmodprobe nct6775\n"

    def installed_host(tag):
        host = hook_host(**{"post-start.sh": mine})
        return host, install_hooks(host, tmp_path, mine, f"inst-{tag}")
    clean, hashes = installed_host("clean")
    installed = tree(clean)
    p = hook_remove_plan(hashes)
    counter = FaultyHost(clean)
    assert run_apply(p, counter, tmp_path, "clean").status == "done"
    for crash_at in range(1, counter.ops + 1):
        host, _ = installed_host(f"{crash_at}{int(after)}")
        root = f"r{crash_at}{int(after)}"
        with pytest.raises(Crash):
            run_apply(p, FaultyHost(host, crash_at=crash_at, after=after), tmp_path, root)
        assert run_apply(p, host, tmp_path, root).status == "done", (crash_at, after)
        assert host.nodes[f"{HOOKS}/post-start.sh"].data.decode() == mine, (crash_at, after)
        assert run_undo(p, host, tmp_path, root).status == "done", (crash_at, after)
        assert tree(host) == installed, (crash_at, after)


# -- the uninstall plan ----------------------------------------------------------------------------------------
def uninstall_answers(**over):
    base = {"story": "uninstall", "install_dir": "/home/pe/apps/pe", "telegram": None, "llm": None, "operator": None}
    return answers(**{**base, **over})


def test_a_systemd_uninstall_removes_the_integration_and_keeps_what_the_person_wrote():
    report = discover(ubuntu(files=LIVE_FILES), repo_root="/home/pe/apps/pe")
    p = plan(report, uninstall_answers())
    assert p.applicable and p.story == "uninstall" and p.steps[0].kind == "state.snapshot"
    targets = [(s.kind, s.target) for s in p.steps]
    assert ("file.remove", "/etc/sudoers.d/planetexpress") in targets
    assert ("service.disable", "casa-dashboard") in targets and ("file.remove", "/etc/systemd/system/casa-planetexpress.service") in targets
    removed = {s.target for s in p.steps if s.kind == "file.remove"}
    assert not any("env" in t or "config" in t for t in removed)                  # secrets and config stay
    assert all(s.params.get("expected_sha256") for s in p.steps if s.kind == "file.remove")
    assert any("configuration" in t and "kept" in t for t in p.will_not_touch)
    assert all(s.risk != "R0" or s.kind == "verify.smoke" for s in p.steps)


def test_nothing_installed_means_nothing_to_uninstall():
    p = plan(discover(ubuntu(), repo_root="/opt/pe"), uninstall_answers(install_dir="/opt/pe"))
    assert not p.applicable and "not installed" in p.blocked[0]


def test_a_mos_uninstall_removes_the_hook_block_first_and_then_the_init_scripts():
    files = {"/boot/optional/scripts/post-start.sh": "#!/bin/sh\n# BEGIN planetexpress (x)\nfoo\n# END planetexpress\n",
             "/boot/optional/scripts/shutdown.sh": "#!/bin/sh\n# BEGIN planetexpress (x)\nbar\n# END planetexpress\n",
             "/etc/init.d/casa-planetexpress": "#!/bin/sh\n", "/etc/init.d/casa-dashboard": "#!/bin/sh\n",
             "/etc/default/casa-planetexpress": "X=1\n", "/mnt/data/pe/config.yaml": "{}\n"}
    report = discover(mos(files=files), repo_root="/mnt/data/pe/planet-express")
    p = plan(report, uninstall_answers(install_dir="/mnt/data/pe/planet-express", run_as="root", accept_root_service=True,
                                       stacks_root="/mnt/data/stacks"))
    assert p.applicable, p.blocked
    kinds = [s.kind for s in p.steps]
    assert kinds.index("boot_hook.remove") < kinds.index("service.disable") < kinds.index("file.remove")
    assert "/etc/init.d/casa-dashboard" in {s.target for s in p.steps}


def test_every_new_step_kind_is_in_the_catalogue_with_a_root_requirement():
    for kind in ("file.remove", "service.disable", "boot_hook.remove"):
        assert kind in CATALOGUE and CATALOGUE[kind].needs_root and CATALOGUE[kind].reversible
    assert CATALOGUE["boot_hook.remove"].risk == "R3"


def test_an_uninstall_plan_applies_and_undoes_on_a_host_that_looks_like_the_live_one(tmp_path):
    host, state = systemd_with_state(True, True)
    host.add_dir("/etc/sudoers.d", mode=0o750)
    host.add_file("/etc/sudoers.d/planetexpress", b"pe ALL=(root) NOPASSWD: /bin/true\n", mode=0o440)
    p = make_plan([
        step(1, "service.disable", "casa-dashboard", flavour="systemd", name="casa-dashboard"),
        step(2, "file.remove", UNIT, path=UNIT, expected_sha256=sha("[Unit]\n"), reload_systemd=True),
        step(3, "file.remove", "/etc/sudoers.d/planetexpress", path="/etc/sudoers.d/planetexpress",
             expected_sha256=sha("pe ALL=(root) NOPASSWD: /bin/true\n"))])
    before = tree(host)
    assert run_apply(p, host, tmp_path).status == "done" and UNIT not in host.nodes and state["enabled"] is False
    assert run_undo(p, host, tmp_path).status == "done" and tree(host) == before and state["enabled"] is True


def test_uninstall_is_bound_to_the_install_that_was_found_not_to_what_the_page_says():
    report = discover(ubuntu(files=LIVE_FILES), repo_root="/home/pe/apps/pe")
    other = plan(report, uninstall_answers(install_dir="/tmp/attacker-controlled"))
    assert not other.applicable and "not /tmp/attacker-controlled" in other.blocked[0]
    as_root = plan(report, uninstall_answers(run_as="root"))
    assert not as_root.applicable and "does not run as root" in as_root.blocked[0]
    assert plan(report, uninstall_answers()).applicable


def test_a_casa_stacks_unit_setup_did_not_write_is_left_alone_and_one_it_did_is_removed():
    from scripts.render_template import render
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    values = dict(INSTALL_DIR="/home/pe/apps/pe", RUN_USER="pe", RUN_GROUP="pe", CONFIG_FILE="/home/pe/apps/pe/config.yaml",
                  DASHBOARD_PORT="8420")
    ours = render((root / "systemd/casa-stacks.service.template").read_text(), **values)
    for content, removed in ((ours, True), ("[Unit]\nDescription=my own stacks unit\n", False)):
        files = {**LIVE_FILES, "/etc/systemd/system/casa-stacks.service": content}
        p = plan(discover(ubuntu(files=files), repo_root="/home/pe/apps/pe"), uninstall_answers(), repo_root=str(root))
        targets = {s.target for s in p.steps}
        assert ("/etc/systemd/system/casa-stacks.service" in targets) is removed
        if not removed:
            assert any("casa-stacks.service" in t and "kept" in t for t in p.will_not_touch)


def test_an_older_revision_of_our_own_casa_stacks_unit_is_still_recognised():
    old_revision = "[Unit]\nDescription=Planet Express stacks\n[Service]\nExecStart=/usr/bin/python3 /home/pe/apps/pe/casa_boot.py\n"
    files = {**LIVE_FILES, "/etc/systemd/system/casa-stacks.service": old_revision}
    p = plan(discover(ubuntu(files=files), repo_root="/home/pe/apps/pe"), uninstall_answers())
    assert "/etc/systemd/system/casa-stacks.service" in {s.target for s in p.steps}
