"""The executor and the first three handlers, on a fake host: ordering, drift, compare-and-swap, resume,
and a crash injected at every mutating operation."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from fake_host import Crash, FakeHost, FaultyHost
from functools import partial

from planet_express.setup.apply import Busy, _Lock
from planet_express.setup.apply import apply as _apply
from planet_express.setup.host import RunResult
from planet_express.setup.journal import Journal
from planet_express.setup.plan import Plan, Step
from planet_express.setup.steps import CATALOGUE

# Backups go on the (fake) host's own filesystem, as they do in real use.
apply = partial(_apply, evidence_dir="/journal/evidence")

SECRET = "123456:TOKEN-SECRET-VALUE"
SVC = (1000, 1000)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def step(i, kind, target, **params):
    validated = CATALOGUE[kind].model(**params).model_dump(mode="json", exclude_none=True)
    return Step(f"s{i:02d}", kind, f"{kind} {target}", target, CATALOGUE[kind].risk, True, False, (), validated, {})


def make_plan(steps, secrets=None):
    return Plan("fresh", tuple(steps), (), (), (), secrets or {})


def standard_plan(**file_extra):
    return make_plan([
        step(1, "dir.ensure", "/etc/pe", path="/etc/pe", mode="0750", owner="svc", group="svc"),
        step(2, "file.write", "/etc/pe/config.yaml", path="/etc/pe/config.yaml", content="forbidden: []\n",
             mode="0640", owner="svc", group="svc", expect_absent=True, **file_extra),
        step(3, "file.write", "/etc/pe/pe.env", path="/etc/pe/pe.env", content="TG_BOT_TOKEN={{secret:tok}}\n",
             mode="0600", owner="svc", group="svc", expect_absent=True, has_secrets=True),
    ], {"tok": SECRET})


def fresh_host():
    host = FakeHost(trusted_uids={0, 1000}, users={"svc": SVC}, groups={"svc": 1000})
    host.add_dir("/etc")
    host.add_dir("/journal", mode=0o700)
    return host


def events(tmp_path, plan):
    return Journal(tmp_path / "j", plan.to_public()["plan_id"]).events()


def test_a_plan_is_applied_in_order_and_the_host_ends_up_exactly_as_planned(tmp_path):
    plan, host = standard_plan(), fresh_host()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "done"
    assert host.nodes["/etc/pe"].mode == 0o750 and host.nodes["/etc/pe"].uid == 1000
    assert host.nodes["/etc/pe/config.yaml"].data == b"forbidden: []\n" and host.nodes["/etc/pe/config.yaml"].mode == 0o640
    assert host.nodes["/etc/pe/pe.env"].data == f"TG_BOT_TOKEN={SECRET}\n".encode()
    assert host.nodes["/etc/pe/pe.env"].mode == 0o600
    assert not [p for p in host.nodes if ".pe-setup-" in p]              # no staged file left behind
    kinds = [(e["type"], e.get("step")) for e in events(tmp_path, plan) if e["type"] in ("step_started", "step_ok", "done")]
    assert kinds == [("step_started", "s01"), ("step_ok", "s01"), ("step_started", "s02"), ("step_ok", "s02"),
                     ("step_started", "s03"), ("step_ok", "s03"), ("done", None)]


def test_no_secret_value_is_written_to_the_journal_or_the_saved_plan(tmp_path):
    plan, host = standard_plan(), fresh_host()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    for path in Path(tmp_path / "j").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="replace"), path
    assert (tmp_path / "j" / plan.to_public()["plan_id"] / "plan.json").exists()


def test_running_the_same_plan_again_does_nothing(tmp_path):
    plan, host = standard_plan(), fresh_host()
    apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    before, mutations = host.tree(), host.mutations
    again = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert again.status == "done" and host.tree() == before and host.mutations == mutations


def test_a_fresh_journal_on_an_already_converged_host_finds_every_step_satisfied(tmp_path):
    plan, host = standard_plan(), fresh_host()
    apply(plan, host=host, journal_root=tmp_path / "first", replan=lambda: plan)
    before, mutations = host.tree(), host.mutations
    result = apply(plan, host=host, journal_root=tmp_path / "second", replan=lambda: plan)
    assert result.status == "done" and host.tree() == before
    ok = [e for e in Journal(tmp_path / "second", plan.to_public()["plan_id"]).events() if e["type"] == "step_ok"]
    assert len(ok) == 3 and all(e.get("satisfied") for e in ok)


def test_a_host_that_drifted_since_review_runs_nothing_and_says_what_changed(tmp_path):
    plan, host = standard_plan(), fresh_host()
    other = make_plan(list(plan.steps) + [step(4, "dir.ensure", "/etc/extra", path="/etc/extra")], {"tok": SECRET})
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: other)
    assert result.status == "refused" and "changed since the plan was reviewed" in result.reason
    assert "new step: dir.ensure /etc/extra" in result.reason
    assert host.mutations == 0 and not list((tmp_path / "j").glob("*/events.jsonl"))      # no step, no journal


def test_an_unapplicable_or_unsupported_plan_is_refused_before_anything_runs(tmp_path):
    host = fresh_host()
    blocked = Plan("fresh", (), (), (), ("Docker is not installed",), {})
    assert apply(blocked, host=host, journal_root=tmp_path / "j", replan=lambda: blocked).status == "refused"
    unsupported = make_plan([step(1, "dir.ensure", "/etc/pe", path="/etc/pe"),
                             step(2, "python.env", "/opt/venv", install_dir="/opt/pe", venv_dir="/opt/venv")])
    result = apply(unsupported, host=host, journal_root=tmp_path / "j", replan=lambda: unsupported)
    assert result.status == "refused" and "python.env" in result.reason
    assert host.mutations == 0                                           # not even the first, supported step ran


def test_a_second_apply_while_one_is_running_is_refused(tmp_path):
    plan, host = standard_plan(), fresh_host()
    with _Lock(tmp_path / "j"):
        result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "refused" and "another apply" in result.reason and host.mutations == 0


# -- compare-and-swap ----------------------------------------------------------------------------------------


def replace_plan(expected_content):
    return make_plan([step(1, "file.write", "/etc/app.conf", path="/etc/app.conf", content="new\n", mode="0644",
                           owner="root", group="root", if_exists="replace", expected_sha256=sha(expected_content))])


def test_a_replace_proceeds_only_if_the_file_still_has_the_hash_the_plan_saw_and_keeps_a_backup(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"old\n")
    plan = replace_plan("old\n")
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert host.nodes["/etc/app.conf"].data == b"new\n"
    backups = [p for p in host.nodes if p.endswith(".bak")]
    assert len(backups) == 1 and host.nodes[backups[0]].data == b"old\n" and host.nodes[backups[0]].mode == 0o600


def test_a_file_that_changed_after_review_is_never_overwritten_and_later_steps_do_not_run(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"someone edited this\n")
    plan = make_plan(list(replace_plan("old\n").steps) + [step(2, "dir.ensure", "/etc/later", path="/etc/later")])
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and result.step == "s01" and "changed since the plan was made" in result.reason
    assert host.nodes["/etc/app.conf"].data == b"someone edited this\n"
    assert "/etc/later" not in host.nodes                               # stop on first failure
    started = [e["step"] for e in events(tmp_path, plan) if e["type"] == "step_started"]
    assert started == ["s01"]


def test_keep_leaves_an_existing_file_alone_and_a_vanished_one_is_not_recreated(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"mine\n")
    kept = make_plan([step(1, "file.write", "/etc/app.conf", path="/etc/app.conf", content="ours\n",
                           if_exists="keep", expected_sha256=sha("mine\n"))])
    assert apply(kept, host=host, journal_root=tmp_path / "a", replan=lambda: kept).status == "done"
    assert host.nodes["/etc/app.conf"].data == b"mine\n"
    del host.nodes["/etc/app.conf"]
    gone = apply(kept, host=host, journal_root=tmp_path / "b", replan=lambda: kept)
    assert gone.status == "stopped" and "is gone now" in gone.reason and "/etc/app.conf" not in host.nodes


def test_a_file_created_since_review_is_not_overwritten_when_the_step_was_approved_to_create_it(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"appeared\n")
    plan = make_plan([step(1, "file.write", "/etc/app.conf", path="/etc/app.conf", content="new\n",
                           if_exists="replace", expect_absent=True)])
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "approved to create it" in result.reason
    assert host.nodes["/etc/app.conf"].data == b"appeared\n"


def test_a_symlink_at_the_target_is_refused_and_what_it_points_to_is_untouched(tmp_path):
    host = fresh_host()
    host.add_file("/etc/real", b"precious\n")
    host.add_symlink("/etc/app.conf", "/etc/real")
    plan = make_plan([step(1, "file.write", "/etc/app.conf", path="/etc/app.conf", content="x\n", expect_absent=True)])
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "symlink" in result.reason
    assert host.nodes["/etc/real"].data == b"precious\n" and host.nodes["/etc/app.conf"].kind == "symlink"


def test_a_secrets_file_with_the_right_content_but_loose_permissions_is_refused_not_trusted(tmp_path):
    host = fresh_host()
    host.add_dir("/etc/pe", uid=1000, gid=1000)
    host.add_file("/etc/pe/pe.env", f"TG_BOT_TOKEN={SECRET}\n".encode(), mode=0o644, uid=1000, gid=1000)
    plan = standard_plan()
    result = apply(make_plan(list(plan.steps)[2:], plan.secrets), host=host, journal_root=tmp_path / "j",
                   replan=lambda: make_plan(list(plan.steps)[2:], plan.secrets))
    assert result.status == "stopped" and "mode 0644" in result.reason and "plan says mode 0600" in result.reason


def test_a_missing_secret_value_refuses_rather_than_writing_the_placeholder(tmp_path):
    host = fresh_host()
    host.add_dir("/etc/pe", uid=1000, gid=1000)
    plan = make_plan(list(standard_plan().steps)[2:], {})
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "no value for secret" in result.reason
    assert "/etc/pe/pe.env" not in host.nodes


def test_an_ancestor_writable_by_others_stops_the_step_without_writing(tmp_path):
    host = fresh_host()
    host.add_dir("/etc/open", mode=0o777)
    plan = make_plan([step(1, "file.write", "/etc/open/x", path="/etc/open/x", content="x\n", expect_absent=True)])
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "writable by group or other" in result.reason
    assert "/etc/open/x" not in host.nodes


def test_dry_run_checks_everything_and_changes_nothing(tmp_path):
    host = fresh_host()
    plan = standard_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan, dry_run=True)
    assert result.status == "dry_run" and host.mutations == 0 and not (tmp_path / "j").exists()
    by_step = {c["step"]: c for c in result.checks}
    assert by_step["s01"]["check"] == "would run"
    assert by_step["s02"]["check"] == "refused now" and "an earlier step" in by_step["s02"]["detail"]


# -- crash safety ----------------------------------------------------------------------------------------------


def _clean_run(plan, tmp_path):
    host = fresh_host()
    counter = FaultyHost(host)                                           # counts mutating operations
    assert apply(plan, host=counter, journal_root=tmp_path / "clean", replan=lambda: plan).status == "done"
    return host.tree(), counter.ops


def _plans_for_the_sweep():
    config = make_plan([
        step(1, "dir.ensure", "/etc/pe", path="/etc/pe", mode="0750", owner="svc", group="svc"),
        step(2, "file.write", "/etc/pe/a", path="/etc/pe/a", content="a\n", mode="0640", owner="svc", group="svc",
             expect_absent=True),
        step(3, "file.write", "/etc/pe/s", path="/etc/pe/s", content="T={{secret:tok}}\n", mode="0600",
             owner="svc", group="svc", expect_absent=True, has_secrets=True)], {"tok": SECRET})
    replace = replace_plan("old\n")
    return {"create": config, "replace": replace}


@pytest.mark.parametrize("name", ["create", "replace"])
@pytest.mark.parametrize("after", [False, True])
def test_a_crash_at_any_operation_leaves_a_host_that_resumes_to_exactly_the_clean_result(tmp_path, name, after):
    plan = _plans_for_the_sweep()[name]

    def host_for():
        host = fresh_host()
        if name == "replace":
            host.add_file("/etc/app.conf", b"old\n")
        return host

    clean = host_for()
    counter = FaultyHost(clean)
    assert apply(plan, host=counter, journal_root=tmp_path / "clean", replan=lambda: plan).status == "done"
    expected_tree, total_ops = clean.tree(), counter.ops
    assert total_ops >= 3
    for crash_at in range(1, total_ops + 1):
        host = host_for()
        root = tmp_path / f"crash-{int(after)}-{crash_at}"
        with pytest.raises(Crash):
            apply(plan, host=FaultyHost(host, crash_at=crash_at, after=after), journal_root=root, replan=lambda: plan)
        # A torn file is never visible: whatever is at a final path is either absent, the old content, or whole.
        for path, node in host.nodes.items():
            if node.kind == "file" and not path.endswith(".bak") and ".pe-setup-" not in path:
                assert node.data in (b"old\n", b"new\n", b"a\n", f"T={SECRET}\n".encode()), (crash_at, path)
        resumed = apply(plan, host=host, journal_root=root, replan=lambda: plan)
        assert resumed.status == "done", (crash_at, after, resumed.reason)
        got = {p: v for p, v in host.tree().items() if ".bak" not in p and "/journal" not in p and "/evidence" not in p}
        want = {p: v for p, v in expected_tree.items() if ".bak" not in p and "/journal" not in p and "/evidence" not in p}
        assert got == want, (crash_at, after)
        assert not [p for p in host.nodes if ".pe-setup-" in p], (crash_at, after, "a staged file was left behind")
        for path in Path(root).rglob("*"):
            if path.is_file():
                assert SECRET not in path.read_text(errors="replace")


def test_an_interrupted_step_whose_host_state_cannot_be_classified_stops_and_asks(tmp_path):
    host = fresh_host()
    host.add_file("/etc/app.conf", b"neither old nor new\n")
    plan = replace_plan("old\n")
    journal = Journal(tmp_path / "j", plan.to_public()["plan_id"])
    journal.append("apply_started")
    journal.append("step_started", step="s01")                          # the last run died here
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "cannot classify" in result.reason
    assert host.nodes["/etc/app.conf"].data == b"neither old nor new\n"


# -- verify.smoke ----------------------------------------------------------------------------------------------


def smoke_plan():
    return make_plan([step(1, "verify.smoke", "/opt/pe", install_dir="/opt/pe", venv_dir="/opt/pe/venv",
                           run_user="svc", env={"CASA_CONFIG": "/etc/pe/config.yaml", "CASA_STATE_DIR": "/opt/pe/state"})])


def smoke_host(result):
    host = fresh_host()
    host.add_file("/opt/pe/venv/bin/python", b"", mode=0o755)
    host.command_results[("/opt/pe/venv/bin/python", "casa_leela.py", "--status")] = result
    return host


def test_the_smoke_check_runs_as_the_service_user_with_only_the_casa_environment(tmp_path):
    host = smoke_host(RunResult(0, json.dumps({"containers": [1, 2, 3]})))
    plan = smoke_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert host.run_as == ["svc"] and host.envs == [{"CASA_CONFIG": "/etc/pe/config.yaml", "CASA_STATE_DIR": "/opt/pe/state"}]
    assert any("saw 3 container" in e.get("line", "") for e in events(tmp_path, plan))


@pytest.mark.parametrize("result, expect", [
    (RunResult(1, "", "permission denied on the docker socket"), "permission denied"),
    (RunResult(0, "not json"), "did not return JSON"),
    (RunResult(0, json.dumps({"no": "containers"})), "no container list"),
])
def test_the_smoke_check_fails_loudly_on_a_bad_scan(tmp_path, result, expect):
    host = smoke_host(result)
    plan = smoke_plan()
    outcome = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert outcome.status == "stopped" and expect in outcome.reason


def test_the_smoke_check_refuses_when_the_environment_step_has_not_run(tmp_path):
    host = fresh_host()
    plan = smoke_plan()
    outcome = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert outcome.status == "stopped" and "environment step has to run first" in outcome.reason


def test_a_double_crash_crash_resume_crash_again_still_converges(tmp_path):
    plan = _plans_for_the_sweep()["create"]
    clean, total = fresh_host(), None
    counter = FaultyHost(clean)
    apply(plan, host=counter, journal_root=tmp_path / "clean", replan=lambda: plan)
    expected, total = clean.tree(), counter.ops
    for first in range(1, total + 1, 2):
        for second in range(1, total + 1, 3):
            host = fresh_host()
            root = tmp_path / f"d-{first}-{second}"
            for crash_at in (first, second):
                try:
                    apply(plan, host=FaultyHost(host, crash_at=crash_at), journal_root=root, replan=lambda: plan)
                except Crash:
                    pass
            assert apply(plan, host=host, journal_root=root, replan=lambda: plan).status == "done", (first, second)
            drop = lambda tree: {p: v for p, v in tree.items() if "/evidence" not in p and "/journal" not in p}
            assert drop(host.tree()) == drop(expected), (first, second)
            assert not [p for p in host.nodes if ".pe-setup-" in p], (first, second)


def test_a_crash_between_creating_the_staged_file_and_journaling_it_is_cleaned_up_by_its_name(tmp_path):
    """The window the identity record alone could not close: the temp file exists, nothing says it is ours."""
    plan = replace_plan("old\n")
    host = fresh_host()
    host.add_file("/etc/app.conf", b"old\n")
    # ops: 1 evidence dir, 2 backup, 3 stage_file -> die right after it, before the 'staged' record.
    with pytest.raises(Crash):
        apply(plan, host=FaultyHost(host, crash_at=3, after=True), journal_root=tmp_path / "j", replan=lambda: plan)
    assert [p for p in host.nodes if ".pe-setup-" in p], "the staged file should exist at the crash point"
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert not [p for p in host.nodes if ".pe-setup-" in p] and host.nodes["/etc/app.conf"].data == b"new\n"


def test_reconcile_never_removes_a_file_that_merely_looks_like_ours_but_was_not_journaled(tmp_path):
    plan = replace_plan("old\n")
    host = fresh_host()
    host.add_file("/etc/app.conf", b"old\n")
    stranger = "/etc/.pe-setup-feedfacecafe.tmp"
    host.add_file(stranger, b"not ours")
    with pytest.raises(Crash):
        apply(plan, host=FaultyHost(host, crash_at=3, after=True), journal_root=tmp_path / "j", replan=lambda: plan)
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert stranger in host.nodes and host.nodes[stranger].data == b"not ours"


def test_dry_run_reports_steps_this_build_cannot_apply_instead_of_refusing(tmp_path):
    plan = make_plan([step(1, "dir.ensure", "/etc/pe", path="/etc/pe"),
                      step(2, "python.env", "/opt/venv", install_dir="/opt/pe", venv_dir="/opt/venv")])
    result = apply(plan, host=fresh_host(), journal_root=tmp_path / "j", replan=lambda: plan, dry_run=True)
    assert result.status == "dry_run" and result.checks[1]["check"] == "not implemented"


# -- the real filesystem, end to end ---------------------------------------------------------------------------


def _real_names():
    import grp
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name


def test_the_executor_and_handlers_work_on_a_real_filesystem(tmp_path):
    from planet_express.setup.host import RealHost
    user, group = _real_names()
    base = tmp_path / "pe"
    tmp_path.chmod(0o755)
    plan = make_plan([
        step(1, "dir.ensure", str(base / "conf"), path=str(base / "conf"), mode="0750", owner=user, group=group),
        step(2, "file.write", str(base / "conf/app.yaml"), path=str(base / "conf/app.yaml"), content="a: 1\n",
             mode="0640", owner=user, group=group, expect_absent=True),
        step(3, "file.write", str(base / "conf/pe.env"), path=str(base / "conf/pe.env"),
             content="TOKEN={{secret:tok}}\n", mode="0600", owner=user, group=group, expect_absent=True,
             has_secrets=True),
    ], {"tok": SECRET})
    host = RealHost()
    (tmp_path / "ev").mkdir(mode=0o700)
    result = _apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan,
                    evidence_ids=(os.getuid(), os.getgid()), evidence_dir=str(tmp_path / "ev" / "plan"))
    assert result.status == "done", result.reason
    assert oct(os.stat(base / "conf").st_mode & 0o777) == "0o750"
    assert oct(os.stat(base / "conf/pe.env").st_mode & 0o777) == "0o600"
    assert (base / "conf/pe.env").read_text() == f"TOKEN={SECRET}\n"
    assert not [p for p in (base / "conf").iterdir() if p.name.startswith(".pe-setup-")]
    for path in (tmp_path / "j").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="replace")
    # Replace with a backup, then refuse to follow a symlink swapped in at the target.
    target = base / "conf/app.yaml"
    replace = make_plan([step(1, "file.write", str(target), path=str(target), content="a: 2\n", mode="0640",
                              owner=user, group=group, if_exists="replace", expected_sha256=sha("a: 1\n"))])
    assert _apply(replace, host=host, journal_root=tmp_path / "j2", replan=lambda: replace,
                  evidence_ids=(os.getuid(), os.getgid()), evidence_dir=str(tmp_path / "ev" / "plan2")).status == "done"
    assert target.read_text() == "a: 2\n" and (tmp_path / "ev/plan2/s01.bak").read_text() == "a: 1\n"
    secret_file = tmp_path / "outside.txt"
    secret_file.write_text("outside")
    target.unlink()
    target.symlink_to(secret_file)
    again = make_plan([step(1, "file.write", str(target), path=str(target), content="pwned\n", mode="0640",
                            owner=user, group=group, expect_absent=True)])
    result = _apply(again, host=host, journal_root=tmp_path / "j3", replan=lambda: again,
                    evidence_ids=(os.getuid(), os.getgid()), evidence_dir=str(tmp_path / "ev" / "plan3"))
    assert result.status == "stopped" and secret_file.read_text() == "outside"


# -- Codex review of A1 ----------------------------------------------------------------------------------------


def test_the_drift_check_happens_while_the_lock_is_held_so_two_applies_cannot_both_pass_it(tmp_path):
    """P1: two applies that start together must not both pass the check and then take turns."""
    plan, host = standard_plan(), fresh_host()
    inner_results = []

    def replan_that_races():
        # While the first apply is mid drift-check, a second apply starts. If the check ran before the lock,
        # this second one would get the lock; it must be refused as busy instead.
        inner_results.append(apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan))
        return plan

    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=replan_that_races).status == "done"
    assert [r.status for r in inner_results] == ["refused"] and "another apply" in inner_results[0].reason


def test_a_plan_that_goes_stale_while_waiting_for_the_lock_is_not_run(tmp_path):
    plan, host = standard_plan(), fresh_host()
    stale = make_plan(list(plan.steps) + [step(4, "dir.ensure", "/etc/changed-meanwhile", path="/etc/changed-meanwhile")],
                      {"tok": SECRET})
    calls = []

    def replan():
        calls.append(1)
        return stale if len(calls) >= 1 else plan                       # the host changed before we got the lock
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=replan)
    assert result.status == "refused" and host.mutations == 0


def _journal_root_attack(tmp_path, kind):
    """Build a hostile journal location and return the path a victim would be induced to use."""
    target = tmp_path / "attacker-chosen"
    target.mkdir()
    root = tmp_path / "j"
    if kind == "symlinked-root-to-an-open-directory":
        target.chmod(0o777)                          # somewhere other users can write: an attacker's drop point
        root.symlink_to(target)
    elif kind == "writable-root":
        root.mkdir()
        root.chmod(0o777)
    elif kind == "symlinked-plan-dir":
        root.mkdir(mode=0o700)
        plan_id = standard_plan().to_public()["plan_id"]
        (root / plan_id).symlink_to(target)
    elif kind == "symlinked-events":
        root.mkdir(mode=0o700)
        plan_id = standard_plan().to_public()["plan_id"]
        (root / plan_id).mkdir(mode=0o700)
        (target / "loot").write_text("original")
        (root / plan_id / "events.jsonl").symlink_to(target / "loot")
    return root, target


@pytest.mark.parametrize("kind", ["symlinked-root-to-an-open-directory", "writable-root", "symlinked-plan-dir",
                                  "symlinked-events"])
def test_a_hostile_journal_location_is_refused_and_nothing_is_written_through_it(tmp_path, kind):
    """P1: the journal is written as root, so a pre-planted symlink or open directory must not redirect it."""
    root, target = _journal_root_attack(tmp_path, kind)
    before = {p.name: p.read_text() for p in target.rglob("*") if p.is_file()}
    plan, host = standard_plan(), fresh_host()
    result = apply(plan, host=host, journal_root=root, replan=lambda: plan)
    assert result.status == "refused" and "not trustworthy" in result.reason, (kind, result)
    assert host.mutations == 0                                          # not one step ran
    assert {p.name: p.read_text() for p in target.rglob("*") if p.is_file()} == before


def test_the_journal_and_lock_are_private_when_everything_is_in_order(tmp_path):
    plan, host = standard_plan(), fresh_host()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    import stat as st
    modes = {p.name: st.S_IMODE(p.stat().st_mode) for p in (tmp_path / "j").rglob("*") if p.is_file()}
    assert modes["events.jsonl"] == 0o600 and modes["apply.lock"] == 0o600 and modes["plan.json"] == 0o600
    assert st.S_IMODE((tmp_path / "j").stat().st_mode) == 0o700


# -- metadata is part of "satisfied" ---------------------------------------------------------------------------


def identical_file_plan(**over):
    params = dict(path="/etc/pe.conf", content="same\n", mode="0640", owner="svc", group="svc", expect_absent=True)
    params.update(over)
    return make_plan([step(1, "file.write", "/etc/pe.conf", **params)])


@pytest.mark.parametrize("mode, uid, gid, complaint", [
    (0o600, 1000, 1000, "mode 0600"),            # right owner, mode too tight for the service group
    (0o640, 0, 0, "owned by 0:0"),               # right mode, wrong owner: the service cannot read it
    (0o666, 1000, 1000, "mode 0666"),            # looser than planned
])
def test_a_file_with_the_planned_bytes_but_the_wrong_owner_or_mode_is_not_called_satisfied(tmp_path, mode, uid, gid, complaint):
    """P2: a config planned as service-readable must not 'succeed' while left root:root 0600."""
    host = fresh_host()
    host.add_file("/etc/pe.conf", b"same\n", mode=mode, uid=uid, gid=gid)
    plan = identical_file_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and complaint in result.reason and "planned" in result.reason
    assert host.nodes["/etc/pe.conf"].mode == mode                      # reported, not silently changed


def test_a_file_with_the_planned_bytes_and_the_planned_metadata_is_satisfied(tmp_path):
    host = fresh_host()
    host.add_file("/etc/pe.conf", b"same\n", mode=0o640, uid=1000, gid=1000)
    plan = identical_file_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert host.mutations == 0


def test_a_kept_file_with_different_bytes_is_still_kept_whatever_its_metadata(tmp_path):
    """keep means we leave it; the metadata promise only applies to a file that is what we planned."""
    host = fresh_host()
    host.add_file("/etc/pe.conf", b"theirs\n", mode=0o600, uid=0, gid=0)
    plan = identical_file_plan(if_exists="keep", expect_absent=False, expected_sha256=sha("theirs\n"))
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert host.nodes["/etc/pe.conf"].data == b"theirs\n"
