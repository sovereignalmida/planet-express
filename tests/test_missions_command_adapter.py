"""CommandRuntime: argv child inside a containment, brief in, bounded/redacted output, JSON result out."""

import json
import os
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import s3b_stubs  # noqa: F401
from planet_express.missions.adapters.command import (
    AgentResultError,
    CommandRuntime,
    EchoRuntime,
    StartContext,
)
from planet_express.missions.containment import ContainedSession
from planet_express.missions.runtime import AgentResult

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
               "confidence": 0.8}, open("result.json", "w"))
elif mode == "malformed":
    open("result.json", "w").write("{not json")
elif mode == "extra":
    json.dump({"diagnosis": "x", "confidence": 0.1, "run_command": "rm -rf /"}, open("result.json", "w"))
elif mode == "oversize":
    json.dump({"diagnosis": "x" * 400000, "confidence": 0.1}, open("result.json", "w"))
elif mode == "symlink":
    os.symlink("/etc/passwd", "result.json")
elif mode == "flood":
    sys.stdout.write("A" * 3000000); sys.stdout.flush()
    json.dump({"diagnosis": "ok", "confidence": 0.5}, open("result.json", "w"))
elif mode == "hang":
    import time; time.sleep(60)
""".replace("__SECRET__", SECRET)


class ProcContainment:
    """Minimal SessionContainment double backed by one real subprocess (no cgroups)."""

    def __init__(self):
        self.procs = {}
        self.outs = {}
        self.spawned = []

    def create(self, session_id):
        return ContainedSession(session_id, "fake", None, None)

    def spawn(self, cs, argv, env, cwd, pass_fds=()):
        r, w = os.pipe()
        os.set_blocking(r, False)
        self.spawned.append((list(argv), dict(env), cwd, tuple(pass_fds)))
        p = subprocess.Popen(argv, env=env, cwd=cwd, pass_fds=pass_fds, stdin=subprocess.DEVNULL,
                             stdout=w, stderr=subprocess.STDOUT, start_new_session=True)
        os.close(w)
        self.procs[cs.session_id] = p
        self.outs[cs.session_id] = r
        return p.pid

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

    def build(mode="ok", **kw):
        opts = dict(argv=[sys.executable, str(script), mode], workspace_root=tmp_path / "ws",
                    worker_socket="/run/test/worker.sock", max_output_bytes=64 * 1024,
                    environ={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-should-not-leak",
                             "KEEP_ME": "yes"})
        opts.update(kw)
        (tmp_path / "ws").mkdir(exist_ok=True)
        return CommandRuntime(cont, **opts)

    return SimpleNamespace(build=build, cont=cont, tmp=tmp_path)


def start(rt, cont, mission_id="m1", brief="# Brief\nfix the thing\n"):
    cs = cont.create("s-" + mission_id)
    handle = rt.start(SimpleNamespace(id=mission_id), StartContext(brief=brief, session=cs, token=TOKEN), None)
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
    assert res.diagnosis == "disk full" and res.confidence == 0.8 and res.suggested_actions == ["prune"]


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
    argv, env, cwd, pass_fds = parts.cont.spawned[0]
    assert TOKEN not in " ".join(argv) and TOKEN not in json.dumps(env)
    fd = int(env["PE_SESSION_TOKEN_FD"])
    assert pass_fds == (fd,)
    with pytest.raises(OSError):
        os.fstat(fd)  # the parent closed its copy after spawn
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
    text = "".join(u.text for u in updates)
    assert SECRET not in text
    assert "[REDACTED]" in text
    assert len(text.encode()) <= 20000 + 200          # cap plus the one truncation notice
    assert "truncated" in text
    assert rt.collect(h).diagnosis == "ok"             # the child was drained, never blocked


def test_planted_secret_in_normal_output_is_redacted(parts):
    rt = parts.build()
    cs, h = start(rt, parts.cont)
    text = "".join(u.text for u in run_to_exit(rt, h, parts.cont, cs))
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
    assert updates[-1].running is False


def test_echo_runtime_is_a_stub_compatible_adapter():
    rt = EchoRuntime()
    h = rt.start(SimpleNamespace(id="m9"), StartContext(brief="hello", session=None, token="x"), None)
    assert rt.status(h).running is False
    res = rt.collect(h)
    assert isinstance(res, AgentResult) and "hello" in res.diagnosis
    rt.cancel(h)


def test_redaction_expansion_cannot_exceed_the_output_ceiling(parts, tmp_path):
    script = tmp_path / "spam.py"
    script.write_text("import sys\nsys.stdout.write('TOKEN=x\\n' * 20000)\n")
    rt = parts.build(argv=[sys.executable, str(script)], max_output_bytes=10000)
    cs, h = start(rt, parts.cont)
    text = "".join(u.text for u in run_to_exit(rt, h, parts.cont, cs))
    assert len(text.encode()) <= 10000 + len(TRUNC_NOTE) + 2
    assert "truncated" in text


TRUNC_NOTE = "[output truncated]"
