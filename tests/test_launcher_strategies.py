"""The launcher strategies against a stand-in helper that speaks the framed status protocol (no privilege)."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from planet_express.missions.launcher.config import HELPER_PATH
from planet_express.missions.launcher.spec import LaunchSpec
from planet_express.missions.launcher.strategies import (
    DirectLauncher,
    HelperLauncher,
    LaunchError,
    SudoLauncher,
    parse_status,
    read_status,
)

SPEC = LaunchSpec(uid=61001, cwd="/w/S-1", argv=("/bin/true",), env={"A": "b"}, token="T" * 40)


def fake_helper(tmp_path, body):
    path = tmp_path / "fake_helper.py"
    path.write_text("import json, os, sys\n" + textwrap.dedent(body))
    return [sys.executable, str(path)]


GOOD = """
spec = json.loads(sys.stdin.read())
status = {"ok": True, "session_id": spec["session_id"], "uid": spec["worker_uid"], "gid": spec["worker_uid"],
          "cgroup": "/sys/fs/cgroup/pe/" + spec["session_id"]}
sys.stdout.write("PE-LAUNCH-STATUS " + json.dumps(status) + "\\n" + "child output line\\n")
sys.stdout.flush()
"""


def test_a_good_launch_returns_the_status_and_leaves_later_output_in_the_pipe(tmp_path):
    launched = HelperLauncher(fake_helper(tmp_path, GOOD)).start("S-1", SPEC)
    assert launched.status["cgroup"] == "/sys/fs/cgroup/pe/S-1" and launched.status["uid"] == 61001
    launched.proc.wait(timeout=10)
    assert os.read(launched.output_fd, 100) == b"child output line\n"          # the status line was stripped, nothing else
    launched.proc.stdout.close()


def test_the_helper_gets_exactly_the_spec_on_stdin_and_a_minimal_environment(tmp_path):
    body = """
    raw = sys.stdin.read()
    seen = {"spec": json.loads(raw), "env": dict(os.environ), "cwd": os.getcwd()}
    open(sys.argv[1], "w").write(json.dumps(seen))
    spec = seen["spec"]
    print("PE-LAUNCH-STATUS " + json.dumps({"ok": True, "session_id": spec["session_id"], "uid": spec["worker_uid"],
          "gid": spec["worker_uid"], "cgroup": "/c/" + spec["session_id"]}), flush=True)
    """
    out = tmp_path / "seen.json"
    hostile = {"PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so", "PYTHONHOME": "/evil"}
    saved = {k: os.environ.get(k) for k in hostile}
    os.environ.update(hostile)
    try:
        HelperLauncher(fake_helper(tmp_path, body) + [str(out)]).start("S-1", SPEC).proc.stdout.close()
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
    seen = json.loads(out.read_text())
    assert seen["env"] == {"PATH": "/usr/bin:/bin"} or set(seen["env"]) <= {"PATH", "LC_CTYPE", "PWD", "SHLVL", "_"}
    assert not {"PYTHONPATH", "LD_PRELOAD", "PYTHONHOME"} & set(seen["env"])
    assert seen["spec"]["worker_uid"] == 61001 and seen["spec"]["session_id"] == "S-1"
    assert seen["spec"]["token"] == "T" * 40 and seen["cwd"] == "/"


def test_the_helper_is_started_without_preexec_fn_and_through_a_plain_popen(tmp_path):
    seen = {}

    class Stub:
        def __init__(self, argv, **kw):
            seen.update(kw, argv=argv)
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        HelperLauncher(["/h"], popen=Stub).start("S-1", SPEC)
    assert "preexec_fn" not in seen and seen["close_fds"] is True and seen["env"] == {"PATH": "/usr/bin:/bin"}
    assert seen["stdout"] == subprocess.PIPE and seen["stderr"] == subprocess.STDOUT


def test_sudo_and_direct_run_the_same_helper_and_sudo_passes_no_arguments():
    assert SudoLauncher().argv == ["/usr/bin/sudo", "-n", HELPER_PATH]
    assert DirectLauncher(euid=0).argv == [HELPER_PATH]


def test_direct_launcher_refuses_a_core_that_is_not_root():
    with pytest.raises(LaunchError):
        DirectLauncher(euid=1000)


@pytest.mark.parametrize("body, needle", [
    ('print("PE-LAUNCH-STATUS " + json.dumps({"ok": False, "error": "uid"}), flush=True)', "refused: uid"),
    ('print("PE-LAUNCH-STATUS " + json.dumps({"ok": False, "error": "x" * 500}), flush=True)', "unspecified"),
    ('print("hello")', "not a status line"),
    ('print("PE-LAUNCH-STATUS {nope")', "not JSON"),
    ('print("PE-LAUNCH-STATUS [1]")', "malformed"),
    ('print("PE-LAUNCH-STATUS " + json.dumps({"ok": 1}))', "malformed"),
    ('', "without a status line"),
    ('sys.stdout.write("x" * 6000); sys.stdout.flush()', "too long"),
])
def test_a_helper_that_refuses_or_misbehaves_is_a_launch_error(tmp_path, body, needle):
    with pytest.raises(LaunchError, match=needle):
        HelperLauncher(fake_helper(tmp_path, "sys.stdin.read()\n" + body)).start("S-1", SPEC)


@pytest.mark.parametrize("change", [{"session_id": "S-other"}, {"uid": 61002}, {"gid": 0}, {"cgroup": "relative"},
                                    {"cgroup": None}])
def test_a_status_that_does_not_describe_the_request_is_refused(change):
    status = {"ok": True, "session_id": "S-1", "uid": 61001, "gid": 61001, "cgroup": "/sys/fs/cgroup/pe/S-1"}
    status.update(change)
    with pytest.raises(LaunchError, match="does not match"):
        parse_status("PE-LAUNCH-STATUS " + json.dumps(status), "S-1", SPEC)


def test_a_silent_helper_times_out_and_is_abandoned(tmp_path):
    launcher = HelperLauncher(fake_helper(tmp_path, "sys.stdin.read()\nimport time\ntime.sleep(30)\n"), timeout_s=0.3)
    with pytest.raises(LaunchError, match="in time"):
        launcher.start("S-1", SPEC)


def test_an_unacceptable_spec_never_reaches_the_helper(tmp_path):
    bad = LaunchSpec(uid=0, cwd="/w/x", argv=("/bin/true",), token="T" * 40)
    with pytest.raises(LaunchError, match="not acceptable") as info:
        HelperLauncher(["/must-not-run"]).start("S-1", bad)
    assert "T" * 40 not in str(info.value)


def test_read_status_reads_one_byte_at_a_time_so_nothing_after_the_line_is_consumed():
    read_end, write_end = os.pipe()
    os.write(write_end, b"first\nsecond\n")
    assert read_status(read_end, 1.0) == "first"
    assert os.read(read_end, 100) == b"second\n"
    os.close(read_end)
    os.close(write_end)
