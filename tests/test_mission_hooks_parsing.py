"""Hook command parsing (slice 7a code-review round, trust mode): wrappers, separators, nested shells, environment dumps,
what cannot be read, and the whole of a large input. A hard-block command must be caught however it is wrapped."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.missions import hooks
from planet_express.missions.hooks import HookPolicy, classify

POLICY = HookPolicy(stacks_root="/srv/stacks")


def verdict(command, cwd="/work/ws"):
    d = classify("Bash", {"command": command}, POLICY, cwd)
    return d.verdict, d.cls


# ── 3: wrappers, separators, nested shells ────────────────────────────────────────────────────────

@pytest.mark.parametrize("command, cls", [
    ("sudo -u root docker volume prune", "docker"), ("sudo -E rm -rf /srv/stacks", "wipe"), ("sudo -H -n -- rm -rf /etc", "wipe"),
    ("sudo --user=root mkfs.ext4 /dev/sdb", "wipe"), ("nice -n 5 mkfs.ext4 /dev/sdb", "wipe"), ("nice -5 docker volume prune", "docker"),
    ("time -p docker volume prune", "docker"), ("time docker volume prune", "docker"), ("env FOO=1 docker volume prune", "docker"), ("env -i A=b docker volume prune", "docker"),
    ("env -u HOME docker volume prune", "docker"), ("timeout 30 docker volume prune", "docker"), ("timeout -s KILL 5 docker volume prune", "docker"),
    ("timeout --signal=KILL 5 rm -rf /usr", "wipe"), ("nohup docker volume prune", "docker"), ("xargs -n1 docker volume prune", "docker"),
    ("xargs -0 -I{} rm -rf /boot", "wipe"), ("command docker volume prune", "docker"), ("exec docker volume prune", "docker"), ("builtin cd x && docker volume prune", "docker"),
    ("FOO=1 BAR=2 docker volume prune", "docker"), ("ionice -c 3 nice -n 5 docker volume prune", "docker"), ("setsid -f docker volume prune", "docker"),
    ("sudo -u root nice -n 5 timeout 9 env X=1 docker volume prune", "docker"), ("doas -u root docker volume prune", "docker"),
])
def test_wrappers_with_their_flags_and_values_are_stripped(command, cls):
    assert verdict(command) == ("block", cls), command


@pytest.mark.parametrize("command", [
    "true & docker volume prune", "true &docker volume prune", "ls | docker volume prune", "ls; docker volume prune", "ls && docker volume prune", "ls || docker volume prune", "ls\ndocker volume prune",
    "echo $(docker volume prune)", "echo `docker volume prune`", "if x; then docker volume prune; fi", "while true; do docker volume prune; done", "if x; then ls; else docker volume prune; fi",
    "for i in a b; do docker volume prune $i; done", "(docker volume prune)", "{ docker volume prune; }", "! docker volume prune", "cat <(docker volume prune)",
])
def test_every_separator_and_shell_keyword_leads_to_the_command_after_it(command):
    assert verdict(command) == ("block", "docker"), command


@pytest.mark.parametrize("command", [
    "sh -c 'docker volume prune'", "bash -c 'docker volume prune'", "bash -lc 'docker volume prune'", "dash -ec \"ls && docker volume prune\"", "zsh -c 'rm -rf /etc'",
    "sudo sh -c 'docker volume prune'", "sh -c \"sh -c 'docker volume prune'\"", "eval 'docker volume prune'", "eval docker volume prune", "su root -c 'docker volume prune'",
    "bash -c 'if x; then docker volume prune; fi'", "timeout 5 bash -c 'docker volume prune'",
])
def test_nested_shells_are_read_through(command):
    assert verdict(command)[0] == "block", command


def test_nesting_is_bounded_and_what_cannot_be_read_but_names_a_hard_block_is_blocked():
    deep = "docker volume prune"
    for _ in range(6):
        deep = "sh -c " + json.dumps(deep)
    assert verdict(deep) == ("block", "docker")                                      # beyond the depth: never allowed
    assert verdict("python3 -c 'import os; os.system(\"docker volume prune\")'") == ("block", "docker")
    assert verdict("perl -e 'system(\"rm -rf /\")'") == ("block", "wipe")
    assert verdict("python3 -c 'import shutil; shutil.rmtree(\"/etc\")'") == ("block", "wipe")
    assert verdict("python3 -c 'print(open(\"/etc/planetexpress/k\").read())'") == ("block", "pe")
    assert verdict("sudo --weird-flag x docker volume prune")[0] == "block"          # an unknown long flag may take a value: unsure
    assert verdict("sudo -Z mkfs.ext4 /dev/sdb")[0] == "block"                       # an unknown short flag: unsure, names mkfs
    assert verdict("python3 -c 'print(1)'")[0] == "allow"                            # nothing on the list in it: allowed
    assert verdict("python3 -c 'import os; os.system(\"docker restart x\")'")[0] == "allow"


def test_a_harmless_nested_command_is_still_allowed():
    for command in ("sh -c 'ls -la'", "sudo -u app ls", "nice -n 5 pytest -q", "timeout 30 curl -s http://localhost/health",
                    "if true; then echo hi; fi", "env FOO=1 true", "git status && git diff"):
        assert verdict(command)[0] == "allow", command


# ── 4: only the listing forms are environment dumps ───────────────────────────────────────────────

@pytest.mark.parametrize("command", ["export", "export -p", "declare", "declare -p", "declare -x", "typeset -x", "printenv", "env", "set",
                                      "env -i", "env -u HOME", "printenv HOME", "sudo env", "echo a; export"])
def test_the_listing_forms_are_environment_dumps(command):
    assert verdict(command) == ("block", "secret"), command


@pytest.mark.parametrize("command", ["export FOO=bar", "export PATH=$PATH:/x", "declare -r X=1", "declare -a arr", "typeset -i n=3",
                                      "set -e", "set -euo pipefail", "env FOO=1 true", "export A=1 B=2", "FOO=1 export BAR"])
def test_assignments_and_options_are_not(command):
    assert verdict(command)[0] != "block" or verdict(command)[1] != "secret", command
    assert verdict(command)[1] != "secret", command


# ── 1: the whole of a large input ─────────────────────────────────────────────────────────────────

def run_cli(payload_text, policy=POLICY):
    out, err = io.StringIO(), io.StringIO()
    code = hooks.main(["pre"], stdin=io.StringIO(payload_text), stderr=err, environ={"PE_HOOK_POLICY": policy.to_json()})
    return code, err.getvalue()


def test_a_legitimate_large_write_is_parsed_and_allowed():
    payload = json.dumps({"tool_name": "Write", "cwd": "/work/ws", "tool_input": {"file_path": "/work/ws/big.txt", "content": "x" * 100_000}})
    assert len(payload) > 100_000
    assert run_cli(payload)[0] == 0


def test_a_large_heredoc_command_is_judged_on_all_of_it():
    body = "cat > f.txt <<'EOF'\n" + ("line of text\n" * 4000) + "EOF\n"
    ok = json.dumps({"tool_name": "Bash", "cwd": "/work/ws", "tool_input": {"command": body}})
    assert run_cli(ok)[0] == 0
    hidden = json.dumps({"tool_name": "Bash", "cwd": "/work/ws", "tool_input": {"command": body + "docker volume prune -f\n"}})
    assert len(body) > 8192 and run_cli(hidden)[0] == 2                    # a blocked command after a long preamble is still seen


def test_an_input_over_the_cap_is_a_failure_and_fails_closed():
    huge = json.dumps({"tool_name": "Write", "cwd": "/w", "tool_input": {"file_path": "/work/ws/a", "content": "x" * (2 * 1024 * 1024)}})
    code, err = run_cli(huge)
    assert code == 2 and "failing closed" in err
    code, _ = run_cli(huge, HookPolicy(blocked_failure="open"))
    assert code == 0                                                       # the configured mode, not a silent default


def test_a_command_longer_than_the_classifier_limit_is_a_failure_too():
    long_cmd = json.dumps({"tool_name": "Bash", "cwd": "/w", "tool_input": {"command": "echo " + "a" * (hooks.MAX_COMMAND_CHARS + 10)}})
    assert run_cli(long_cmd)[0] == 2
