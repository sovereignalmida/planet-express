"""The Operator Console under the REAL launcher (root-only, disposable VM only): the phase-0 mechanics, the cleanup session, settings isolation, the state directory and the Console end to end.

The real `claude` runs as a session uid in its own cgroup against a stand-in Messages API (no model, no cost). It checks, with the
launcher's own rules in force, what the Console's turn model needs: resume across separate contained launches, live stream-json,
kill/freeze mid-turn then resume, what a turn leaves in the workspace, and how that workspace is cleaned up.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from test_launcher_privileged import ENABLED, RANGE, SESSIONS, UIDS, WS, host  # noqa: F401  (host is the module fixture)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "spikes"))
pytestmark = [pytest.mark.skipif(not ENABLED, reason="root-only spike: run scripts/run_privileged_tests.sh on the disposable VM")]

CLAUDE = os.environ.get("PE_P0_CLAUDE", "/usr/local/bin/claude")


@pytest.fixture(scope="module")
def standin():
    if not os.path.exists(CLAUDE):
        pytest.skip("no claude CLI on this VM")
    import console_p0_e1_e2 as base
    import console_p0_e3_e4_e6 as e
    def script(last, data):
        if "SLOW" in last:
            return [(0.5, f"chunk{i} ") for i in range(12)]
        done = any(isinstance(m.get("content"), list) and any(c.get("type") == "tool_result" for c in m["content"])
                   for m in data["messages"][-1:])
        if "USEBASH" in last and not done:
            return [("tool", "Bash", {"command": "true", "description": "t"})]
        return None
    srv, _ = e.server_with(script)
    yield srv, base.FAKE
    srv.shutdown()


def turn_argv(prompt, flags, out, state, pre=""):
    cmd = (f"{pre}exec {CLAUDE} -p {json.dumps(prompt)} {flags} --output-format stream-json --verbose --include-partial-messages "
           f"--disallowedTools WebFetch,WebSearch > {out} 2> {out}.err")
    return ["/bin/sh", "-c", cmd]


def env_for(url, state, key):
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": state, "CLAUDE_CONFIG_DIR": f"{state}/cfg", "LANG": "C.UTF-8",
            "ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": key, "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def wait_empty(host, sid, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if host.core.call("empty", session_id=sid)["empty"]:
            return True
        time.sleep(0.3)
    return False


def read(host, path):
    try:
        return host.core.call("read", path=path)["text"]
    except Exception:
        return ""


def messages_have(fake, since, needle):
    return any(needle in json.dumps(b.get("messages", [])) for (_, p, _, b) in fake.requests[since:] if "count_tokens" not in p and b.get("messages"))


def launch_turn(host, url, state, key, prompt, flags, uid=UIDS[0]):
    """One contained turn in its own session id and its own fresh workspace; the conversation state lives in `state`."""
    sid = host.sid("p0")
    ws = host.cwd(sid)
    os.makedirs(ws, mode=0o700)
    os.chown(ws, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    out = f"{ws}/out.jsonl"
    _, got = host.launch(turn_argv(prompt, flags, out, state), uid=uid, sid=sid, make_cwd=False, env=env_for(url, state, key))
    return sid, ws, out


def new_state(host, uid=UIDS[0]):
    """A conversation-state directory the session uid owns. Created here as root for the spike; the real design must decide who makes it."""
    state = f"/tmp/pe-p0-state-{uuid.uuid4().hex[:8]}"
    os.makedirs(state, mode=0o700)
    os.chown(state, uid, uid)
    return state


def test_resume_works_across_separate_contained_launches_with_a_code_per_turn(host, standin):
    srv, fake = standin
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    state, conv = new_state(host), str(uuid.uuid4())
    try:
        fake.requests.clear()
        sid1, ws1, out1 = launch_turn(host, url, state, "code-one", "Remember PINEAPPLE", f"--session-id {conv}")
        assert wait_empty(host, sid1)
        events = [json.loads(line) for line in read(host, out1).splitlines() if line.strip()]
        assert events and events[0]["type"] == "system" and events[-1]["type"] == "result", read(host, out1 + ".err")[:300]
        mark = len(fake.requests)
        sid2, ws2, out2 = launch_turn(host, url, state, "code-two", "what word?", f"--resume {conv}")
        assert wait_empty(host, sid2)
        assert messages_have(fake, mark, "PINEAPPLE"), read(host, out2 + ".err")[:300]
        keys2 = {k for (_, _, k, _) in fake.requests[mark:]}
        assert keys2 == {"code-two"}
        assert host.core.call("empty", session_id=sid2)["empty"] is True
    finally:
        for s in (locals().get("sid1"), locals().get("sid2")):
            if s:
                host.finish(s)
        shutil.rmtree(state, ignore_errors=True)


def test_kill_by_cgroup_mid_turn_then_resume_keeps_the_context_and_freeze_thaw_completes(host, standin):
    srv, fake = standin
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    state, conv = new_state(host), str(uuid.uuid4())
    sids = []
    try:
        sid, ws, out = launch_turn(host, url, state, "c1", "SLOW remember PINEAPPLE", f"--session-id {conv}")
        sids.append(sid)
        time.sleep(1.5)
        assert host.core.call("kill", session_id=sid)["empty"] is True                 # cgroup.kill mid-stream
        mark = len(fake.requests)
        sid2, ws2, out2 = launch_turn(host, url, state, "c2", "what word?", f"--resume {conv}")
        sids.append(sid2)
        assert wait_empty(host, sid2)
        assert messages_have(fake, mark, "PINEAPPLE")
        # freeze for a while mid-stream, thaw, the turn must finish on its own
        sid3, ws3, out3 = launch_turn(host, url, state, "c3", "SLOW again", f"--resume {conv}")
        sids.append(sid3)
        time.sleep(1.5)
        assert "frozen 1" in host.core.call("freeze", session_id=sid3)["events"]
        time.sleep(8)
        assert "frozen 0" in host.core.call("thaw", session_id=sid3)["events"]
        assert wait_empty(host, sid3, timeout=60)
        last = [json.loads(x) for x in read(host, out3).splitlines() if x.strip()][-1]
        print("after freeze/thaw the last event is", last.get("type"), last.get("subtype"))
        assert last.get("type") == "result"
    finally:
        for s in sids:
            host.finish(s)
        shutil.rmtree(state, ignore_errors=True)


def test_a_turn_with_home_in_the_workspace_leaves_a_tree_the_helper_then_refuses_and_core_cannot_delete(host, standin):
    """The spike's E8. This is what the first live mission hit."""
    srv, fake = standin
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    sid = host.sid("p0ws")
    ws = host.cwd(sid)
    os.makedirs(ws, mode=0o700)
    os.chown(ws, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    out = f"{ws}/out.jsonl"
    try:
        # the live mission left ~/.cache as drwx--S--- (the CLI or a tool it ran chose 0700 explicitly): reproduce exactly that, as the session uid
        pre = "mkdir -m 700 $HOME/.cache && echo x > $HOME/.cache/f; "
        _, _ = host.launch(turn_argv("hello", "", out, ws, pre), uid=UIDS[0], sid=sid, make_cwd=False, env=env_for(url, ws, "k"))   # HOME=workspace
        assert wait_empty(host, sid)
        tree = sorted(p.name for p in Path(ws).iterdir())
        modes = {p.name: oct(p.stat().st_mode & 0o7777) for p in Path(ws).iterdir() if p.is_dir()}
        modes.update({p.name: oct(p.stat().st_mode & 0o7777) for p in Path(ws).iterdir() if p.is_dir()})
        print("workspace after one turn:", tree, modes)
        # (1) a second launch into the same workspace is refused by the helper (it hands over regular files only)
        sid_b = host.sid("p0ws2")
        with pytest.raises(Exception) as err:
            host.core.call("launch", session_id=sid_b, uid=UIDS[0], cwd=ws, argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"}, make_cwd=False)
        print("second launch into that workspace:", str(err.value)[:160])
        # (2) can Core delete it?
        if host.strategy == "sudo":
            subprocess.run(["sudo", "-n", "-u", "pe-core-test", "rm", "-rf", ws], capture_output=True)
            left = Path(ws).exists()
            print("strategy sudo: Core's own rm -rf left the workspace behind:", left)
            assert left, "expected the 0700 directory to defeat Core's group access"
        else:
            left = True                                                      # Core is root here (MOS): the bug cannot occur, still prove the fix
        # (3) the proposed fix: a cleanup session as the same uid, from an empty scratch dir, deleting the old workspace's contents
        if left:
            scratch_sid = host.sid("p0clean")
            scratch = host.cwd(scratch_sid)
            os.makedirs(scratch, mode=0o700)
            os.chown(scratch, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
            _, _ = host.launch(["/usr/bin/find", ws, "-mindepth", "1", "-delete"], uid=UIDS[0], sid=scratch_sid, make_cwd=False,
                               env={"PATH": "/usr/bin:/bin"})
            assert wait_empty(host, scratch_sid)
            host.core.call("cleanup", session_id=scratch_sid)
            shutil.rmtree(scratch, ignore_errors=True)
            remaining = list(Path(ws).iterdir())
            print("after the cleanup session, entries left in the old workspace:", remaining)
            assert remaining == []
    finally:
        host.finish(sid)


def test_production_cleanup_frees_a_workspace_core_cannot_remove(host):
    """`Supervisor._clear_workspace`, the production path, under the real launcher: a 0700 tree made by the session uid is emptied by a
    cleanup session as the same uid, then the workspace is removed and the uid is free. The same steps run under both Core strategies."""
    from planet_express.missions.supervisor import cleanup_id
    sid = host.sid("clean")
    host.launch(["/bin/sh", "-c", "mkdir -m 700 .cache && mkdir -m 700 .cache/deep && echo x > .cache/deep/f && chmod 500 .cache/deep && "
                 "mkdir -m 000 .sealed && ln -s /etc .cache/link"], uid=UIDS[0], sid=sid)
    assert wait_empty(host, sid)
    host.core.call("cleanup", session_id=sid)
    ws = host.cwd(sid)
    assert os.path.isdir(f"{ws}/.cache/deep")                                  # the session left a tree behind
    done, started = False, time.time()
    while not done and time.time() - started < 60:
        got = host.core.call("clear_workspace", session_id=sid, uid=UIDS[0], workspace_dir=WS)
        done = got["done"]
        time.sleep(0.3)
    assert done, got
    assert not os.path.exists(ws) and not os.path.exists(f"{WS}/{cleanup_id(sid)}")      # the workspace and the scratch directory are gone
    assert not host.core.call("uid_in_use", uid=UIDS[0])["in_use"]
    assert not os.path.exists(f"{SESSIONS}/{cleanup_id(sid)}")                           # and so is the cleanup session's cgroup


def test_planted_project_settings_are_ignored_and_pes_own_hook_still_fires(host, standin):
    """The production launch flags (`claude_code_argv`): a settings file the session plants in its own workspace governs nothing,
    while the hook PE installs through `--settings` runs on every tool call. The control run without the flag shows the plant works."""
    import shlex

    from planet_express.missions.hooks import claude_code_argv
    srv, _ = standin
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    results = {}
    for label, forced in (("control", False), ("forced", True)):
        state = new_state(host)
        hook_dir = f"/tmp/pe-p0-hooks-{uuid.uuid4().hex[:8]}"
        os.makedirs(hook_dir, mode=0o755)
        os.chmod(hook_dir, 0o755)
        planted_log, pe_log = f"{state}/planted.log", f"{state}/pe.log"
        pe_settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"echo pe >> {pe_log}"}]}]}}
        planted = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"echo planted >> {planted_log}"}]}]},
                   "permissions": {"allow": ["Bash"]}}
        Path(hook_dir, "settings.json").write_text(json.dumps(pe_settings))
        os.chmod(f"{hook_dir}/settings.json", 0o644)
        base = [CLAUDE, "-p", "USEBASH", "--max-turns", "3", "--output-format", "json", "--disallowedTools", "WebFetch,WebSearch"]
        argv = claude_code_argv(base, hook_dir) if forced else [*base, "--settings", f"{hook_dir}/settings.json"]
        sid = host.sid("p0set")
        ws = host.cwd(sid)
        os.makedirs(ws, mode=0o700)
        os.chown(ws, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
        script = (f"mkdir -p .claude && printf '%s' {shlex.quote(json.dumps(planted))} > .claude/settings.json; "
                  f"exec {' '.join(shlex.quote(a) for a in argv)} > {ws}/out.json 2> {ws}/out.err")
        try:
            host.launch(["/bin/sh", "-c", script], uid=UIDS[0], sid=sid, make_cwd=False, env=env_for(url, state, "k"))
            assert wait_empty(host, sid)
            results[label] = {"planted_hook_ran": os.path.exists(planted_log), "pe_hook_ran": os.path.exists(pe_log),
                              "err": read(host, f"{ws}/out.err")[:200]}
        finally:
            host.finish(sid)
            shutil.rmtree(state, ignore_errors=True)
            shutil.rmtree(hook_dir, ignore_errors=True)
    print("settings-source results:", results)
    assert results["control"]["planted_hook_ran"] and results["control"]["pe_hook_ran"], results       # the probe is not vacuous
    assert results["forced"]["pe_hook_ran"] and not results["forced"]["planted_hook_ran"], results


def test_a_conversation_state_directory_survives_across_sessions_of_the_same_uid_and_is_closed_to_every_other(host):
    """`state_dir` under the real helper: session A writes into the adopted directory; session B (new session id, new workspace, new
    cgroup, same uid) reads it back; a session of another uid is refused; and the directory itself, not its contents, was handed over."""
    conv = f"C-conv-{os.getpid()}-{host.n}"
    state = f"{WS}/{conv}"
    env = {"PATH": "/usr/bin:/bin", "STATE": state}
    sids, refused_ids = [], []
    try:
        a = host.sid("stA")
        sids.append(a)
        host.launch(["/bin/sh", "-c", 'mkdir -m 700 "$STATE/cfg" "$STATE/.cache" && echo remembered > "$STATE/cfg/fact"'], uid=UIDS[0], sid=a,
                    env=env, state_dir=conv)
        assert wait_empty(host, a)
        host.finish(a)
        sids.remove(a)
        info = os.stat(state)
        assert info.st_uid == UIDS[0] and oct(info.st_mode & 0o7777) == "0o2770"
        b = host.sid("stB")
        sids.append(b)
        host.launch(["/bin/sh", "-c", 'cat "$STATE/cfg/fact" > "$STATE/cfg/seen"'], uid=UIDS[0], sid=b, env=env, state_dir=conv)
        assert wait_empty(host, b)
        assert open(f"{state}/cfg/seen").read().strip() == "remembered"
        host.finish(b)
        sids.remove(b)
        for label, uid, name, why in (("stC", UIDS[1], conv, "state dir owner"), ("stD", UIDS[0], "C-link", "state dir")):
            if name == "C-link":
                os.symlink(state, f"{WS}/C-link")
            refused = host.sid(label)
            refused_ids.append(refused)
            with pytest.raises(RuntimeError, match=why):
                host.launch(["/bin/true"], uid=uid, sid=refused, env=env, state_dir=name)         # another uid / a symlink: refused
    finally:
        for s in sids:
            host.finish(s)
        for refused in refused_ids:                       # never launched, so no cgroup: only the workspace Core made for it
            shutil.rmtree(host.cwd(refused), ignore_errors=True)
        if os.path.islink(f"{WS}/C-link"):
            os.unlink(f"{WS}/C-link")
        shutil.rmtree(state, ignore_errors=True)


def test_the_console_end_to_end_under_the_real_launcher_two_turns_one_uid_resumed_context_and_an_archive_that_frees_everything(host, standin):
    """The daemon's assembly (real supervisor, containment and helper; the real claude CLI against a stand-in model): a conversation
    keeps its uid and its agent state across two separate contained sessions, the second resumes the first, and archiving empties
    the state directory (including a 0700 tree Core cannot delete) and returns the uid."""
    srv, fake = standin
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    db_dir = f"/tmp/pe-console-db-{uuid.uuid4().hex[:8]}"
    os.makedirs(db_dir, mode=0o700)
    os.chown(db_dir, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    host.core.call("console_setup", workspace_dir=WS, base_url=url, claude=CLAUDE, db_dir=db_dir)
    conv = host.core.call("console_open", text="Remember the word PINEAPPLE")
    state = f"{WS}/{conv['state_dir']}"

    def wait_for(want_mission, turns, timeout=120):
        end, look = time.time() + timeout, {}
        while time.time() < end:
            host.core.call("console_tick")
            look = host.core.call("console_look", conversation_id=conv["id"])
            if look["mission"] == want_mission and look["turns"] == turns and all(r[2] for r in look["sessions"]):
                return look
            time.sleep(0.4)
        raise AssertionError(look)
    fake.requests.clear()
    first = wait_for("awaiting_decision", 1)
    uid = first["uid"]
    assert first["reserved"] == uid and RANGE[0] <= uid <= RANGE[1]
    assert [m[0] for m in first["messages"]] == ["operator", "agent"] and first["messages"][1][1]
    assert first["tokens"] > 0                                                         # the real broker counted the turn's tokens and PE charged them
    info = os.stat(state)
    assert info.st_uid == uid and oct(info.st_mode & 0o7777) == "0o2770"             # adopted by the helper for the conversation's uid
    assert os.path.isdir(f"{state}/cfg") and not host.core.call("uid_in_use", uid=uid)["in_use"]
    assert not [d for d in os.listdir(WS) if d.startswith("S-")]                      # every session workspace was removed
    mark = len(fake.requests)
    host.core.call("console_post", conversation_id=conv["id"], text="what word did I ask you to remember?")
    second = wait_for("awaiting_decision", 2)
    assert len(second["sessions"]) == 2 and {r[1] for r in second["sessions"]} == {uid}      # a new session, the same uid
    assert messages_have(fake, mark, "PINEAPPLE"), "the second contained process did not resume the first one's conversation"
    # leave a tree Core cannot delete, as the CLI's cache is, then archive
    plant = host.sid("plant2")
    host.launch(["/bin/sh", "-c", f"mkdir -m 700 {state}/.cache && mkdir -m 000 {state}/.cache/sealed && echo x > {state}/.cache/f"],
                uid=uid, sid=plant)
    assert wait_empty(host, plant)
    host.finish(plant)
    assert host.core.call("console_archive", conversation_id=conv["id"])["archived"] is True
    end, look = time.time() + 120, {}
    while time.time() < end:
        host.core.call("console_tick")
        look = host.core.call("console_look", conversation_id=conv["id"])
        if look["state"] == "archived":
            break
        time.sleep(0.4)
    assert look["state"] == "archived" and look["mission"] == "completed" and look["reserved"] is None and look["uid"] is None, look
    assert not os.path.exists(state) and not host.core.call("uid_in_use", uid=uid)["in_use"]
    leftovers = [d for d in os.listdir(WS) if d.startswith(("S-", "X-", "C-"))]
    assert not leftovers, leftovers
    shutil.rmtree(db_dir, ignore_errors=True)
