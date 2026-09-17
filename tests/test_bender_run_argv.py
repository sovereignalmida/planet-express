"""
casa_bender.run_argv (landing 1a, T3): the non-shell runner every typed action and the
verifier go through. Written before the implementation.

Contract: argv list only (a string is a TypeError, never silently shell-split), always
shell=False, a minimal environment (PATH/HOME/LANG plus the non-secret Docker connection
variables, so LLM keys and the bot token in casa-planetexpress's environment never reach
a child process while custom-context and rootless Docker still work), (returncode, stdout,
stderr) with output stripped, timeout -> RUN_ARGV_TIMEOUT_EXIT (124), missing
executable -> 127.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_bender as bender


def test_rejects_string_command():
    with pytest.raises(TypeError):
        bender.run_argv("docker ps", timeout=5)


def test_rejects_empty_argv():
    with pytest.raises(ValueError):
        bender.run_argv([], timeout=5)


def test_rejects_non_string_argv_items():
    with pytest.raises(TypeError):
        bender.run_argv(["echo", 42], timeout=5)


def test_shell_metacharacters_are_passed_literally():
    payload = "$(id) && rm -rf /tmp/pe-never ; `whoami` | cat > /dev/null"
    rc, out, err = bender.run_argv(["echo", payload], timeout=5)
    assert rc == 0
    assert out == payload
    assert err == ""


def test_always_calls_subprocess_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="  ok \n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert bender.run_argv(["docker", "ps"], timeout=7) == (0, "ok", "")
    assert seen["cmd"] == ["docker", "ps"]
    assert seen["kwargs"].get("shell") is False
    assert seen["kwargs"].get("timeout") == 7


def test_child_gets_only_minimal_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-must-not-leak")
    monkeypatch.setenv("TG_BOT_TOKEN", "123:must-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    rc, out, _ = bender.run_argv(["env"], timeout=5)
    assert rc == 0
    keys = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    assert "PATH" in keys
    assert keys <= set(bender._RUN_ARGV_ENV_KEYS)
    assert "ANTHROPIC_API_KEY" not in keys and "TG_BOT_TOKEN" not in keys
    assert "must-not-leak" not in out


def test_docker_connection_environment_is_preserved(monkeypatch):
    # Custom contexts and rootless Docker only work if the CLI still sees these.
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "homelab")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/ssh-agent.socket")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leak")
    rc, out, _ = bender.run_argv(["env"], timeout=5)
    assert rc == 0
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert env["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
    assert env["DOCKER_CONTEXT"] == "homelab"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/ssh-agent.socket"
    assert "OPENAI_API_KEY" not in env


def test_timeout_returns_typed_exit_code(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr("subprocess.run", fake_run)
    rc, out, err = bender.run_argv(["docker", "compose", "config", "--services"], timeout=3)
    assert rc == bender.RUN_ARGV_TIMEOUT_EXIT == 124
    assert out == ""
    assert "timed out after 3s" in err


def test_missing_executable_returns_127():
    rc, out, err = bender.run_argv(["pe-definitely-not-a-real-binary"], timeout=5)
    assert rc == 127
    assert out == ""
    assert "not found" in err.lower()


def test_nonzero_exit_is_returned_not_raised():
    rc, _out, _err = bender.run_argv(["false"], timeout=5)
    assert rc == 1



# ── run_argv_bounded (T29): memory-bounded capture for docker logs ─────────────────
PY = sys.executable


def test_bounded_keeps_the_newest_bytes_and_drops_the_partial_first_line():
    script = "import sys\nfor i in range(20000): sys.stdout.write(f'line {i:05d}\\n')"
    rc, out, err, truncated = bender.run_argv_bounded([PY, "-c", script], timeout=10, max_bytes=4096)
    assert rc == 0 and truncated and err == ""
    assert len(out.encode()) <= 4096
    assert out.endswith("line 19999\n")                 # newest kept, NOT stripped
    assert out.splitlines()[0].startswith("line ")       # no partial first line
    assert all(len(line) == 10 for line in out.splitlines())


def test_bounded_small_output_is_untouched_and_not_stripped():
    rc, out, err, truncated = bender.run_argv_bounded(
        [PY, "-c", "import sys; sys.stdout.write('a  \\n'); sys.stderr.write('e\\n')"], timeout=10, max_bytes=4096)
    assert (rc, out, err, truncated) == (0, "a  \n", "e\n", False)


def test_bounded_timeout_missing_executable_and_argv_rules():
    rc, out, _err, truncated = bender.run_argv_bounded([PY, "-c", "import time; time.sleep(5)"], timeout=0.5, max_bytes=64)
    assert rc == bender.RUN_ARGV_TIMEOUT_EXIT and out == "" and not truncated
    assert bender.run_argv_bounded(["definitely-not-a-real-binary-pe"], timeout=5, max_bytes=64)[0] == 127
    with pytest.raises(TypeError):
        bender.run_argv_bounded("echo hi", timeout=5, max_bytes=64)
    with pytest.raises(ValueError):
        bender.run_argv_bounded(["true"], timeout=5, max_bytes=0)


def test_bounded_child_gets_only_minimal_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    rc, out, _err, _t = bender.run_argv_bounded(
        [PY, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'absent'))"], timeout=10, max_bytes=1024)
    assert rc == 0 and out.strip() == "absent"
