"""A stand-in for Core, for the privileged launcher tests (tests/test_launcher_privileged.py).

`CoreOps` performs the operations Core performs (launch through a strategy, freeze, kill, ask whether a cgroup is
empty, clean up). `Core.direct()` runs them in this process (MOS: Core is root). `Core.as_user(uid)` runs the SAME
operations in a child started as an unprivileged uid with no supplementary groups, talking JSON lines over its
stdin/stdout (Ubuntu: unprivileged Core, `sudo -n pe-launch`). The test process itself stays root so it can inspect
/proc and the cgroup files the way an auditor would.

NOT to be run outside a disposable container or VM: see scripts/run_privileged_tests.sh.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planet_express.missions.containment import ContainedSession  # noqa: E402
from planet_express.missions.containment_cgroup import CgroupContainment  # noqa: E402
from planet_express.missions.launcher.spec import LaunchSpec  # noqa: E402
from planet_express.missions.launcher.strategies import DirectLauncher, SudoLauncher  # noqa: E402


def cs_of(d):
    return ContainedSession(d["session_id"], d["kind"], d["cgroup_path"], d["session_uid"])


class CoreOps:
    def __init__(self, strategy, cgroup_root, sock_root, uid_range):
        launcher = DirectLauncher() if strategy == "direct" else SudoLauncher()
        self.c = CgroupContainment(cgroup_root, launcher=launcher, uid_range=tuple(uid_range), sock_root=sock_root,
                                   sleep=__import__("time").sleep)
        self.sessions = {}
        self.listeners = {}                      # the per-session worker sockets Core binds before every launch

    def handle(self, op, **a):
        return getattr(self, "op_" + op)(**a)

    def op_whoami(self):
        return {"euid": os.geteuid(), "egid": os.getegid(), "groups": os.getgroups()}

    def op_launch(self, session_id, uid, cwd, argv, env=None, token="T" * 48, sock_dir=False, make_cwd=True, state_dir=None):
        if make_cwd and not os.path.isdir(cwd):
            os.mkdir(cwd, 0o700)
        if sock_dir:                             # what the Supervisor's SessionSockets does: BEFORE the launch
            import socket
            directory = os.path.join(self.c.sock_root, session_id)
            os.mkdir(directory, 0o700)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(os.path.join(directory, "worker.sock"))
            listener.listen(4)
            self.listeners[session_id] = listener
        if state_dir is not None:                # what a conversation does: Core makes it beside the workspaces, once
            path = os.path.join(os.path.dirname(cwd), state_dir)
            if not os.path.isdir(path):
                os.mkdir(path, 0o700)
        spec = LaunchSpec(uid=uid, cwd=cwd, argv=tuple(argv), env=env or {"PATH": "/usr/bin:/bin"}, token=token,
                          sock_dir=sock_dir, state_dir=state_dir)
        cs = self.c.launch(session_id, spec)
        self.sessions[session_id] = cs
        return {"session_id": cs.session_id, "kind": cs.kind, "cgroup_path": cs.cgroup_path,
                "session_uid": cs.session_uid, "pids": self.c.pids(cs)}

    def op_first_line(self, session_id, timeout=15.0):
        import select
        import time
        fd = self.c.output_fd(self.sessions[session_id])
        data, deadline = b"", time.time() + timeout
        while time.time() < deadline:                     # one byte at a time: nothing after the newline is consumed
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 1)
                except BlockingIOError:
                    continue
                if not chunk or chunk == b"\n":
                    break
                data += chunk
        return {"line": data.decode(errors="replace")}

    def op_pids(self, session_id):
        return {"pids": self.c.pids(self.sessions[session_id])}

    def op_freeze(self, session_id):
        self.c.freeze(self.sessions[session_id])
        return {"events": Path(self.sessions[session_id].cgroup_path, "cgroup.events").read_text()}

    def op_thaw(self, session_id):
        self.c.thaw(self.sessions[session_id])
        return {"events": Path(self.sessions[session_id].cgroup_path, "cgroup.events").read_text()}

    def op_kill(self, session_id):
        self.c.kill(self.sessions[session_id])
        return {"empty": self.c.is_empty(self.sessions[session_id])}

    def op_empty(self, session_id):
        return {"empty": self.c.is_empty(self.sessions[session_id])}

    def op_cleanup(self, session_id):
        listener = self.listeners.pop(session_id, None)
        if listener is not None:
            listener.close()
        cs = self.sessions.pop(session_id)
        self.c.cleanup(cs)
        return {"gone": not os.path.exists(cs.cgroup_path)}

    def op_uid_in_use(self, uid):
        return {"in_use": self.c.uid_in_use(uid)}

    def op_accept_on(self, session_id):
        """Core accepts one connection on the session's socket and returns the peer's uid (SO_PEERCRED)."""
        import socket
        import struct
        listener = self.listeners[session_id]
        listener.settimeout(15)
        conn, _ = listener.accept()
        pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        conn.close()
        return {"uid": uid, "gid": gid}

    def op_read(self, path):
        return {"text": Path(path).read_text()}

    def op_clear_workspace(self, session_id, uid, workspace_dir):
        """One step of the production `Supervisor._clear_workspace`, against this Core's real containment."""
        import time
        from pathlib import Path as P

        from planet_express.missions.supervisor import Supervisor
        if getattr(self, "sup", None) is None:
            self.sup = Supervisor.__new__(Supervisor)            # only what `_clear_workspace` reads; no store, no runtime
            self.sup.workspace_dir, self.sup.containment, self.sup.clock = P(workspace_dir), self.c, time.time
            self.sup._cleaners, self.sup._cleanup_retry_at, self.sup._shutting_down = {}, {}, False
            self.sup._lifecycle = __import__("threading").RLock()
            self.sup._cleanup_leftovers_reaped = set()
            self.sup.preflight_fn = None                          # the preflight is Supervisor.launcher_ready's own concern
        done = self.sup._clear_workspace(session_id, uid)
        return {"done": done, "workspace_exists": os.path.exists(P(workspace_dir) / session_id),
                "cleaning": sorted(self.sup._cleaners)}

    def op_preflight(self, workspace_root, helper, conf, python, strategy):
        from planet_express.missions.launcher.preflight import run_preflight
        low, high = self.c.uid_range[0], self.c.uid_range[1] + 1
        report = run_preflight(self.c, strategy=strategy, workspace_root=workspace_root, sock_root=self.c.sock_root,
                               probe_uid=high, helper=helper, conf=conf, python=python, uid_range=(low, high),
                               sudo=strategy == "sudo")
        return report.to_dict()


class Core:
    """The proxy: same `call()` for both strategies."""

    def __init__(self, ops=None, proc=None):
        self.ops, self.proc = ops, proc

    @classmethod
    def direct(cls, **cfg):
        return cls(ops=CoreOps("direct", **cfg))

    @classmethod
    def as_user(cls, uid, **cfg):
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "CASA_CONFIG": str(ROOT / "config.example.yaml"),
               # hostile values Core might carry: sudo (env_reset) must keep every one of them away from the helper
               "LD_PRELOAD": "/nonexistent/evil.so", "LD_LIBRARY_PATH": "/nonexistent", "PYTHONSTARTUP": "/nonexistent"}
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), json.dumps(cfg), "strategy=sudo"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, user=uid, group=uid, extra_groups=[],
                                env=env, text=True, bufsize=1, cwd="/")
        return cls(proc=proc)

    def call(self, op, **args):
        if self.ops is not None:
            return self.ops.handle(op, **args)
        self.proc.stdin.write(json.dumps({"op": op, "args": args}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def serve():
    cfg = json.loads(sys.argv[1])
    ops = CoreOps("sudo", **cfg)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            reply = {"result": ops.handle(request["op"], **request["args"])}
        except Exception as exc:       # noqa: BLE001 -- relayed to the test, which decides
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()
