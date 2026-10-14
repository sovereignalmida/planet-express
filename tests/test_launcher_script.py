"""The scripts that deliver the privileged tests and the rehearsal: they must refuse to be careless."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_privileged_tests.sh"


def run(*args):
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=False,
                          env={"PATH": "/usr/bin:/bin"})


def test_the_script_is_valid_posix_sh_and_executable():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0
    assert os.access(SCRIPT, os.X_OK) and SCRIPT.read_text().startswith("#!/bin/sh\n")


def test_here_refuses_without_the_disposable_acknowledgement():
    result = run("--here")
    assert result.returncode == 2 and "--i-know-this-is-disposable" in result.stderr


def test_here_refuses_to_run_as_a_non_root_user_even_when_acknowledged():
    if os.geteuid() == 0:
        return
    result = run("--here", "--i-know-this-is-disposable")
    assert result.returncode == 2 and "must run as root" in result.stderr


def test_unknown_arguments_are_refused_and_help_prints():
    assert run("--bogus").returncode == 2
    helped = run("--help")
    assert helped.returncode == 0 and "disposable" in helped.stdout


def test_the_script_only_sets_the_unlock_variable_inside_a_container_or_after_the_acknowledgement():
    text = SCRIPT.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert code.count("PE_PRIVILEGED_TESTS_OK=1") == 2
    assert "docker run --rm --privileged" in text and "-v \"$HERE\":/src:ro" in text     # the repo is mounted read-only


def test_the_privileged_tests_refuse_to_run_unless_unlocked():
    text = (ROOT / "tests" / "test_launcher_privileged.py").read_text()
    assert 'os.environ.get("PE_PRIVILEGED_TESTS_OK") == "1"' in text and "os.geteuid() == 0" in text


def test_the_rehearsal_refuses_a_system_range_uid_without_touching_anything():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "mission_containment_rehearsal.py"), "--workspace-root", "/nonexistent", "--uid-min", "100"],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 2 and '"ok": false' in result.stdout
