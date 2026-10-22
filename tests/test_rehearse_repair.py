"""scripts/rehearse_repair.py (release proof item 7): argument and refusal handling, the synthetic project, the record.
Nothing here touches Docker; the real run is `scripts/run_rehearsal.sh --vm HOST`."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("rehearse_repair", ROOT / "scripts/rehearse_repair.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearse_repair"] = module
    spec.loader.exec_module(module)
    return module


def docker(ps_out="", compose_rc=0, ps_rc=0):
    calls = []

    def run(argv, timeout=120):
        calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return ps_rc, ps_out
        if argv[:3] == ["docker", "compose", "version"]:
            return compose_rc, "v2"
        return 127, ""
    run.calls = calls
    return run


GOOD_ENV = {"PE_REHEARSAL_OK": "1"}


def args_of(mod, *extra):
    return mod.parse_args(["--i-know-this-is-disposable", *extra])


# ── refusals ────────────────────────────────────────────────────────────────

def test_a_clean_disposable_root_host_is_accepted(mod):
    assert mod.refusals(args_of(mod), GOOD_ENV, 0, docker()) == []


def test_it_refuses_without_the_environment_switch(mod):
    why = mod.refusals(args_of(mod), {}, 0, docker())
    assert len(why) == 1 and "PE_REHEARSAL_OK=1" in why[0]
    assert mod.refusals(args_of(mod), {"PE_REHEARSAL_OK": "yes"}, 0, docker())


def test_it_refuses_without_the_disposable_flag(mod):
    why = mod.refusals(mod.parse_args([]), GOOD_ENV, 0, docker())
    assert any("--i-know-this-is-disposable" in w for w in why)


def test_it_refuses_when_not_root(mod):
    assert any("root" in w for w in mod.refusals(args_of(mod), GOOD_ENV, 1000, docker()))


def test_it_refuses_a_host_with_containers_that_are_not_its_own(mod):
    run = docker("abc123 \ndef456 media\n111222 pe-rehearsal\n")
    why = mod.refusals(args_of(mod), GOOD_ENV, 0, run)
    assert len(why) == 1 and "2 container(s)" in why[0]


def test_its_own_leftover_containers_do_not_make_a_host_foreign(mod):
    assert mod.refusals(args_of(mod), GOOD_ENV, 0, docker("111222 pe-rehearsal\n333444 pe-rehearsal\n")) == []


def test_foreign_containers_are_accepted_only_with_the_explicit_override(mod):
    run = docker("abc123 media\n")
    assert mod.refusals(args_of(mod, "--allow-foreign-containers"), GOOD_ENV, 0, run) == []


def test_it_refuses_when_docker_or_compose_is_missing(mod):
    assert any("docker does not answer" in w for w in mod.refusals(args_of(mod), GOOD_ENV, 0, docker(ps_rc=127)))
    assert any("compose plugin" in w for w in mod.refusals(args_of(mod), GOOD_ENV, 0, docker(compose_rc=1)))


def test_every_reason_is_reported_at_once(mod):
    why = mod.refusals(mod.parse_args([]), {}, 1000, docker("x media\n"))
    assert len(why) == 4


# ── main: refusal touches nothing, exit codes ──────────────────────────────────────────────────────

def test_a_refused_run_exits_2_prints_the_reasons_and_never_runs_the_rehearsal(mod):
    out = io.StringIO()
    called = []
    code = mod.main(["--i-know-this-is-disposable"], env={}, euid=0, run=docker(),
                    rehearse=lambda a, r: called.append(1), out=out)
    doc = json.loads(out.getvalue())
    assert code == 2 and called == [] and doc["ok"] is False and doc["refused"]


def test_a_passing_rehearsal_exits_0_with_a_record(mod, tmp_path):
    out = io.StringIO()

    def rehearse(args, rec):
        rec.step("project-up", True)
        rec.step("namespace:ended", True, status="completed")
    json_out = tmp_path / "record.json"
    code = mod.main(["--i-know-this-is-disposable", "--json-out", str(json_out)], env=GOOD_ENV, euid=0, run=docker(),
                    rehearse=rehearse, out=out)
    doc = json.loads(out.getvalue())
    assert code == 0 and doc["ok"] is True and [s["step"] for s in doc["steps"]] == ["project-up", "namespace:ended"]
    assert json.loads(json_out.read_text()) == doc
    assert doc["not_exercised"] and all("at_s" in s for s in doc["steps"])


def test_a_failed_step_or_a_crash_exits_1(mod):
    out = io.StringIO()
    assert mod.main(["--i-know-this-is-disposable"], env=GOOD_ENV, euid=0, run=docker(),
                    rehearse=lambda a, r: r.step("canary:failed-as-planned", False, "status needs_operator"), out=out) == 1
    assert json.loads(out.getvalue())["failures"] == ["canary:failed-as-planned: status needs_operator"]

    def boom(args, rec):
        raise RuntimeError("docker fell over")
    out = io.StringIO()
    assert mod.main(["--i-know-this-is-disposable"], env=GOOD_ENV, euid=0, run=docker(), rehearse=boom, out=out) == 1
    assert "rehearsal-crashed" in out.getvalue()


def test_bad_arguments_are_a_usage_error(mod):
    with pytest.raises(SystemExit) as exc:
        mod.parse_args(["--timeout-s", "5"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        mod.parse_args(["--scenario", "everything"])


# ── the synthetic project and the agent ────────────────────────────────────────────────────────────

def test_the_compose_project_is_valid_yaml_with_the_dependency_chain_and_no_real_names(mod):
    doc = yaml.safe_load(mod.compose_text("busybox:1.36"))
    services = doc["services"]
    assert doc["name"] == "pe-rehearsal" and set(services) == {"gateway", "app1", "app2", "web"}
    assert services["app1"]["network_mode"] == services["app2"]["network_mode"] == "service:gateway"
    assert services["app2"]["depends_on"] == ["app1"] and services["app1"]["depends_on"] == ["gateway"]
    assert services["web"]["environment"]["GREETING"] == "web-ok"
    text = mod.compose_text()
    assert "192.168." not in text and "example" not in text.replace("busybox", "")


def test_dependency_order_puts_dependencies_first_and_rejects_cycles(mod):
    deps = {"app2": ["app1"], "app1": ["gateway"], "gateway": []}
    assert mod.dependency_order(deps, ["app2", "app1"]) == ["app1", "app2"]
    assert mod.dependency_order(deps, ["app1", "app2"]) == ["app1", "app2"]
    assert mod.dependency_order({"a": [], "b": []}, ["b", "a"]) == ["b", "a"]
    with pytest.raises(ValueError):
        mod.dependency_order({"a": ["b"], "b": ["a"]}, ["a", "b"])


def test_the_verification_plans_are_valid_for_the_real_catalogue(mod):
    sys.path.insert(0, str(ROOT))
    import os
    os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))
    from planet_express.missions.models import VerificationPlan
    from planet_express.missions.verify.plan import validate_plan
    for scenario in ("namespace", "canary-bad", "canary-fix"):
        plan = validate_plan(VerificationPlan.model_validate({"catalogue_v": 1, "checks": mod.verification_checks(scenario)}))
        assert plan.checks


def test_the_agent_command_is_an_isolated_absolute_argv(mod, tmp_path):
    argv = mod.agent_argv("namespace", str(tmp_path))
    assert argv[0].startswith("/") and argv[1] == "-I" and argv[2].endswith("rehearse_repair.py")
    ns = mod.parse_args(argv[3:])
    assert ns.command == "agent" and ns.agent_scenario == "namespace"


def test_the_agent_refuses_a_directory_that_is_not_a_rehearsal_stack(mod, tmp_path):
    ns = mod.parse_args(["agent", "--agent-scenario", "namespace", "--stack-dir", str(tmp_path)])
    assert mod.agent_main(ns) == 2
    ns = mod.parse_args(["agent", "--agent-scenario", "namespace", "--stack-dir", str(tmp_path), "--project", "media"])
    (tmp_path / mod.MARKER).write_text("x")
    assert mod.agent_main(ns) == 2


def test_the_record_shape(mod):
    clock = iter(range(100)).__next__
    rec = mod.Recorder(clock=lambda: float(clock()))
    rec.step("a", True, ids=["x"])
    rec.step("b", False, "went wrong")
    doc = rec.document(scenario="all")
    assert doc["ok"] is False and doc["failures"] == ["b: went wrong"] and doc["steps"][0]["ids"] == ["x"]
    assert set(doc) >= {"rehearsal", "scenario", "ok", "refused", "failures", "steps", "elapsed_s", "not_exercised"}


# ── scripts/run_rehearsal.sh ─────────────────────────────────────────────────────────────────────

import os
import subprocess

SCRIPT = ROOT / "scripts/run_rehearsal.sh"


def sh(*args, path=None, env=None):
    e = {**os.environ, **(env or {})}
    if path:
        e["PATH"] = f"{path}:{e['PATH']}"
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, env=e, check=False)


def test_the_wrapper_is_valid_shell_and_documented():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0
    assert "--vm HOST" in sh("--help").stdout


@pytest.mark.parametrize("args", [[], ["--here"], ["--bogus"], ["--vm"]])
def test_the_wrapper_refuses_to_guess_where_to_run(args):
    done = sh(*args)
    assert done.returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="the not-root refusal is only observable as a normal user")
def test_here_refuses_a_non_root_user_even_with_the_flag():
    done = sh("--here", "--i-know-this-is-disposable")
    assert done.returncode == 2 and "root" in done.stderr


def test_vm_mode_copies_the_tree_then_runs_the_rehearsal_as_root_with_the_switches(tmp_path):
    log = tmp_path / "calls.log"
    for name in ("rsync", "ssh"):
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
        stub.chmod(0o755)
    done = sh("--vm", "test-vm.invalid", "--", "--scenario", "canary", path=str(tmp_path))
    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("rsync") and "root@test-vm.invalid:/root/pe-rehearsal/" in calls[0]
    assert "--exclude .git" in calls[0]
    ssh = calls[1]
    assert ssh.startswith("ssh") and "root@test-vm.invalid" in ssh
    assert "PE_REHEARSAL_OK=1" in ssh and "scripts/rehearse_repair.py --i-know-this-is-disposable" in ssh
    assert "'--scenario' 'canary'" in ssh
