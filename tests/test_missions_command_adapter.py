"""CommandRuntime: argv child inside a containment, brief in, bounded/redacted output, JSON result out."""

import json
import os
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import missions_helpers
import pytest

from planet_express.missions.adapters.command import (
    AgentResultError,
    CommandRuntime,
    EchoRuntime,
)
from planet_express.missions.containment import ContainedSession, FakeContainment
from planet_express.missions.runtime import AgentResult, RunContext

TOKEN = "T0ken" + "x" * 38
SECRET = "hunter2-planted-secret"

ECHO = r"""
import json, os, sys
brief = open("BRIEF.md").read()
tok = os.read(int(os.environ["PE_SESSION_TOKEN_FD"]), 200).decode()
open("seen.json", "w").write(json.dumps({"tok": tok, "env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd()}))
print("brief has", len(brief), "chars")
print("API_TOKEN=" + '__SECRET__')
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "ok":
    json.dump({"diagnosis": "disk full", "evidence": ["df shows 100%"], "suggested_actions": ["prune"],
               "confidence": "high"}, open("result.json", "w"))
elif mode == "malformed":
    open("result.json", "w").write("{not json")
elif mode == "extra":
    json.dump({"diagnosis": "x", "confidence": "low", "run_command": "rm -rf /"}, open("result.json", "w"))
elif mode == "oversize":
    json.dump({"diagnosis": "x" * 400000, "confidence": "low"}, open("result.json", "w"))
elif mode == "symlink":
    os.symlink("/etc/passwd", "result.json")
elif mode == "flood":
    sys.stdout.write("A" * 3000000); sys.stdout.flush()
    json.dump({"diagnosis": "ok", "confidence": "medium"}, open("result.json", "w"))
elif mode == "hang":
    import time; time.sleep(60)
""".replace("__SECRET__", SECRET)


class ProcContainment:
    """Minimal SessionContainment double backed by one real subprocess (no cgroups)."""

    uid_range = (61000, 61998)

    def __init__(self):
        self.procs = {}
        self.outs = {}
        self.spawned = []

    def cgroup_path(self, session_id):
        return f"/fake/{session_id}"

    def launch(self, session_id, spec):
        """What the helper does for the child: the token on a pipe, nothing else inherited. (The real helper puts
        it at fd 3; a test double cannot dup2 safely between fork and exec, so it passes the fd it has and says
        which in the env the child reads.)"""
        cs = ContainedSession(session_id, "local_cgroup", self.cgroup_path(session_id), spec.uid)
        r, w = os.pipe()
        os.set_blocking(r, False)
        token_r, token_w = os.pipe()
        os.write(token_w, spec.token.encode())
        os.close(token_w)
        self.spawned.append((list(spec.argv), dict(spec.env), spec.cwd, spec.token))
        env = {**spec.env, "PE_SESSION_TOKEN_FD": str(token_r)}
        p = subprocess.Popen(list(spec.argv), env=env, cwd=spec.cwd, pass_fds=(token_r,),
                             stdin=subprocess.DEVNULL, stdout=w, stderr=subprocess.STDOUT, start_new_session=True)
        os.close(w)
        os.close(token_r)
        self.procs[session_id] = p
        self.outs[session_id] = r
        return cs

    def pids(self, cs):
        p = self.procs[cs.session_id]
        return [] if p.poll() is not None else [p.pid]

    def freeze(self, cs): pass
    def thaw(self, cs): pass

    def kill(self, cs):
        self.procs[cs.session_id].kill()
        self.procs[cs.session_id].wait()

    def is_empty(self, cs):
        return self.procs[cs.session_id].poll() is not None

    def cleanup(self, cs): pass

    def output_fd(self, cs):
        return self.outs[cs.session_id]

    def release_output(self, cs):
        fd = self.outs.pop(cs.session_id, None)
        if fd is not None:
            os.close(fd)


@pytest.fixture
def parts(tmp_path):
    script = tmp_path / "echo_agent.py"
    script.write_text(ECHO)
    cont = ProcContainment()
    cont.tmp = tmp_path

    def build(mode="ok", **kw):
        opts = dict(argv=[sys.executable, str(script), mode],
                    worker_socket="/run/test/worker.sock", max_output_bytes=64 * 1024,
                    environ={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-should-not-leak",
                             "KEEP_ME": "yes"})
        opts.update(kw)
        return CommandRuntime(cont, **opts)

    return SimpleNamespace(build=build, cont=cont, tmp=tmp_path)


def output_text(updates):
    return "".join(str(e.body.get("output", "")) for u in updates for e in u.events)


def start(rt, cont, mission_id="m1", brief="# Brief\nfix the thing\n"):
    """Does what the Supervisor does around `start()`: a token pipe, a `spawn` closure over the containment."""
    sid = "s-" + mission_id
    cs = ContainedSession(sid, "local_cgroup", cont.cgroup_path(sid), 61000)
    workspace = cont.tmp / "ws" / (mission_id if mission_id.isalnum() else "bad")
    workspace.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, TOKEN.encode())
    os.close(write_fd)

    def spawn(argv, env=None, cwd=None, extra_fds=()):
        from planet_express.missions.launcher.spec import LaunchSpec
        spec = LaunchSpec(uid=cs.session_uid, cwd=cwd or str(workspace), argv=tuple(argv), token=TOKEN,
                          env={**(env or {}), "PE_SESSION_TOKEN_FD": "3"})        # the Supervisor's fixed fd map
        cont.launch(sid, spec)
        pids = cont.pids(cs)
        return pids[0] if pids else 1

    context = RunContext(brief=brief, session=cs, token_fd=read_fd, env={}, cwd=str(workspace), spawn=spawn)
    try:
        handle = rt.start(SimpleNamespace(id=mission_id), context, None)
    finally:
        os.close(read_fd)           # the Supervisor closes its copy once start() returns
    return cs, handle


def run_to_exit(rt, handle, cont, cs, timeout=15):
    updates = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        updates.append(rt.status(handle))
        if cont.is_empty(cs):
            updates.append(rt.status(handle))  # final drain
            return updates
        time.sleep(0.02)
    raise AssertionError("child did not exit")


def test_echo_child_result_is_parsed_and_validated(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    run_to_exit(rt, h, parts.cont, cs)
    res = rt.collect(h)
    assert isinstance(res, AgentResult)
    assert res.diagnosis == "disk full" and res.confidence == "high" and res.suggested_actions == ["prune"]


def test_workspace_holds_the_brief_and_is_the_cwd(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont, brief="BRIEF-BODY")
    run_to_exit(rt, h, parts.cont, cs)
    ws = parts.tmp / "ws" / "m1"
    assert (ws / "BRIEF.md").read_text() == "BRIEF-BODY"
    assert stat.S_IMODE((ws / "BRIEF.md").stat().st_mode) == 0o400
    seen = json.loads((ws / "seen.json").read_text())
    assert seen["cwd"] == str(ws)
    assert parts.cont.spawned[0][2] == str(ws)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".", "..", "a\0b", "x" * 200])
def test_mission_id_cannot_escape_the_workspace_root(parts, bad):
    rt = parts.build()
    with pytest.raises(ValueError):
        start(rt, parts.cont, mission_id=bad)


def test_token_is_on_an_inherited_fd_only(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    run_to_exit(rt, h, parts.cont, cs)
    argv, env, cwd, delivered = parts.cont.spawned[0]
    assert TOKEN not in " ".join(argv) and TOKEN not in json.dumps(env)
    assert env["PE_SESSION_TOKEN_FD"] == "3" and TOKEN not in env.values()     # the launcher's fixed fd map
    assert delivered == TOKEN                                                  # handed over in the spec, not the env
    seen = json.loads((parts.tmp / "ws" / "m1" / "seen.json").read_text())
    assert seen["tok"] == TOKEN  # the child, and only the child, got it
    assert TOKEN not in json.dumps(seen["env"]) and TOKEN not in json.dumps(seen["argv"])
    assert TOKEN not in (parts.tmp / "ws" / "m1" / "BRIEF.md").read_text()


def test_env_is_minimal_plus_socket_and_fd_and_allowlisted_names(parts):
    rt = parts.build(env_allow=["KEEP_ME", "NOT_SET_HERE"])
    cs, h = start(rt, parts.cont)
    run_to_exit(rt, h, parts.cont, cs)
    env = parts.cont.spawned[0][1]
    assert env["PE_WORKER_SOCKET"] == "/run/test/worker.sock"
    assert env["KEEP_ME"] == "yes" and "NOT_SET_HERE" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == str(parts.tmp / "ws" / "m1")
    assert set(env) <= {"PATH", "LANG", "HOME", "PE_WORKER_SOCKET", "PE_SESSION_TOKEN_FD", "KEEP_ME"}


def test_pe_prefixed_or_secret_looking_env_allow_names_are_refused(parts):
    for name in ("PE_WORKER_SOCKET", "OPENAI_API_KEY", "DB_PASSWORD", "lower", "A B", ""):
        with pytest.raises(ValueError):
            parts.build(env_allow=[name])


def test_output_is_redacted_and_bounded(parts):
    rt = parts.build("flood", max_output_bytes=20000)
    cs, h = start(rt, parts.cont)
    updates = run_to_exit(rt, h, parts.cont, cs)
    text = output_text(updates)
    assert SECRET not in text
    assert "[REDACTED]" in text
    assert len(text.encode()) <= 20000 + 200          # cap plus the one truncation notice
    assert "truncated" in text
    assert rt.collect(h).diagnosis == "ok"             # the child was drained, never blocked


def test_planted_secret_in_normal_output_is_redacted(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    text = output_text(run_to_exit(rt, h, parts.cont, cs))
    assert "brief has" in text and SECRET not in text


@pytest.mark.parametrize("mode", ["malformed", "extra", "oversize", "symlink"])
def test_bad_results_are_rejected(parts, mode):
    rt = parts.build(mode)
    cs, h = start(rt, parts.cont)
    run_to_exit(rt, h, parts.cont, cs)
    with pytest.raises(AgentResultError):
        rt.collect(h)


def test_missing_result_is_an_error(parts):
    rt = parts.build("none")
    cs, h = start(rt, parts.cont)
    run_to_exit(rt, h, parts.cont, cs)
    with pytest.raises(AgentResultError):
        rt.collect(h)


def test_collect_refuses_while_the_session_is_not_provably_empty(parts):
    rt = parts.build("hang")
    cs, h = start(rt, parts.cont)
    try:
        with pytest.raises(RuntimeError):
            rt.collect(h)
    finally:
        rt.cancel(h)


def test_cancel_kills_through_the_containment_and_releases_output(parts):
    rt = parts.build("hang")
    cs, h = start(rt, parts.cont)
    rt.cancel(h)
    assert parts.cont.is_empty(cs)
    assert cs.session_id not in parts.cont.outs


def test_runtime_reports_not_running_after_exit(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    updates = run_to_exit(rt, h, parts.cont, cs)
    assert updates[-1].state in ("finished", "failed")


def test_echo_runtime_is_a_stub_compatible_adapter():
    rt = EchoRuntime()
    ctx = RunContext(brief="hello", session=None, token_fd=-1, env={}, cwd="/nonexistent", spawn=None)
    h = rt.start(SimpleNamespace(id="m9"), ctx, None)
    assert rt.status(h).state == "finished"
    res = rt.collect(h)
    assert isinstance(res, AgentResult) and "hello" in res.diagnosis
    rt.cancel(h)


def test_redaction_expansion_cannot_exceed_the_output_ceiling(parts, tmp_path):
    script = tmp_path / "spam.py"
    script.write_text("import sys\nsys.stdout.write('TOKEN=x\\n' * 20000)\n")
    rt = parts.build(argv=[sys.executable, str(script)], max_output_bytes=10000)
    cs, h = start(rt, parts.cont)
    text = output_text(run_to_exit(rt, h, parts.cont, cs))
    assert len(text.encode()) <= 10000 + len(TRUNC_NOTE) + 2
    assert "truncated" in text


TRUNC_NOTE = "[output truncated]"


def test_runtime_without_output_streaming_still_runs_and_reports_state(tmp_path):
    """FakeContainment has no pipe until a test gives it one: the adapter degrades to no streaming."""
    cont = FakeContainment()
    cont.tmp = tmp_path
    rt = CommandRuntime(cont, argv=["/bin/true"], worker_socket="/run/test/worker.sock")
    cs, h = start(rt, cont)
    upd = rt.status(h)
    assert upd.state == "running" and upd.events == []
    cont.crash(cs.session_id)
    assert rt.status(h).state == "failed"          # empty and no result file: a crash, not a result
    rt.cancel(h)


def test_fake_containment_output_is_streamed_redacted_and_released(tmp_path):
    cont = FakeContainment()
    cont.tmp = tmp_path
    rt = CommandRuntime(cont, argv=["/bin/true"], worker_socket="/run/test/worker.sock")
    cs, h = start(rt, cont)
    cont.write_output(cs.session_id, ("hello\nAPI_TOKEN=" + SECRET + "\n").encode())
    text = output_text([rt.status(h)])
    assert "hello" in text and SECRET not in text
    rt.cancel(h)
    assert cont.output_fd(cs) is None               # released


def test_status_reports_finished_when_the_child_left_a_result(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    updates = run_to_exit(rt, h, parts.cont, cs)
    assert updates[-1].state == "finished"


def test_status_reports_failed_when_the_child_died_without_a_result(parts):
    rt = parts.build("none")
    cs, h = start(rt, parts.cont)
    assert run_to_exit(rt, h, parts.cont, cs)[-1].state == "failed"


def test_events_are_bounded_note_events_only(parts):
    rt = parts.build("flood", max_output_bytes=40000)
    cs, h = start(rt, parts.cont)
    for u in run_to_exit(rt, h, parts.cont, cs):
        assert len(u.events) <= 256 and all(e.kind == "note" for e in u.events)
        assert all(len(str(e.body["output"])) <= 8192 for e in u.events)
