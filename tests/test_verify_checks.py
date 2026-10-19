"""The eight verification checks (slice 8, contract 10): pass, fail, timeout, malformed params, and the
"green but broken" cases that MUST fail. Docker is a scripted fake; the HTTP check also runs against a real local server."""
import hashlib
import http.server
import json
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify_fakes import FakeRun, check_env

from planet_express.missions.models import VerificationCheck
from planet_express.missions.verify.checks import (
    PARAM_MODELS,
    HttpResponse,
    default_http,
    regex_search,
    run_check,
)


def chk(kind, **params):
    return VerificationCheck(id=kind, check=kind, params=params)


def started(seconds_ago=3600):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def inspect_json(*, status="running", health=None, healthcheck=False, restarting=False, restarts=0, ago=3600, **state):
    doc = {"State": {"Status": status, "Running": status == "running", "Restarting": restarting, "Paused": False,
                     "Dead": False, "StartedAt": started(ago), **state},
           "RestartCount": restarts, "Config": {"Healthcheck": {"Test": ["CMD", "true"]} if healthcheck else None}}
    if health:
        doc["State"]["Health"] = {"Status": health}
    return 0, json.dumps([doc]), ""


# ── params ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("kind", "params"), [
    ("http", {"url": "ftp://x/"}), ("http", {"url": "http://u:p@x/"}), ("http", {"url": "http://x/", "resolve_to": "name"}),
    ("http", {"url": "http://x/", "consecutive": 0}), ("http", {"url": "http://x/", "timeout_s": 0}),
    ("http", {"url": "http://x/", "timeout_s": 61}), ("http", {"url": "http://x/", "retries": 4}),
    ("exec_probe", {"container": "c", "argv": []}), ("exec_probe", {"container": "-x", "argv": ["ls"]}),
    ("exec_probe", {"container": "c", "argv": ["--help"]}), ("exec_probe", {"container": "c", "argv": ["a\0b"]}),
    ("exec_probe", {"container": "c;rm", "argv": ["ls"]}), ("container_state", {"container": "a b"}),
    ("mount_readable", {"path": "relative"}), ("mount_readable", {"path": "/a/../b"}),
    ("project_complete", {"stack": "../x"}), ("log_absent", {"container": "c", "pattern": ""}),
    ("log_absent", {"container": "c", "pattern": "x", "window_s": 0}),
    ("log_absent", {"container": "c", "pattern": "(", "regex": True}),
    ("log_absent", {"container": "c", "pattern": "a" * 129, "regex": True}),
    ("file_state", {"path": "/x"}), ("file_state", {"path": "/x", "sha256": "zz"}),
    ("file_state", {"path": "/x", "mode": "999"}), ("query", {"tool": "Bad Name"}),
    ("query", {"tool": "app.health_get", "arguments_json": "[1]"}),
    ("query", {"tool": "app.health_get", "arguments_json": '{"a": {"b": 1}}'}),
])
def test_malformed_params_are_refused_by_the_model(kind, params):
    with pytest.raises(ValueError):
        PARAM_MODELS[kind].model_validate(params)


@pytest.mark.parametrize("kind", sorted(PARAM_MODELS))
def test_run_check_with_bad_params_fails_and_never_raises(kind):
    r = run_check(chk(kind, bogus=1), check_env())
    assert r.ok is False and r.detail == "bad params" and r.attempts == 0


# ── http ────────────────────────────────────────────────────────────────────
def test_http_passes_n_consecutive_times_and_fails_on_the_first_miss():
    seen = []

    def http(req):
        seen.append(req)
        return HttpResponse(200, b"ok")

    r = run_check(chk("http", url="http://app.example:8080/h?x=1", consecutive=3, interval_s=0), check_env(http=http))
    assert r.ok and len(seen) == 3
    assert (seen[0].host, seen[0].port, seen[0].target) == ("app.example", 8080, "/h?x=1")
    answers = iter([200, 200, 502])
    r = run_check(chk("http", url="http://app.example/", consecutive=3, interval_s=0),
                  check_env(http=lambda req: HttpResponse(next(answers), b"")))
    assert not r.ok and "probe 3/3" in r.detail and "502" in r.detail


def test_http_route_up_but_wrong_body_fails():
    env = check_env(http=lambda req: HttpResponse(200, b"<html>Bad Gateway</html>"))
    r = run_check(chk("http", url="http://app.example/", expect_body="status: ok", consecutive=1), env)
    assert not r.ok and "body does not match" in r.detail
    assert "Bad Gateway" not in r.detail                       # the body is matched against, never copied


def test_http_connection_errors_and_timeouts_fail():
    def boom(req):
        raise ConnectionRefusedError("nope")

    assert not run_check(chk("http", url="http://x/", consecutive=1), check_env(http=boom)).ok

    def slow(req):
        time.sleep(3)
        return HttpResponse(200, b"")

    t0 = time.monotonic()
    r = run_check(chk("http", url="http://x/", consecutive=1, timeout_s=1), check_env(http=slow, backstop_grace_s=0.3))
    assert not r.ok and r.detail == "timed out" and time.monotonic() - t0 < 2.5


class _Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                                 # noqa: N802
        body = json.dumps({"host": self.headers.get("Host"), "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_http_resolve_override_connects_to_the_address_but_sends_the_real_host():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        r = run_check(chk("http", url=f"http://app.example.test:{port}/p?q=1", resolve_to="127.0.0.1", consecutive=2,
                          interval_s=0, expect_body='"host": "app.example.test:%d"' % port), check_env(http=default_http))
        assert r.ok, r.detail
        resp = default_http(SimpleNamespace(scheme="http", host="app.example.test", port=port, target="/p",
                                            resolve_to="127.0.0.1", timeout_s=3.0, max_body=10))
        assert resp.status == 200 and resp.truncated and len(resp.body) <= 10       # the body is bounded
    finally:
        server.shutdown()


# ── exec_probe ──────────────────────────────────────────────────────────────
def test_exec_probe_is_unavailable_without_an_allowlist_and_refuses_other_binaries():
    run = FakeRun([(["docker", "exec"], (0, "ok", ""))])
    r = run_check(chk("exec_probe", container="c", argv=["/bin/true"]), check_env(run))
    assert not r.ok and "unavailable" in r.detail and not run.calls
    env = check_env(run, exec_allowlist=frozenset({"/usr/bin/curl"}))
    r = run_check(chk("exec_probe", container="c", argv=["/bin/true"]), env)
    assert not r.ok and "allowlist" in r.detail and not run.calls


def test_exec_probe_pass_fail_and_green_but_broken():
    env_run = FakeRun([(["docker", "exec", "app", "/usr/bin/curl"], (0, "pong", ""))])
    env = check_env(env_run, exec_allowlist=frozenset({"/usr/bin/curl"}))
    assert run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl", "-fs", "localhost/ping"],
                         expect_contains="pong"), env).ok
    assert env_run.calls[0] == ["docker", "exec", "app", "/usr/bin/curl", "-fs", "localhost/ping"]
    r = run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl", "x"], expect_contains="other"), env)
    assert not r.ok and "does not match" in r.detail
    failing = FakeRun([(["docker", "exec"], (22, "", "curl: (22)"))])
    assert not run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl"]),
                         check_env(failing, exec_allowlist=frozenset({"/usr/bin/curl"}))).ok


def test_exec_probe_shell_metacharacters_are_inert_arguments():
    run = FakeRun([(["docker", "exec"], (0, "", ""))])
    env = check_env(run, exec_allowlist=frozenset({"/usr/bin/curl"}))
    evil = "http://x/;touch /tmp/pwned && $(id) `id` | cat"
    assert run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl", evil]), env).ok
    assert run.calls[0] == ["docker", "exec", "app", "/usr/bin/curl", evil]       # one argv item, passed as-is
    assert all(isinstance(a, str) for a in run.calls[0])


def test_exec_probe_slow_probe_times_out_and_output_is_bounded():
    run = FakeRun([(["docker", "exec"], (0, "x", ""))])
    run.delay = 3
    env = check_env(run, exec_allowlist=frozenset({"/usr/bin/curl"}), backstop_grace_s=0.2)
    t0 = time.monotonic()
    r = run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl"], timeout_s=1), env)
    assert not r.ok and r.detail == "timed out" and time.monotonic() - t0 < 3
    assert run.calls[0] and run_check.__name__            # the runner passes a byte bound to every call
    seen = []

    def bounded(argv, timeout, max_bytes):
        seen.append((timeout, max_bytes))
        return 0, "x" * 10, "", True

    run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl"], timeout_s=7),
              check_env(bounded, exec_allowlist=frozenset({"/usr/bin/curl"}), max_output_bytes=1234))
    assert seen == [(7, 1234)]


def test_timeouts_are_capped_by_core_config():
    seen = []

    def bounded(argv, timeout, max_bytes):
        seen.append(timeout)
        return 0, "", "", False

    run_check(chk("mount_readable", path="/mnt/x", container="app", timeout_s=60), check_env(bounded, timeout_cap_s=5))
    assert seen == [5]


# ── container_state ─────────────────────────────────────────────────────────
def state_env(*responses):
    it = iter(responses)
    run = FakeRun([(["docker", "inspect"], lambda argv: next(it))])
    return check_env(run), run


def test_container_state_passes_when_running_healthy_and_stable():
    env, run = state_env(inspect_json(healthcheck=True, health="healthy"), inspect_json(healthcheck=True, health="healthy"))
    r = run_check(chk("container_state", container="app"), env)
    assert r.ok, r.detail
    assert run.calls[0] == ["docker", "inspect", "--type", "container", "app"]


@pytest.mark.parametrize("doc", [
    inspect_json(status="exited"), inspect_json(restarting=True, status="running"),
    inspect_json(healthcheck=True, health="starting"), inspect_json(healthcheck=True, health="unhealthy"),
    inspect_json(healthcheck=True), (1, "", "no such container"), (0, "not json", ""), (0, "[]", ""),
    (0, json.dumps([{"State": {"Status": "running", "Running": True, "StartedAt": "garbage"}}]), ""),
])
def test_container_state_fails_when_not_clearly_running_and_healthy(doc):
    env, _ = state_env(doc, doc)
    assert not run_check(chk("container_state", container="app"), env).ok


def test_container_state_fails_when_it_only_just_started():
    env, _ = state_env(inspect_json(ago=1), inspect_json(ago=1))
    r = run_check(chk("container_state", container="app", min_uptime_s=600), env)
    assert not r.ok and "less than 600s" in r.detail


def test_container_state_docker_healthy_alone_never_satisfies_it():
    """`healthy` on a container that is restarting or exited is not a pass."""
    for doc in (inspect_json(status="exited", healthcheck=True, health="healthy"),
                inspect_json(restarting=True, healthcheck=True, health="healthy")):
        env, _ = state_env(doc, doc)
        assert not run_check(chk("container_state", container="app"), env).ok


def test_container_state_fails_if_it_restarts_while_observed():
    env, _ = state_env(inspect_json(restarts=1), inspect_json(restarts=2))
    r = run_check(chk("container_state", container="app"), env)
    assert not r.ok and "restarted" in r.detail


def test_green_but_broken_container_healthy_but_the_in_container_probe_fails():
    healthy = inspect_json(healthcheck=True, health="healthy")
    run = FakeRun([(["docker", "inspect"], healthy), (["docker", "exec"], (7, "", "connection refused"))])
    env = check_env(run, exec_allowlist=frozenset({"/usr/bin/curl"}))
    assert run_check(chk("container_state", container="app", observe_s=0), env).ok          # the string says healthy ...
    assert not run_check(chk("exec_probe", container="app", argv=["/usr/bin/curl", "localhost"]), env).ok   # ... it is not


# ── mount_readable ──────────────────────────────────────────────────────────
def test_mount_readable_host_and_consumer(tmp_path):
    (tmp_path / "f").write_text("x")
    assert run_check(chk("mount_readable", path=str(tmp_path)), check_env()).ok
    assert run_check(chk("mount_readable", path=str(tmp_path / "f")), check_env()).ok
    assert not run_check(chk("mount_readable", path=str(tmp_path / "missing")), check_env()).ok
    run = FakeRun([(["docker", "exec", "app", "ls", "--", "/data"], (0, "a\nb", ""))])
    assert run_check(chk("mount_readable", path="/data", container="app"), check_env(run)).ok


def test_green_but_broken_mount_present_but_unreadable_by_the_consumer(tmp_path):
    # Core can see the mount point, the consumer container cannot list it (stale NFS handle, wrong uid).
    assert run_check(chk("mount_readable", path=str(tmp_path)), check_env()).ok
    run = FakeRun([(["docker", "exec"], (2, "", "ls: /data: Permission denied"))])
    r = run_check(chk("mount_readable", path="/data", container="app"), check_env(run))
    assert not r.ok and "inside the consumer" in r.detail


# ── project_complete ────────────────────────────────────────────────────────
class FakeProvider:
    compose_argv = ("docker", "compose")

    def __init__(self, project="media", chain=True):
        self.project, self.chain = project, chain

    def compose_chain(self, stack):
        return [SimpleNamespace(real_path=f"/stacks/{stack}/docker-compose.yml")] if self.chain else []

    def project_name(self, stack):
        return self.project


PS = ('{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}')


def project_env(services, rows, provider=None, ps_rc=0):
    run = FakeRun([(["docker", "compose"], (0, "\n".join(services), "")),
                   (["docker", "ps"], (ps_rc, "\n".join("\t".join(r) for r in rows), ""))])
    return check_env(run, provider=provider or FakeProvider()), run


def test_project_complete_requires_every_defined_service_to_exist_and_run():
    env, run = project_env(["web", "db"], [("web", "running", "Up 2 hours"), ("db", "running", "Up 2 hours (healthy)")])
    assert run_check(chk("project_complete", stack="media"), env).ok
    assert run.calls[1][:4] == ["docker", "ps", "-a", "--filter"] and "label=com.docker.compose.project=media" in run.calls[1]
    assert run.calls[1][-1] == PS


def test_green_but_broken_project_missing_a_service_or_one_not_running_fails():
    env, _ = project_env(["web", "db"], [("web", "running", "Up 2 hours")])
    r = run_check(chk("project_complete", stack="media"), env)
    assert not r.ok and "db: absent" in r.detail
    for row in (("db", "exited", "Exited (1) 2 minutes ago"), ("db", "restarting", "Restarting (1)"),
                ("db", "running", "Up 1 minute (unhealthy)"), ("db", "running", "Up 3 seconds (health: starting)")):
        env, _ = project_env(["web", "db"], [("web", "running", "Up"), row])
        assert not run_check(chk("project_complete", stack="media"), env).ok, row


def test_project_complete_unknown_or_unreadable_project_fails_not_passes():
    env, _ = project_env(["web"], [("web", "running", "Up")], provider=FakeProvider(project=None))
    assert not run_check(chk("project_complete", stack="media"), env).ok
    env, _ = project_env(["web"], [("web", "running", "Up")], provider=FakeProvider(chain=False))
    assert not run_check(chk("project_complete", stack="media"), env).ok
    env, _ = project_env([], [])
    assert not run_check(chk("project_complete", stack="media"), env).ok          # no services defined
    env, _ = project_env(["web"], [], ps_rc=1)
    assert not run_check(chk("project_complete", stack="media"), env).ok
    env, _ = project_env(["web"], [("web", "running")])                            # malformed listing
    assert not run_check(chk("project_complete", stack="media"), env).ok
    assert not run_check(chk("project_complete", stack="media"), check_env()).ok     # no provider at all


# ── log_absent ──────────────────────────────────────────────────────────────
def log_env(out="", err="", rc=0, truncated=False, **over):
    run = FakeRun([(["docker", "logs"], (rc, out, err, truncated))])
    return check_env(run, **over), run


def test_log_absent_literal_pass_fail_and_command():
    env, run = log_env("all good\n")
    assert run_check(chk("log_absent", container="app", pattern="FATAL", window_s=120), env).ok
    assert run.calls[0] == ["docker", "logs", "--since", "120s", "app"]
    env, _ = log_env("x\nFATAL: db down\n")
    r = run_check(chk("log_absent", container="app", pattern="FATAL"), env)
    assert not r.ok and "FATAL" not in r.detail
    env, _ = log_env("", "FATAL on stderr")
    assert not run_check(chk("log_absent", container="app", pattern="FATAL"), env).ok        # both streams are read


def test_log_absent_literal_is_not_a_regex():
    env, _ = log_env("a+b\n")
    assert not run_check(chk("log_absent", container="app", pattern="a+b"), env).ok
    env, _ = log_env("aab\n")
    assert run_check(chk("log_absent", container="app", pattern="a+b"), env).ok


def test_log_absent_unreadable_or_huge_window_fails_closed():
    env, _ = log_env(rc=1)
    assert not run_check(chk("log_absent", container="app", pattern="x"), env).ok
    env, _ = log_env("clean", truncated=True)                                   # more than the byte bound: cannot show absence
    r = run_check(chk("log_absent", container="app", pattern="x"), env)
    assert not r.ok and "absence cannot be shown" in r.detail
    seen = []

    def bounded(argv, timeout, max_bytes):
        seen.append(max_bytes)
        return 0, "", "", False

    run_check(chk("log_absent", container="app", pattern="x"), check_env(bounded, max_output_bytes=777))
    assert seen == [777]


def test_log_absent_regex_runs_in_a_killable_child():
    env, _ = log_env("request id=42 failed\n")
    assert not run_check(chk("log_absent", container="app", pattern=r"id=\d+ failed", regex=True), env).ok
    env, _ = log_env("request ok\n")
    assert run_check(chk("log_absent", container="app", pattern=r"id=\d+ failed", regex=True), env).ok


def test_catastrophic_regex_fails_closed_within_the_deadline():
    text = "a" * 60 + "b\n"
    env, _ = log_env(text, regex_deadline_s=1.0)
    t0 = time.monotonic()
    r = run_check(chk("log_absent", container="app", pattern="(a+)+$", regex=True, timeout_s=5), env)
    assert not r.ok and "no verdict" in r.detail
    assert time.monotonic() - t0 < 4
    assert regex_search("(a+)+$", "a" * 60 + "b", 1.0, sys.executable) is None
    assert regex_search("a+", "caab", 2.0, sys.executable) is True
    assert regex_search("z", "caab", 2.0, sys.executable) is False


# ── file_state ──────────────────────────────────────────────────────────────
def test_file_state_hash_and_mode(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"data")
    f.chmod(0o640)
    digest = hashlib.sha256(b"data").hexdigest()
    assert run_check(chk("file_state", path=str(f), sha256=digest, mode="0640"), check_env()).ok
    assert not run_check(chk("file_state", path=str(f), sha256="0" * 64), check_env()).ok
    assert not run_check(chk("file_state", path=str(f), mode="0600"), check_env()).ok
    assert not run_check(chk("file_state", path=str(tmp_path / "nope"), mode="0600"), check_env()).ok
    assert not run_check(chk("file_state", path=str(tmp_path), mode="0755"), check_env()).ok       # a directory is not a file


def test_file_state_refuses_symlinks_and_fifos(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b"x")
    (tmp_path / "link").symlink_to(real)
    assert not run_check(chk("file_state", path=str(tmp_path / "link"), mode="0644"), check_env()).ok
    os.mkfifo(tmp_path / "fifo")
    assert not run_check(chk("file_state", path=str(tmp_path / "fifo"), mode="0644"), check_env()).ok
    assert stat.S_ISFIFO(os.stat(tmp_path / "fifo").st_mode)


# ── query ───────────────────────────────────────────────────────────────────
class FakeBroker:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def core_read(self, mission_id, name, params):
        self.calls.append((mission_id, name, params))
        if self.exc:
            raise self.exc
        return self.result


def test_query_goes_only_through_the_broker():
    broker = FakeBroker({"ok": True, "status": 200, "output": "queue empty", "truncated": False})
    env = check_env(broker=broker, mission_id="M-1")
    assert run_check(chk("query", tool="arr.queue_item", arguments_json='{"id": 3}', expect_contains="empty"), env).ok
    assert broker.calls == [("M-1", "arr.queue_item", {"id": 3})]
    assert not run_check(chk("query", tool="arr.queue_item", expect_contains="busy"), env).ok
    for bad in (FakeBroker({"ok": False, "status": 502, "output": ""}), FakeBroker(exc=RuntimeError("refused"))):
        assert not run_check(chk("query", tool="arr.queue_item"), check_env(broker=bad, mission_id="M-1")).ok
    assert not run_check(chk("query", tool="arr.queue_item"), check_env()).ok          # no broker


# ── retries ─────────────────────────────────────────────────────────────────
def test_retries_are_bounded_by_the_check_and_by_core_config():
    calls = []

    def bounded(argv, timeout, max_bytes):
        calls.append(1)
        return (0, "", "", False) if len(calls) == 3 else (1, "", "", False)

    r = run_check(chk("mount_readable", path="/d", container="app", retries=3), check_env(bounded))
    assert r.ok and r.attempts == 3
    calls.clear()
    r = run_check(chk("mount_readable", path="/d", container="app", retries=3), check_env(bounded, max_retries=0))
    assert not r.ok and r.attempts == 1 and len(calls) == 1


def test_host_header_brackets_ipv6_literals():
    from planet_express.missions.verify.checks import host_header_for
    assert host_header_for("::1", 8080, 80) == "[::1]:8080"
    assert host_header_for("::1", 80, 80) == "[::1]"
    assert host_header_for("app.example", 8080, 80) == "app.example:8080"
    assert host_header_for("app.example", 443, 443) == "app.example"


def test_ipv6_url_is_probed_with_a_bracketed_host_header():
    import socket
    if not socket.has_ipv6:
        pytest.skip("no ipv6")
    class V6(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

    try:
        server = V6(("::1", 0), _Echo)
    except OSError:
        pytest.skip("no ipv6 loopback")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        r = run_check(chk("http", url=f"http://[::1]:{port}/", consecutive=1, expect_body='"host": "[::1]:%d"' % port),
                      check_env(http=default_http))
        assert r.ok, r.detail
    finally:
        server.shutdown()
