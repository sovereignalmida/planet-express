"""service.install, boot_hook.install and service.enable (A2c)."""
from functools import partial

import pytest

from fake_host import Crash, FaultyHost
from planet_express.setup.apply import apply as _apply
from planet_express.setup.handlers import merge_block
from planet_express.setup.host import RunResult
from test_setup_apply import fresh_host, make_plan, sha, step

apply = partial(_apply, evidence_dir="/journal/evidence")
UNIT = "/etc/systemd/system/casa-dashboard.service"
INIT = "/etc/init.d/casa-dashboard"
HOOKS = "/boot/optional/scripts"
BODY = "pe_post_start() {\n    echo hi\n    return 0\n}\npe_post_start\n"


def run(plan, host, tmp_path, name="j"):
    return apply(plan, host=host, journal_root=tmp_path / name, replan=lambda: plan)


def unit_plan(content="[Unit]\n", **over):
    params = dict(flavour="systemd", name="casa-dashboard", path=UNIT, content=content, expect_absent=True)
    params.update(over)
    return make_plan([step(1, "service.install", UNIT, **params)])


def systemd_host(load="loaded", need_reload=None):
    """`need_reload` is a mutable {'yes': bool}: daemon-reload clears it, a unit written sets it."""
    host = fresh_host()
    host.add_dir("/etc/systemd/system")
    host.reload_needed = need_reload if need_reload is not None else {"yes": False}

    def show(h, argv):
        return RunResult(0, f"LoadState={load}\nNeedDaemonReload={'yes' if h.reload_needed['yes'] else 'no'}\n")

    def reload(h, argv):
        h.reload_needed["yes"] = False
        return RunResult(0, "")
    host.on(lambda a: a[:2] == ["systemctl", "show"], show)
    host.on(lambda a: a[:2] == ["systemctl", "daemon-reload"], reload)
    return host


# -- service.install ---------------------------------------------------------------------------------------
def test_a_unit_is_written_0644_and_systemd_is_told_to_reload(tmp_path):
    host, plan = systemd_host(), unit_plan()
    assert run(plan, host, tmp_path).status == "done"
    assert host.nodes[UNIT].data == b"[Unit]\n" and host.nodes[UNIT].mode == 0o644
    assert ["systemctl", "daemon-reload"] in host.commands


def test_an_existing_unit_with_local_edits_is_kept_and_nothing_reloads(tmp_path):
    host = systemd_host()
    host.add_file(UNIT, b"[Unit]\n# local edit\n")
    plan = unit_plan(expect_absent=False, expected_sha256=sha("[Unit]\n# local edit\n"))
    assert run(plan, host, tmp_path).status == "done"
    assert host.nodes[UNIT].data == b"[Unit]\n# local edit\n"
    assert ["systemctl", "daemon-reload"] not in host.commands


def test_a_failed_reload_is_a_stop_not_a_success(tmp_path):
    host = systemd_host()
    host.on(lambda a: a[:2] == ["systemctl", "daemon-reload"], RunResult(1, "", "bus unavailable"))
    result = run(unit_plan(), host, tmp_path)
    assert result.status == "stopped" and "reload" in result.reason


def init_plan(**over):
    params = dict(flavour="sysvinit", name="casa-dashboard", path=INIT, content="#!/bin/sh\n", if_exists="replace",
                  expected_sha256=sha("old\n"), defaults_path="/etc/default/casa-dashboard",
                  defaults_content="PYTHON=/mnt/data/pe/venv/bin/python\n")
    params.update(over)
    return make_plan([step(1, "service.install", INIT, **params)])


def init_host(defaults=None):
    host = fresh_host()
    host.add_dir("/etc/init.d")
    host.add_dir("/etc/default")
    host.add_file(INIT, b"old\n", mode=0o755)
    if defaults is not None:
        host.add_file("/etc/default/casa-dashboard", defaults)
    return host


def test_an_init_script_is_executable_and_its_defaults_file_is_written_with_it(tmp_path):
    host = init_host()
    assert run(init_plan(), host, tmp_path).status == "done"
    assert host.nodes[INIT].mode == 0o755 and host.nodes[INIT].data == b"#!/bin/sh\n"
    assert host.nodes["/etc/default/casa-dashboard"].data == b"PYTHON=/mnt/data/pe/venv/bin/python\n"
    assert not any(c[:1] == ["systemctl"] for c in host.commands)


def test_a_defaults_file_changed_since_review_is_not_overwritten(tmp_path):
    host = init_host(defaults=b"mine\n")
    result = run(init_plan(defaults_if_exists="replace", defaults_expected_sha256=sha("what the plan saw\n")), host, tmp_path)
    assert result.status == "stopped" and "changed since the plan" in result.reason
    assert host.nodes[INIT].data == b"old\n" and host.nodes["/etc/default/casa-dashboard"].data == b"mine\n"


def test_a_stale_defaults_copy_that_matches_the_plan_is_replaced_and_backed_up(tmp_path):
    host = init_host(defaults=b"stale\n")
    plan = init_plan(defaults_if_exists="replace", defaults_expected_sha256=sha("stale\n"))
    assert run(plan, host, tmp_path).status == "done"
    assert host.nodes["/etc/default/casa-dashboard"].data == b"PYTHON=/mnt/data/pe/venv/bin/python\n"
    assert any(p.endswith("s01.defaults.bak") for p in host.nodes)


def test_without_a_replace_expectation_an_existing_defaults_file_is_left_alone(tmp_path):
    host = init_host(defaults=b"mine\n")
    assert run(init_plan(), host, tmp_path).status == "done"
    assert host.nodes["/etc/default/casa-dashboard"].data == b"mine\n" and host.nodes[INIT].data == b"#!/bin/sh\n"


def test_an_init_script_changed_since_review_is_not_replaced(tmp_path):
    host = init_host()
    host.add_file(INIT, b"someone edited this\n", mode=0o755)
    result = run(init_plan(), host, tmp_path)
    assert result.status == "stopped" and "changed since the plan" in result.reason


# -- boot_hook.install -------------------------------------------------------------------------------------
def hook_plan(**over):
    params = dict(dest_dir=HOOKS, hooks={"post-start.sh": BODY}, expect_absent=["post-start.sh"])
    params.update(over)
    return make_plan([step(1, "boot_hook.install", HOOKS, **params)])


def hook_host(**files):
    host = fresh_host()
    host.add_dir(HOOKS)
    for name, data in files.items():
        host.add_file(f"{HOOKS}/{name}", data.encode(), mode=0o755)
    return host


def test_a_missing_hook_file_becomes_a_small_script_holding_the_block(tmp_path):
    host = hook_host()
    assert run(hook_plan(), host, tmp_path).status == "done"
    text = host.nodes[f"{HOOKS}/post-start.sh"].data.decode()
    assert text.startswith("#!/bin/sh\n") and "# BEGIN planetexpress" in text and text.rstrip().endswith("# END planetexpress")


def test_the_operators_own_commands_survive_the_merge_and_a_rerun_changes_nothing(tmp_path):
    mine = "#!/bin/sh\nmodprobe nct6775\nmount /dev/sdx /mnt/x\n"
    host = hook_host(**{"post-start.sh": mine})
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha(mine)})
    assert run(plan, host, tmp_path).status == "done"
    merged = host.nodes[f"{HOOKS}/post-start.sh"].data.decode()
    assert merged.startswith(mine) and "pe_post_start" in merged
    before = host.tree()
    # A rerun against the merged file is satisfied and writes nothing (the plan's expectation is the old hash,
    # but the file already has the wanted content, which is accepted).
    assert run(plan, host, tmp_path, "j2").status == "done" and host.tree() == before


def test_an_existing_block_is_updated_in_place_not_duplicated():
    old = merge_block("#!/bin/sh\necho before\n", "planetexpress", "old body\n") + "echo after\n"
    new = merge_block(old, "planetexpress", "new body\n")
    assert new.count("BEGIN planetexpress") == 1 and "new body" in new and "old body" not in new
    assert new.startswith("#!/bin/sh\necho before\n") and new.endswith("echo after\n")


def test_half_a_block_is_refused_rather_than_guessed_at(tmp_path):
    broken = "#!/bin/sh\n# BEGIN planetexpress (x)\nfoo\n"
    host = hook_host(**{"post-start.sh": broken})
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha(broken)})
    result = run(plan, host, tmp_path)
    assert result.status == "stopped" and "markers" in result.reason
    assert host.nodes[f"{HOOKS}/post-start.sh"].data.decode() == broken


def test_a_hook_that_changed_since_review_is_not_touched(tmp_path):
    host = hook_host(**{"post-start.sh": "#!/bin/sh\nsomething new\n"})
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha("#!/bin/sh\n")})
    result = run(plan, host, tmp_path)
    assert result.status == "stopped" and "changed since" in result.reason


def test_a_known_old_whole_file_copy_is_replaced_by_the_marked_version(tmp_path):
    legacy = "#!/bin/sh\n# the old whole-file hook\n"
    host = hook_host(**{"post-start.sh": legacy})
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha(legacy)},
                     legacy_sha256={"post-start.sh": [sha(legacy)]})
    assert run(plan, host, tmp_path).status == "done"
    assert "old whole-file hook" not in host.nodes[f"{HOOKS}/post-start.sh"].data.decode()


def test_two_hooks_each_keep_their_own_backup(tmp_path):
    a, b = "#!/bin/sh\nA\n", "#!/bin/sh\nB\n"
    host = hook_host(**{"post-start.sh": a, "shutdown.sh": b})
    plan = hook_plan(hooks={"post-start.sh": BODY, "shutdown.sh": BODY}, expect_absent=[],
                     expected_sha256={"post-start.sh": sha(a), "shutdown.sh": sha(b)})
    assert run(plan, host, tmp_path).status == "done"
    backups = sorted(p for p in host.nodes if p.startswith("/journal/evidence/") and p.endswith(".bak"))
    assert len(backups) == 2 and {host.nodes[x].data for x in backups} == {a.encode(), b.encode()}


@pytest.mark.parametrize("after", [False, True])
def test_a_crash_anywhere_in_a_hook_merge_resumes_to_the_clean_result(tmp_path, after):
    mine = "#!/bin/sh\nmine\n"
    plan = hook_plan(expect_absent=[], expected_sha256={"post-start.sh": sha(mine)})
    clean = hook_host(**{"post-start.sh": mine})
    counter = FaultyHost(clean)
    assert run(plan, counter, tmp_path, "clean").status == "done"
    for crash_at in range(1, counter.ops + 1):
        host, root = hook_host(**{"post-start.sh": mine}), f"c{crash_at}"
        with pytest.raises(Crash):
            run(plan, FaultyHost(host, crash_at=crash_at, after=after), tmp_path, root)
        assert run(plan, host, tmp_path, root).status == "done", crash_at
        assert host.nodes[f"{HOOKS}/post-start.sh"].data == clean.nodes[f"{HOOKS}/post-start.sh"].data
        assert not [p for p in host.nodes if ".pe-setup-" in p], crash_at


# -- service.enable ----------------------------------------------------------------------------------------
def enable_plan(flavour="systemd", start=False):
    return make_plan([step(1, "service.enable", "casa-dashboard", flavour=flavour, name="casa-dashboard", start=start)])


def systemctl(host, state):
    """A tiny systemd: `state` = {'enabled': bool, 'active': bool}."""
    def respond(h, argv):
        verb = argv[1]
        if verb == "enable":
            state["enabled"] = True
            return RunResult(0, "")
        if verb == "start":
            state["active"] = True
            return RunResult(0, "")
        if verb == "is-enabled":
            return RunResult(0 if state["enabled"] else 1, "enabled\n" if state["enabled"] else "disabled\n")
        if verb == "is-active":
            return RunResult(0 if state["active"] else 3, "active\n" if state["active"] else "inactive\n")
        return RunResult(1, "", "unexpected")
    host.on(lambda a: a[:1] == ["systemctl"], respond)


def test_enable_without_start_enables_and_does_not_start(tmp_path):
    host, state = systemd_host(), {"enabled": False, "active": False}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)
    assert run(enable_plan(), host, tmp_path).status == "done"
    assert state == {"enabled": True, "active": False}
    assert ["systemctl", "start", "casa-dashboard"] not in host.commands


def test_enable_and_start_does_both_and_a_rerun_does_nothing(tmp_path):
    host, state = systemd_host(), {"enabled": False, "active": False}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)
    plan = enable_plan(start=True)
    assert run(plan, host, tmp_path).status == "done" and state == {"enabled": True, "active": True}
    host.commands.clear()
    assert run(plan, host, tmp_path, "j2").status == "done"
    assert not [c for c in host.commands if c[1] in ("enable", "start")]


def test_a_service_that_does_not_come_up_is_a_stop(tmp_path):
    host, state = systemd_host(), {"enabled": False, "active": False}
    host.add_file(UNIT, b"[Unit]\n")
    systemctl(host, state)
    host.on(lambda a: a[:3] == ["systemctl", "start", "casa-dashboard"], RunResult(1, "", "Job failed"))
    result = run(enable_plan(start=True), host, tmp_path)
    assert result.status == "stopped" and "starting casa-dashboard failed" in result.reason


def test_enable_refuses_when_the_unit_is_not_installed(tmp_path):
    host = systemd_host()
    systemctl(host, {"enabled": False, "active": False})
    result = run(enable_plan(), host, tmp_path)
    assert result.status == "stopped" and "install step has to run first" in result.reason


def test_on_sysvinit_enable_is_only_a_start_and_boot_start_is_the_hooks_job(tmp_path):
    host = init_host()
    running = {"up": False}

    def initd(h, argv):
        if argv[1] == "start":
            running["up"] = True
            return RunResult(0, "")
        return RunResult(0 if running["up"] else 3, "")
    host.on(lambda a: a[:1] == [INIT], initd)
    assert run(enable_plan("sysvinit"), host, tmp_path, "a").status == "done" and not running["up"]
    assert run(enable_plan("sysvinit", start=True), host, tmp_path, "b").status == "done" and running["up"]


# -- review findings (A2c) -----------------------------------------------------------------------------------
def test_a_unit_on_disk_that_systemd_has_not_read_yet_is_reloaded_on_resume(tmp_path):
    host = systemd_host()
    host.add_file(UNIT, b"[Unit]\n")
    host.reload_needed["yes"] = True                                   # as after a crash between rename and reload
    plan = unit_plan(expect_absent=False, expected_sha256=sha("[Unit]\n"))
    assert run(plan, host, tmp_path).status == "done"
    assert ["systemctl", "daemon-reload"] in host.commands and host.reload_needed["yes"] is False


def test_a_unit_systemd_cannot_load_is_not_reported_as_installed(tmp_path):
    host = systemd_host(load="error")
    result = run(unit_plan(), host, tmp_path)
    assert result.status == "stopped" and "expected result" in result.reason


def test_a_matching_defaults_file_with_the_wrong_owner_or_mode_is_refused(tmp_path):
    host = init_host(defaults=b"PYTHON=/mnt/data/pe/venv/bin/python\n")
    host.nodes["/etc/default/casa-dashboard"].mode = 0o666
    result = run(init_plan(), host, tmp_path)
    assert result.status == "stopped" and "must be 0644 root:root" in result.reason


def test_a_crash_after_one_hook_and_before_the_next_resumes_instead_of_giving_up(tmp_path):
    a, b = "#!/bin/sh\nA\n", "#!/bin/sh\nB\n"
    plan = hook_plan(hooks={"post-start.sh": BODY, "shutdown.sh": BODY}, expect_absent=[],
                     expected_sha256={"post-start.sh": sha(a), "shutdown.sh": sha(b)})

    def build():
        return hook_host(**{"post-start.sh": a, "shutdown.sh": b})
    counter = FaultyHost(build())
    assert run(plan, counter, tmp_path, "clean").status == "done"
    for crash_at in range(1, counter.ops + 1):
        for after in (False, True):
            host, root = build(), f"m{crash_at}{int(after)}"
            with pytest.raises(Crash):
                run(plan, FaultyHost(host, crash_at=crash_at, after=after), tmp_path, root)
            r = run(plan, host, tmp_path, root)
            assert r.status == "done", (crash_at, after, r.reason)
