"""state.snapshot and python.env on a fake host with scripted commands (A2a)."""
import json

import pytest

from functools import partial

from planet_express.setup.apply import apply as _apply
from planet_express.setup.handlers import GET_PIP_URL, REQUIRED_MODULES
from planet_express.setup.host import RunResult
from test_setup_apply import fresh_host, make_plan, step

apply = partial(_apply, evidence_dir="/journal/evidence")
INSTALL, VENV = "/opt/pe", "/opt/pe/venv"
PY = f"{VENV}/bin/python"
DATA = "/opt/pe/data"
SNAP = f"{DATA}/snapshots/20261008T120000Z-pre-setup"
ENV = {"CASA_CONFIG": "/etc/pe/config.yaml", "CASA_DATA_DIR": DATA}


def host_with_checkout(**over):
    host = fresh_host()
    host.add_dir(INSTALL, uid=0, gid=0)
    host.add_file(f"{INSTALL}/requirements.txt", b"flask\n")
    host.add_dir(f"{INSTALL}/scripts")
    host.add_file(f"{INSTALL}/scripts/state_snapshot.py", b"# stdlib only\n")
    return host


def snapshot_plan(**over):
    params = {"install_dir": INSTALL, "run_user": "svc", "env": ENV, **over}
    return make_plan([step(1, "state.snapshot", INSTALL, **params)])


def env_plan(**over):
    params = {"install_dir": INSTALL, "venv_dir": VENV, "run_user": "svc", **over}
    return make_plan([step(1, "python.env", VENV, **params)])


SNAPSHOT_CMD = ("python3", f"{INSTALL}/scripts/state_snapshot.py", "create", "--label", "pre-setup")


def snapshot_creates(host, argv):
    host.add_dir(SNAP)
    host.add_file(f"{SNAP}/manifest.json", b"{}")
    return RunResult(0, SNAP + "\n")


# -- state.snapshot ------------------------------------------------------------------------------------------


def test_the_snapshot_runs_the_stdlib_script_as_the_service_user_with_only_casa_paths(tmp_path):
    host = host_with_checkout()
    host.command_results[SNAPSHOT_CMD] = partial(snapshot_creates)
    plan = snapshot_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert host.commands == [list(SNAPSHOT_CMD)] and host.run_as == ["svc"] and host.envs == [ENV]
    assert host.cwds == [INSTALL]
    from planet_express.setup.journal import Journal
    evidence = Journal(tmp_path / "j", plan.to_public()["plan_id"]).steps()["s01"].evidence
    assert evidence == [{"snapshot": SNAP}]


def test_a_missing_snapshot_script_refuses_before_anything_runs(tmp_path):
    host = fresh_host()
    plan = snapshot_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "checkout is incomplete" in result.reason and host.commands == []


@pytest.mark.parametrize("outcome, expect", [
    (RunResult(1, "", "no space left on device"), "no space left"),
    (RunResult(0, "/etc/passwd\n"), "outside the data directory"),
    (RunResult(0, ""), "outside the data directory"),
])
def test_a_failed_or_suspicious_snapshot_stops_the_plan(tmp_path, outcome, expect):
    host = host_with_checkout()
    host.command_results[SNAPSHOT_CMD] = outcome
    plan = snapshot_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and expect in result.reason


def test_a_snapshot_that_is_not_there_afterwards_is_not_reported_as_taken(tmp_path):
    host = host_with_checkout()
    host.command_results[SNAPSHOT_CMD] = RunResult(0, SNAP + "\n")      # claims a path, creates nothing
    plan = snapshot_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "does not show the expected result" in result.reason


# -- python.env ----------------------------------------------------------------------------------------------


def venv_cmd(host, argv):
    host.add_dir(VENV, uid=1000, gid=1000)
    host.add_dir(f"{VENV}/bin", uid=1000, gid=1000)
    host.add_file(PY, b"", mode=0o755, uid=1000, gid=1000)
    return RunResult(0, "")


def healthy_checks(host):
    host.command_results[("python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")] = RunResult(0, "")
    host.command_results[(PY, "-c", REQUIRED_MODULES)] = RunResult(0, "")


def test_a_fresh_environment_is_created_then_filled_by_the_service_user_with_a_fixed_umask(tmp_path):
    host = host_with_checkout()
    healthy_checks(host)
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    plan = env_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert [c[:3] for c in host.commands if c[0] == "python3" and "venv" in c] == [["python3", "-m", "venv"]]
    pip = [c for c in host.commands if c[:3] == [PY, "-m", "pip"] and "install" in c]
    assert pip == [[PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", f"{INSTALL}/requirements.txt"]]
    assert set(host.run_as) == {"svc"}                                  # never root
    creating = host.commands.index(["python3", "-m", "venv", VENV])
    assert host.umasks[creating] == 0o022 and host.umasks[host.commands.index(pip[0])] == 0o022
    from planet_express.setup.journal import Journal
    evidence = Journal(tmp_path / "j", plan.to_public()["plan_id"]).steps()["s01"].evidence
    assert any(e.get("created_venv") == VENV and "ino" in e for e in evidence)


def test_an_environment_that_already_imports_its_dependencies_is_left_completely_alone(tmp_path):
    host = host_with_checkout()
    host.add_dir(VENV)
    host.add_file(PY, b"", mode=0o755)
    healthy_checks(host)
    plan = env_plan()
    before = host.mutations
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert not any("install" in c for c in host.commands) and not any("venv" in c for c in host.commands)
    from planet_express.setup.journal import Journal
    assert Journal(tmp_path / "j", plan.to_public()["plan_id"]).steps()["s01"].satisfied is True


@pytest.mark.parametrize("probe, expect", [
    (RunResult(127, "", "not found"), "python3 was not found"),
    (RunResult(1, ""), "older than 3.11"),
])
def test_an_unusable_python_refuses_with_the_reason(tmp_path, probe, expect):
    host = host_with_checkout()
    host.command_results[("python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")] = probe
    plan = env_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and expect in result.reason
    assert not any("venv" in c for c in host.commands)


def test_a_missing_requirements_file_refuses(tmp_path):
    host = fresh_host()
    host.add_dir(INSTALL)
    plan = env_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "requirements.txt is missing" in result.reason


def fetches_pip(argv):
    return argv[:2] == ["python3", "-c"] and GET_PIP_URL in argv


def test_on_mos_pip_is_downloaded_into_the_environment_and_the_installer_script_is_removed(tmp_path):
    host = host_with_checkout()
    healthy_checks(host)
    get_pip = f"{VENV}/get-pip.py"
    host.command_results[("python3", "-m", "venv", "--without-pip", VENV)] = venv_cmd
    host.command_results[(PY, "-m", "pip", "--version")] = RunResult(1, "", "No module named pip")
    host.on(fetches_pip, lambda h, argv: (h.add_file(get_pip, b"# get-pip"), RunResult(0, ""))[1])
    plan = env_plan(bootstrap_pip=True)
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    run_pip = [PY, get_pip, "--quiet", "--disable-pip-version-check"]
    assert run_pip in host.commands                                      # run with the venv's own python
    assert host.umasks[host.commands.index(run_pip)] == 0o022
    assert get_pip not in host.nodes                                     # and then removed
    order = [host.commands.index(c) for c in (["python3", "-m", "venv", "--without-pip", VENV], run_pip)]
    assert order == sorted(order)


def test_a_failed_pip_download_says_the_host_may_lack_internet_access(tmp_path):
    host = host_with_checkout()
    healthy_checks(host)
    host.command_results[("python3", "-m", "venv", "--without-pip", VENV)] = venv_cmd
    host.command_results[(PY, "-m", "pip", "--version")] = RunResult(1, "")
    host.on(fetches_pip, RunResult(1, "", "URLError: Temporary failure in name resolution"))
    plan = env_plan(bootstrap_pip=True)
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "internet access" in result.reason and "name resolution" in result.reason


def test_a_failed_requirements_install_stops_and_reports_the_tail_of_pip_output(tmp_path):
    host = host_with_checkout()
    healthy_checks(host)
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    host.command_results[(PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
                          f"{INSTALL}/requirements.txt")] = RunResult(1, "", "ERROR: No matching distribution found for flask")
    plan = env_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "No matching distribution" in result.reason


def test_an_environment_that_cannot_import_after_installing_is_not_reported_as_built(tmp_path):
    host = host_with_checkout()
    host.command_results[("python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")] = RunResult(0, "")
    host.command_results[("python3", "-m", "venv", VENV)] = venv_cmd
    host.command_results[(PY, "-c", REQUIRED_MODULES)] = RunResult(1, "", "ModuleNotFoundError: yaml")
    plan = env_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "does not show the expected result" in result.reason
