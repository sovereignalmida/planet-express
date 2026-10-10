"""state.snapshot and python.env on a fake host with scripted commands (A2a)."""
import json
from functools import partial

import pytest
from test_setup_apply import fresh_host, make_plan, step

from planet_express.setup.apply import apply as _apply
from planet_express.setup.handlers import GET_PIP_URL
from planet_express.setup.host import RunResult

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
    host.on(lambda a: a[:2] == [PY, "-c"], RunResult(0, ""))


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
    host.on(lambda a: a[:2] == [PY, "-c"], RunResult(1, "", "ModuleNotFoundError: yaml"))
    plan = env_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "does not show the expected result" in result.reason


def existing_venv():
    host = host_with_checkout()
    host.add_dir(VENV)
    host.add_file(PY, b"", mode=0o755)
    host.command_results[("python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")] = RunResult(0, "")
    return host


def test_the_venv_check_runs_in_the_venv_and_carries_every_requirement(tmp_path):
    host = existing_venv()
    seen = []
    host.on(lambda a: a[:2] == [PY, "-c"], lambda h, a: (seen.append(a), RunResult(0, ""))[1])
    plan = env_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    script, wanted = seen[0][2], seen[0][3]
    assert "sys.version_info < (3, 11)" in script and "flask" in wanted
    assert not any("install" in c for c in host.commands)


def test_an_old_or_incomplete_venv_is_not_trusted_and_gets_its_requirements_installed(tmp_path):
    host = existing_venv()
    state = {"fixed": False}

    def check(h, argv):
        return RunResult(0 if state["fixed"] else 3, "", "PackageNotFoundError: gunicorn")

    def pip(h, argv):
        state["fixed"] = True
        return RunResult(0, "")
    host.on(lambda a: a[:2] == [PY, "-c"], check)
    host.on(lambda a: a[:3] == [PY, "-m", "pip"] and "install" in a, pip)
    host.on(lambda a: a[:3] == [PY, "-m", "pip"] and "--version" in a, RunResult(0, "pip 24"))
    plan = env_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert any(c[:3] == [PY, "-m", "pip"] and "install" in c for c in host.commands)


def test_a_snapshot_path_that_climbs_out_of_the_snapshots_directory_is_rejected(tmp_path):
    host = host_with_checkout()
    host.command_results[SNAPSHOT_CMD] = RunResult(0, f"{DATA}/snapshots/../../elsewhere\n")
    plan = snapshot_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "outside the data directory" in result.reason


def test_the_real_check_script_accepts_a_good_environment_and_rejects_a_missing_or_too_new_requirement():
    import subprocess
    import sys

    from planet_express.setup.handlers import _CHECK_ENV
    run = lambda reqs: subprocess.run([sys.executable, "-c", _CHECK_ENV, json.dumps(reqs)], capture_output=True, check=False).returncode
    assert run(["pydantic>=2.0", "pyyaml>=6.0", "# comment-free", "pytest>=1.0"]) == 0
    assert run(["definitely-not-installed-pkg>=1"]) == 3
    assert run(["pydantic>=999.0"]) == 4


def test_the_pip_installer_is_pinned_to_a_commit_and_a_digest_in_both_places():
    import re
    from pathlib import Path

    from planet_express.setup.handlers import GET_PIP_SHA256
    assert re.search(r"/pypa/get-pip/[0-9a-f]{40}/", GET_PIP_URL) and "bootstrap.pypa.io" not in GET_PIP_URL
    script = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
    assert GET_PIP_URL in script and GET_PIP_SHA256 in script and "bootstrap.pypa.io" not in script


def test_the_fetch_script_refuses_a_download_that_does_not_match_its_digest(tmp_path):
    import hashlib
    import subprocess
    import sys

    from planet_express.setup.handlers import _FETCH
    source = tmp_path / "pip.py"
    source.write_bytes(b"print('hello')\n")
    good = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "out.py"
    ok = subprocess.run([sys.executable, "-c", _FETCH, source.as_uri(), str(target), good], capture_output=True, check=False)
    assert ok.returncode == 0 and target.read_bytes() == source.read_bytes()
    target.unlink()
    bad = subprocess.run([sys.executable, "-c", _FETCH, source.as_uri(), str(target), "0" * 64], capture_output=True, check=False)
    assert bad.returncode != 0 and not target.exists()
