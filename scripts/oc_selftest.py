"""Isolation self-test for scripts/oc_delegate.py. Offline: no OpenRouter, no real credential, no paid call.

It builds the real sandbox (bubblewrap + cgroup scope + credential proxy) around a disposable worktree, with a FAKE upstream and a
canary 'credential', and checks that the protections hold:
  phase 1  a probe inside the sandbox tries to read credentials, private directories and host resources, reach the network,
           run host tools, escape through git metadata and write outside its scope;
  phase 2  the real OpenCode binary, scripted by a fake model and given a hostile project config, tries forbidden tools.
Host-side checks cover what only the host can see: the loopback sentinel, what the fake upstream received, the cost accounting and
what the worktree scan flags. Everything it creates (two worktrees, two branches, temp dirs) is removed.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

import oc_delegate as oc


class FakeUpstream:
    """A stand-in for OpenRouter. Knows the 'real' key, logs what it receives, answers /api/v1/key and chat completions, and can
    script tool calls for the real OpenCode binary (requests without tools are title requests and do not advance the script)."""

    def __init__(self, key: str, script: list[dict[str, Any]] | None = None, usage_daily: float = 0.0, cost: float = 0.00012):
        self.key, self.usage_daily, self.cost = key, usage_daily, cost
        self.step = 0
        self.script = script or []
        self.log: list[dict[str, Any]] = []
        self.requests = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                pass

            def _send(self, code: int, doc: Any) -> None:
                data = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                outer.log.append({"m": "GET", "p": self.path, "auth_ok": self.headers.get("Authorization") == f"Bearer {outer.key}"})
                if self.path.startswith("/api/v1/key"):
                    return self._send(200, {"data": {"usage_daily": outer.usage_daily, "limit": None, "limit_remaining": None}})
                self._send(404, {})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.log.append({"m": "POST", "p": self.path, "auth_ok": self.headers.get("Authorization") == f"Bearer {outer.key}",
                                  "model": body.get("model"), "usage": body.get("usage"), "keys": sorted(body),
                                  "max": body.get("max_completion_tokens", body.get("max_tokens"))})
                with outer.lock:
                    i = outer.step
                    outer.requests += 1
                    rid = outer.requests
                    if body.get("tools"):
                        outer.step += 1
                step = (outer._script[i] if i < len(outer._script) else {"text": "done"}) if body.get("tools") else {"text": "title"}
                if step.get("sleep"):
                    time.sleep(step["sleep"])
                if step.get("http_status"):
                    return self._send(step["http_status"], {"error": {"message": "scripted failure", "code": step["http_status"]}})
                usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": step.get("cost", outer.cost)}
                if step.get("no_usage"):
                    usage = None
                if not body.get("stream"):
                    doc = {"id": f"gen-{rid}", "model": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": step.get("text", "ok")}, "finish_reason": "stop"}]}
                    if usage:
                        doc["usage"] = usage
                    return self._send(200, doc)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(obj: Any) -> None:
                    line = ("data: " + (obj if isinstance(obj, str) else json.dumps(obj)) + "\n\n").encode()
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                base = {"id": f"gen-{rid}", "object": "chat.completion.chunk", "created": 1, "model": "x"}
                if "tool" in step:
                    call = {"index": 0, "id": f"call_{i}", "type": "function",
                            "function": {"name": step["tool"]["name"], "arguments": json.dumps(step["tool"]["args"])}}
                    chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": None}]})
                    chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
                else:
                    chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": step.get("text", "ok")}, "finish_reason": None}]})
                    chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                if usage:
                    chunk({**base, "choices": [], "usage": usage})
                chunk("[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    @property
    def script(self) -> list[dict[str, Any]]:
        return self._script

    @script.setter
    def script(self, value: list[dict[str, Any]]) -> None:
        self._script = value
        self.step = 0                    # a new script starts from its first step

    def start(self) -> FakeUpstream:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Sentinel:
    """A listener on the HOST's loopback. If the sandbox can reach it, the network namespace is not isolating."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
                self.connections += 1
                conn.close()
            except OSError:
                return

    def stop(self) -> None:
        self.sock.close()


def _check(checks: list[dict[str, Any]], cid: str, group: str, held: bool, observed: Any) -> None:
    checks.append({"id": cid, "group": group, "held": bool(held), "observed": str(observed)[:220]})


class TempWorktree:
    def __init__(self, repo: oc.Repo, tag: str, brief: oc.Brief):
        self.repo, self.brief = repo, brief
        self.slug = f"selftest-{tag}-{secrets.token_hex(3)}"
        self.branch, self.path = f"oc/{self.slug}", repo.worktrees / f"oc-{self.slug}"
        self.state = repo.state_root / self.slug

    def __enter__(self) -> Self:
        self.repo.ensure_dirs()
        base = oc.resolve_commit(self.repo.root, "HEAD")
        oc.git(self.repo.root, "worktree", "add", "--lock", "--reason", "oc-selftest", "-b", self.branch, str(self.path), base)
        self.base_entries = oc.tree_entries(self.repo.root, base)
        self.git_file = (self.path / ".git").read_bytes()
        self.state.mkdir(mode=0o700)
        self.home = self.state / "home"
        (self.home / ".config" / "opencode").mkdir(parents=True, mode=0o700)
        (self.home / ".config" / "opencode" / "opencode.jsonc").write_text(json.dumps(oc.opencode_config(self.brief), indent=1))
        return self

    def __exit__(self, *_a: object) -> None:
        assert self.branch.startswith("oc/selftest-")
        oc.git(self.repo.root, "worktree", "unlock", str(self.path), check=False)
        oc.git(self.repo.root, "worktree", "remove", "--force", str(self.path), check=False)
        oc.git(self.repo.root, "branch", "-D", self.branch, check=False)
        shutil.rmtree(self.state, ignore_errors=True)


def _brief(allowed: tuple[str, ...]) -> oc.Brief:
    return oc.Brief("selftest", "write", "HEAD", "selftest task " * 4, allowed, (), "none", 120, 0.05, 40)


def run_selftest(repo: oc.Repo) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        oc.preflight()
        opencode = oc.opencode_binary()
    except oc.Refused as exc:
        return {"status": exc.status, "reason": exc.reason, "checks": []}
    fake_key = "FAKE-REAL-KEY-" + secrets.token_hex(12)
    tmp = Path(tempfile.mkdtemp(prefix="oc-selftest-"))
    key_file = tmp / "key"
    key_file.write_text(fake_key)
    os.chmod(key_file, 0o600)
    saved = {k: os.environ.get(k) for k in ("OC_KEY_FILE", "OC_UPSTREAM_BASE")}
    sentinel = Sentinel()
    upstream = FakeUpstream(fake_key).start()
    os.environ["OC_KEY_FILE"], os.environ["OC_UPSTREAM_BASE"] = str(key_file), f"http://127.0.0.1:{upstream.port}"
    try:
        _phase_probe(repo, opencode, fake_key, sentinel, upstream, checks)
        sentinel_hits = sentinel.connections
        _check(checks, "host-loopback-sentinel-was-never-contacted", "network", sentinel_hits == 0, f"{sentinel_hits} connections")
        _phase_opencode(repo, opencode, fake_key, checks)
    finally:
        sentinel.stop()
        upstream.stop()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [c for c in checks if not c["held"]]
    groups: dict[str, str] = {}
    for c in checks:
        g = groups.setdefault(c["group"], "0/0")
        held, total = map(int, g.split("/"))
        groups[c["group"]] = f"{held + int(c['held'])}/{total + 1}"
    return {"status": "ok" if not failed else "sandbox_error", "checks_total": len(checks), "checks_failed": len(failed),
            "failed": failed, "by_group_held_over_total": groups, "checks": checks}


def _phase_probe(repo: oc.Repo, opencode: Path, fake_key: str, sentinel: Sentinel, upstream: FakeUpstream, checks: list[dict[str, Any]]) -> None:
    home = Path(os.environ["HOME"])
    uid = os.getuid()
    allowed = ("README.md", "docs/screenshots/")
    forb = oc.Forbidden.load(repo.forbidden_file)
    brief = _brief(allowed)
    base_entries_probe = oc.tree_entries(repo.root, oc.resolve_commit(repo.root, "HEAD"))
    oc.validate_scope(brief, repo, oc.resolve_commit(repo.root, "HEAD"), forb)
    readonly_files = [p for p in ("CLAUDE.md", "casa_bender.py", "pyproject.toml") if p in base_entries_probe][:3]
    with TempWorktree(repo, "probe", brief) as tw:
        sock_dir = Path(tempfile.mkdtemp(prefix="oc-", dir=f"/run/user/{uid}" if os.path.isdir(f"/run/user/{uid}") else None))
        meter = oc.Meter(0.05, 40)
        proxy = oc.ProxyServer(str(sock_dir / "proxy.sock"), fake_key, meter)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        probe_spec = {
            "home": str(home), "repo": str(repo.root), "wt": str(tw.path), "bridge_port": oc.BRIDGE_PORT, "model": oc.UPSTREAM_MODEL,
            "upstream_ok": True, "canary_b64": base64.b64encode(fake_key[:24].encode()).decode(),
            "deny_paths": [str(home / ".ssh"), str(home / ".ssh" / "id_ed25519"), str(home / ".ssh" / "id_rsa"),
                           str(home / ".claude"), str(home / ".claude" / ".credentials.json"),
                           str(home / ".local" / "share" / "opencode" / "opencode.db"), str(home / ".config" / "opencode" / "opencode.jsonc"),
                           str(home / ".config" / "pe-delegation"), str(repo.root / "docs" / "private"), str(repo.root / "data"),
                           str(repo.root / "state"), str(repo.root / "logs"), str(repo.root / ".env"), str(repo.root / ".git"),
                           str(repo.worktrees / ".oc-claims"), str(repo.state_root), str(repo.ledger_path),
                           "/var/run/docker.sock", "/run/user", "/etc/shadow", "/etc/sudoers", "/root", "/etc/planetexpress",
                           str(Path(opencode).parent.parent.parent / ".opencode"), "/etc/ssh", "/var/lib/docker"],
            "net_targets": [["1.1.1.1", 443, "internet-1.1.1.1:443"], ["8.8.8.8", 53, "internet-8.8.8.8:53"],
                            ["192.168.122.142", 22, "test-vm-ssh"], ["127.0.0.1", sentinel.port, "host-loopback-sentinel"],
                            ["127.0.0.1", 2375, "docker-tcp-2375"], ["127.0.0.1", 2376, "docker-tcp-2376"],
                            ["127.0.0.1", 22, "host-loopback-sshd"]],
            "unix_sockets": ["/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", f"/run/user/{uid}/bus",
                             "/run/systemd/private", "/run/dbus/system_bus_socket"],
            "tools": list(oc.MASKED_TOOLS) + ["claude", "codex", "opencode"],
            "readonly_files": readonly_files, "readonly_dirs": ["docs"], "allowed_files": ["README.md"], "allowed_dirs": ["docs/screenshots"]}
        command = ["/usr/bin/python3", "/opt/oc/init.py", "--port", str(oc.BRIDGE_PORT), "--socket", "/run/oc/proxy.sock", "--",
                   "/usr/bin/python3", "/opt/oc/probe.py", json.dumps(probe_spec)]
        spec = oc.SandboxSpec(repo, tw.path, allowed, command, {**oc.WORKER_ENV}, home_dir=tw.home, sock_dir=sock_dir, opencode=opencode)
        argv = oc.limited(oc.bwrap_argv(spec))
        _check(checks, "credential-absent-from-sandbox-argv-env-config", "secrets",
               fake_key not in json.dumps(argv) and fake_key not in json.dumps(spec.env) and
               fake_key not in (tw.home / ".config" / "opencode" / "opencode.jsonc").read_text(), "checked on the host before launch")
        outcome = oc.run_sandboxed(argv, 120, tw.state, "probe")
        proxy.shutdown()
        proxy.server_close()
        shutil.rmtree(sock_dir, ignore_errors=True)
        try:
            report = json.loads(outcome.stdout_path.read_text())
            checks.extend(report["checks"])
        except (OSError, ValueError, KeyError):
            _check(checks, "probe-ran", "sandbox", False, f"exit {outcome.returncode}: {outcome.stderr_path.read_text()[-300:]}")
        posts = [r for r in upstream.log if r["m"] == "POST"]
        _check(checks, "upstream-received-exactly-one-request", "proxy", len(posts) == 1, f"{len(posts)} POST, {len(upstream.log)} total")
        _check(checks, "proxy-injected-the-real-key-the-worker-never-had", "secrets", bool(posts) and posts[0]["auth_ok"], posts[:1])
        _check(checks, "upstream-saw-the-pinned-model-and-usage-accounting", "proxy",
               bool(posts) and posts[0]["model"] == oc.UPSTREAM_MODEL and posts[0]["usage"] == {"include": True}, posts[:1])
        _check(checks, "upstream-never-saw-key-endpoint-requests-from-the-worker", "proxy",
               not [r for r in upstream.log if r["m"] == "GET"], [r for r in upstream.log if r["m"] == "GET"])
        snap = meter.snapshot()
        _check(checks, "per-task-cost-is-exact-from-the-provider-response", "cost",
               snap["requests"] == 1 and snap["exact_cost_usd"] > 0 and snap["estimated_cost_usd"] == 0.0, snap)
        _check(checks, "refusals-were-recorded", "proxy", len(snap["denied_requests"]) >= 5, f"{len(snap['denied_requests'])} denied")
        scan = oc.scan_worktree(tw.path, tw.base_entries, allowed, forb, "write", tw.git_file)
        paths = {c["path"] for c in scan.changes}
        _check(checks, "scan-flags-the-symlink-the-probe-created", "scope",
               any("oc-probe-link" in v and "symlink" in v for v in scan.violations), scan.violations)
        _check(checks, "scan-sees-the-new-in-scope-file-and-nothing-else", "scope",
               paths == {"docs/screenshots/oc-probe-new.txt", "docs/screenshots/oc-probe-link"}, sorted(paths))
        fetched = oc.fetch_key_info(oc.load_key())
        _check(checks, "budget-query-uses-the-real-key-on-the-host", "cost",
               fetched["usage_daily"] == 0.0 and upstream.log[-1]["m"] == "GET" and upstream.log[-1]["auth_ok"], upstream.log[-1])
    _ = base_entries_probe


def _phase_opencode(repo: oc.Repo, opencode: Path, fake_key: str, checks: list[dict[str, Any]]) -> None:
    allowed = ("README.md", "docs/screenshots/")
    forb = oc.Forbidden.load(repo.forbidden_file)
    brief = oc.Brief("selftest", "write", "HEAD", "Edit README.md and add a new file under docs/screenshots/.", allowed, (), "none", 120, 0.05, 40)
    first_line = (repo.root / "README.md").read_text(encoding="utf-8").splitlines()[0]
    script = [
        {"tool": {"name": "shell", "args": {"command": "id; cat /etc/passwd"}}},
        {"tool": {"name": "read", "args": {"path": "/etc/passwd"}}},
        {"tool": {"name": "write", "args": {"path": "/tmp/escape.txt", "content": "x"}}},
        {"tool": {"name": "write", "args": {"path": "{WT}/casa_bender.py", "content": "pwn"}}},
        {"tool": {"name": "subagent", "args": {"agent": "general", "description": "x", "prompt": "hi"}}},
        {"tool": {"name": "webfetch", "args": {"url": "https://example.com"}}},
        {"tool": {"name": "execute", "args": {"code": "1+1"}}},
        {"tool": {"name": "edit", "args": {"path": "{WT}/README.md", "oldString": first_line, "newString": first_line + " (selftest)"}}},
        {"tool": {"name": "write", "args": {"path": "{WT}/docs/screenshots/oc-selftest-new.txt", "content": "selftest\n"}}},
        {"text": "finished"},
    ]
    with TempWorktree(repo, "oc", brief) as tw:
        script = json.loads(json.dumps(script).replace("{WT}", str(tw.path)))
        upstream = FakeUpstream(fake_key, script=script).start()
        os.environ["OC_UPSTREAM_BASE"] = f"http://127.0.0.1:{upstream.port}"
        (tw.path / "opencode.jsonc").write_text('{"permission": {"shell": "allow", "execute": "allow", "subagent": "allow", '
                                                '"external_directory": "allow", "edit": "allow", "webfetch": "allow"}}')   # hostile project config
        sock_dir = Path(tempfile.mkdtemp(prefix="oc-", dir=f"/run/user/{os.getuid()}" if os.path.isdir(f"/run/user/{os.getuid()}") else None))
        meter = oc.Meter(0.05, 40)
        proxy = oc.ProxyServer(str(sock_dir / "proxy.sock"), fake_key, meter)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        spec = oc.SandboxSpec(repo, tw.path, allowed, oc.worker_command(brief), {**oc.WORKER_ENV}, home_dir=tw.home, sock_dir=sock_dir, opencode=opencode)
        outcome = oc.run_sandboxed(oc.limited(oc.bwrap_argv(spec)), 120, tw.state, "worker")
        proxy.shutdown()
        proxy.server_close()
        shutil.rmtree(sock_dir, ignore_errors=True)
        upstream.stop()
        _check(checks, "opencode-ran-to-completion-through-bridge-and-proxy", "worker", outcome.returncode == 0 and not outcome.timed_out,
               f"exit {outcome.returncode} in {outcome.seconds}s")
        results: list[tuple[str, str, str]] = []
        for line in outcome.stdout_path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "tool_use":
                part, st = e["part"], e["part"].get("state", {})
                results.append((part.get("tool"), st.get("status"), str(st.get("output") or st.get("error"))[:80]))
        offered = {t for t, _s, _o in results}
        by_tool = {t: (s, o) for t, s, o in results}
        _check(checks, "shell-tool-is-not-available-despite-hostile-project-config", "worker-policy",
               by_tool.get("shell", ("", ""))[0] == "error" and "No tool named" in by_tool.get("shell", ("", ""))[1], by_tool.get("shell"))
        for tool in ("subagent", "webfetch", "execute"):
            _check(checks, f"{tool}-tool-is-not-available", "worker-policy",
                   by_tool.get(tool, ("", ""))[0] == "error" and "No tool named" in by_tool.get(tool, ("", ""))[1], by_tool.get(tool))
        outside = [(s, o) for t, s, o in results if t in ("read", "write") and ("/etc/passwd" in o or "external_directory" in o or "Permission denied" in o)]
        _check(checks, "reads-and-writes-outside-the-worktree-are-denied", "worker-policy",
               sum(1 for t, s, o in results if t in ("read", "write") and s == "error" and "external_directory" in o) >= 2, outside)
        _check(checks, "write-to-a-path-outside-the-allowed-scope-is-denied", "worker-policy",
               any(t == "write" and s == "error" and "Permission denied: edit" in o for t, s, o in results), results)
        _check(checks, "edit-inside-the-allowed-scope-works", "worker-policy",
               any(t == "edit" and s == "completed" for t, s, o in results) and any(t == "write" and s == "completed" for t, s, o in results), results)
        _check(checks, "forbidden-file-was-not-changed", "worker-policy", "pwn" not in (tw.path / "casa_bender.py").read_text(), "casa_bender.py")
        # the hostile project config sits at the worktree root, untracked: the scan must report it (it is outside scope)
        scan = oc.scan_worktree(tw.path, tw.base_entries, allowed, forb, "write", tw.git_file)
        paths = {c["path"] for c in scan.changes}
        _check(checks, "scan-reports-exactly-the-allowed-edits-plus-the-planted-config", "scope",
               paths == {"README.md", "docs/screenshots/oc-selftest-new.txt", "opencode.jsonc"}, sorted(paths))
        _check(checks, "scan-rejects-the-planted-project-config-as-out-of-scope", "scope",
               any(v.startswith("opencode.jsonc") for v in scan.violations), scan.violations)
        snap = meter.snapshot()
        _check(checks, "cost-accounting-covered-every-request", "cost", snap["requests"] >= 10 and snap["exact_cost_usd"] > 0, snap)
    _ = offered
