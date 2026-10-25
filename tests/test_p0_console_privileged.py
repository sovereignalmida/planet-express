"""Console phase 0 spike under the REAL launcher (throwaway; see docs/designs/v4-console-p0-spike.md). Root-only, disposable VM only.

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
from test_launcher_privileged import ENABLED, SESSIONS, UIDS, WS, host  # noqa: F401  (host is the module fixture)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "spikes"))
pytestmark = [pytest.mark.skipif(not ENABLED, reason="root-only spike: run scripts/run_privileged_tests.sh on the disposable VM")]

CLAUDE = os.environ.get("PE_P0_CLAUDE", "/usr/local/bin/claude")


@pytest.fixture(scope="module")
def standin():
    if not os.path.exists(CLAUDE):
        pytest.skip("no claude CLI on this VM")
    import console_p0_e1_e2 as base
    import console_p0_e3_e4_e6 as e
    srv, _ = e.server_with(lambda last, data: [(0.5, f"chunk{i} ") for i in range(12)] if "SLOW" in last else None)
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
