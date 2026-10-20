"""Hook command parsing (slice 7a code-review round): option grammars, wrappers, separators, nested shells, environment dumps,
and the whole of a large input."""
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

POLICY = HookPolicy(live_trees=("/srv/live",), protected_paths=("/mnt/data",))


def verdict(command, cwd="/work/ws"):
    d = classify("Bash", {"command": command}, POLICY, cwd)
    return d.verdict, d.cls


# ── 2: publish checks read each tool's option grammar ─────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git -C /work/ws push", "git -C dir push origin main", "git -c user.name=x push", "git -c a=b -C d push",
    "git --git-dir=/work/ws/.git push", "git --git-dir /work/ws/.git push", "git --no-pager push", "git -P push",
    "git --work-tree=/work/ws push", "docker --context prod push img:1", "docker -H tcp://h:2375 push img", "docker image push img",
    "docker --config /x push i", "podman --remote push i", "docker buildx build --push .", "docker compose -f x.yml push",
    "gh -R org/repo release create v1", "gh --repo org/repo pr create", "gh --hostname h.example release upload v1 f",
    "npm --prefix pkg publish", "yarn --cwd pkg publish", "cargo --quiet publish", "twine upload dist/*", "helm cm-push chart repo",
])
def test_publishing_is_found_whatever_options_come_first(command):
    got = verdict(command)
    if command.startswith("helm cm-push"):
        assert got[0] == "allow"                        # a plugin verb this hook does not know is not guessed at
    else:
        assert got == ("block", "publish"), command


@pytest.mark.parametrize("command", ["git -C dir status", "git -c a=b log", "git --git-dir=.git diff", "docker --context x ps",
                                      "gh -R org/repo issue list", "npm --prefix pkg test", "gh release list --repo r"])
def test_the_same_options_before_a_harmless_verb_are_not_publishing(command):
    assert verdict(command)[0] != "block" or verdict(command)[1] != "publish"
    if "release list" not in command:
        assert verdict(command)[1] != "publish"


def test_destructive_git_in_a_live_tree_is_found_through_git_dir_and_work_tree_options():
    assert verdict("git --git-dir=/srv/live/app/.git reset --hard")[0] == "block"
    assert verdict("git --work-tree=/srv/live/app reset --hard")[0] == "block"
    assert verdict("git -C/srv/live/app reset --hard")[0] == "block"


# ── 3: wrappers, separators, nested shells ────────────────────────────────────────────────────────

@pytest.mark.parametrize("command, cls", [
    ("sudo -u root git push", "publish"), ("sudo -E rm -rf /srv/x", "wipe"), ("sudo -H -n -- rm -rf /etc", "wipe"),
    ("sudo --user=root mkfs.ext4 /dev/sdb", "wipe"), ("nice -n 5 mkfs.ext4 /dev/sdb", "wipe"), ("nice -5 git push", "publish"),
    ("time -p git push", "publish"), ("time git push", "publish"), ("env FOO=1 git push", "publish"), ("env -i A=b git push", "publish"),
    ("env -u HOME git push", "publish"), ("timeout 30 git push", "publish"), ("timeout -s KILL 5 git push", "publish"),
    ("timeout --signal=KILL 5 rm -rf /usr", "wipe"), ("nohup git push", "publish"), ("xargs -n1 git push", "publish"),
    ("xargs -0 -I{} rm -rf /var", "wipe"), ("command git push", "publish"), ("exec git push", "publish"), ("builtin cd x && git push", "publish"),
    ("FOO=1 BAR=2 git push", "publish"), ("ionice -c 3 nice -n 5 git push", "publish"), ("setsid -f git push", "publish"),
    ("sudo -u root nice -n 5 timeout 9 env X=1 git push", "publish"), ("doas -u root git push", "publish"),
])
def test_wrappers_with_their_flags_and_values_are_stripped(command, cls):
    assert verdict(command) == ("block", cls), command


@pytest.mark.parametrize("command", [
    "true & git push", "true &git push", "ls | git push", "ls; git push", "ls && git push", "ls || git push", "ls\ngit push",
    "echo $(git push)", "echo `git push`", "if x; then git push; fi", "while true; do git push; done", "if x; then ls; else git push; fi",
    "for i in a b; do git push $i; done", "(git push)", "{ git push; }", "! git push", "cat <(git push)",
])
def test_every_separator_and_shell_keyword_leads_to_the_command_after_it(command):
    assert verdict(command) == ("block", "publish"), command


@pytest.mark.parametrize("command", [
    "sh -c 'git push'", "bash -c 'git push'", "bash -lc 'git push'", "dash -ec \"ls && git push\"", "zsh -c 'rm -rf /etc'",
    "sudo sh -c 'git push'", "sh -c \"sh -c 'git push'\"", "eval 'git push'", "eval git push", "su root -c 'git push'",
    "bash -c 'if x; then git push; fi'", "timeout 5 bash -c 'git push'",
])
def test_nested_shells_are_read_through(command):
    assert verdict(command)[0] == "block", command


def test_nesting_is_bounded_and_what_cannot_be_read_with_a_blocked_word_is_asked_about():
    deep = "git push"
    for _ in range(6):
        deep = "sh -c " + json.dumps(deep)
    assert verdict(deep)[0] in ("ask", "block")                                      # beyond the depth: never allow
    assert verdict("python3 -c 'import os; os.system(\"git push\")'")[0] == "ask"    # code in another language, naming git
    assert verdict("perl -e 'system(\"rm -rf /\")'")[0] in ("ask", "block")
    assert verdict("sudo --weird-flag x git push")[0] in ("ask", "block")            # an unknown long flag may take a value: unsure
    assert verdict("sudo -Z git push")[0] in ("ask", "block")                        # an unknown short flag: unsure, names git
    assert verdict("python3 -c 'print(1)'")[0] == "allow"                            # nothing blocked-class in it: allowed


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
    hidden = json.dumps({"tool_name": "Bash", "cwd": "/work/ws", "tool_input": {"command": body + "git push origin main\n"}})
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
