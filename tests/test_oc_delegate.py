"""scripts/oc_delegate.py: the OpenCode delegation wrapper. Offline throughout: a fake upstream stands in for OpenRouter, the
credential is a canary, and no paid model is ever called. Tests that need bubblewrap, a user cgroup scope and the OpenCode binary
are skipped where those are missing (CI)."""
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("CASA_CONFIG", str(SCRIPTS.parent / "config.example.yaml"))

import oc_delegate as oc
import oc_selftest

REAL_REPO = Path(__file__).resolve().parent.parent
KEY = "FAKE-REAL-KEY-0123456789abcdef"


def have_sandbox() -> bool:
    try:
        oc.opencode_binary()
        return subprocess.run(["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                               "--symlink", "usr/lib64", "/lib64", "--unshare-all", "/usr/bin/true"],
                              capture_output=True, timeout=20, check=False).returncode == 0 and oc.cgroup_available()
    except (oc.Refused, OSError, subprocess.SubprocessError):
        return False


needs_sandbox = pytest.mark.skipif(not have_sandbox(), reason="needs bubblewrap, a user cgroup scope and the opencode binary")


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────────

GIT_IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, env=GIT_IDENT, check=True, capture_output=True, text=True).stdout


FILES = {
    "README.md": "# Demo project\n\nSome text.\n",
    "docs/guide.md": "# Guide\n\nOriginal guide text.\n",
    "docs/other.md": "other\n",
    "notes/a.md": "note a\n",
    "casa_bender.py": "SAFETY = True\n",
    "tests/test_demo.py": "def test_demo():\n    assert True\n",
    "tests/test_mission_x.py": "def test_x():\n    assert True\n",
    "app/tool.py": "def tool():\n    return 1\n",
    "config.example.yaml": "a: 1\n",
}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "oc_forbidden_paths.txt").write_text((SCRIPTS / "oc_forbidden_paths.txt").read_text())
    (root / ".gitignore").write_text("/.claude/\n/.venv\n")
    if (REAL_REPO / ".venv").is_dir():
        (root / ".venv").symlink_to(REAL_REPO / ".venv")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "add", "-A", ".")
    sh(root, "git", "commit", "-q", "-m", "base")
    return oc.Repo(root)


@pytest.fixture
def upstream():
    ups = oc_selftest.FakeUpstream(KEY).start()
    yield ups
    ups.stop()


@pytest.fixture
def env(tmp_path, upstream, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("OC_KEY_FILE", str(key_file))
    monkeypatch.setenv("OC_UPSTREAM_BASE", f"http://127.0.0.1:{upstream.port}")
    monkeypatch.delenv("PE_DELEGATION", raising=False)
    monkeypatch.delenv("OC_DAILY_CAP_USD", raising=False)
    return upstream


def brief_doc(**over):
    doc = {"slug": "t-docs-edit", "mode": "write", "base": "main", "task": "Update docs/guide.md as described in the task text.",
           "allowed": ["docs/guide.md"], "acceptance": [], "timeout_s": 120}
    doc.update(over)
    return doc


def refused(doc, match):
    with pytest.raises(oc.Refused, match=match):
        oc.parse_brief(doc)


# ── paths ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x", "a/../b", "a/./b", "a\\b", "a\0b", "-rf", "~/x", ".git/config", "x/.claude/y",
                                 ".venv/bin/python", "a\nb", "é.md", "x" * 401])
def test_norm_rel_rejects_hostile_paths(bad):
    with pytest.raises(oc.Refused):
        oc.norm_rel(bad)


def test_norm_rel_canonicalises():
    assert oc.norm_rel("docs//guide.md") == "docs/guide.md"
    assert oc.norm_rel("docs/") == "docs/"


def test_overlap_and_inside():
    assert oc.overlaps("docs/", "docs/a.md") and oc.overlaps("docs/a/", "docs/") and oc.overlaps("a.md", "a.md")
    assert not oc.overlaps("docs/a.md", "docs/b.md") and not oc.overlaps("docs/", "docs2/x") and not oc.overlaps("a/", "ab/c")
    assert oc.inside("docs/a/b.md", ("docs/",)) and not oc.inside("docs2/a.md", ("docs/",)) and oc.inside("x.md", ("x.md",))


@pytest.mark.parametrize("path", ["casa_bender.py", "casa_farnsworth.py", "planet_express/missions/grants.py", "planet_express/execution/policy.py",
                                  "planet_express/core/redact.py", "tests/test_mission_x.py", "tests/test_dockerproxy_policy.py", "tests/conftest.py",
                                  "tests/test_oc_delegate.py", "scripts/oc_delegate.py", "scripts/run_rehearsal.sh", "systemd/x.service",
                                  "deploy.sh", "requirements.txt", "pyproject.toml", ".github/workflows/ci.yml", "docs/private/x.md", "data/a.db",
                                  "a/b/.env", ".env", "keys/server.pem", "x/secrets.yaml", "CLAUDE.md", "docs/handoff/v4/STATE.md",
                                  "docs/designs/v4-orchestration.md", "web_auth.py", "sub/.gitattributes", "opencode.jsonc", "config.yaml",
                                  "planet_express/widgets/sonarr.py", "static/elevate.js", "templates/approvals.html", "docs/CLAUDE.md", "docs/sub/AGENTS.md",
                                  "docs/CLAUDE.local.md", "tests/unit/conftest.py", "pytest.ini", "tests/pytest.ini", "tox.ini", "ruff.toml",
                                  "docs/.ruff.toml", "setup.cfg", "tests/sitecustomize.py", "x/evil.pth", "sub/pyproject.toml", "Makefile"])
def test_forbidden_patterns_cover_the_safety_surface(path):
    assert oc.Forbidden.load(SCRIPTS / "oc_forbidden_paths.txt").match(path), path


@pytest.mark.parametrize("path", ["README.md", "docs/guide.md", "notes/a.md", "tests/test_demo.py", "INSTALL.md", "docs/screenshots/a.png"])
def test_ordinary_paths_are_not_forbidden(path):
    assert oc.Forbidden.load(SCRIPTS / "oc_forbidden_paths.txt").match(path) is None, path


def test_directory_conflicts_force_narrow_scopes():
    forb = oc.Forbidden.load(SCRIPTS / "oc_forbidden_paths.txt")
    assert forb.dir_conflict("tests/", ["tests/test_demo.py"])          # patterns live inside tests/
    assert forb.dir_conflict("planet_express/", ["planet_express/widgets/x.py"])
    assert forb.dir_conflict("docs/", ["docs/guide.md"])
    assert forb.dir_conflict("planet_express/missions/", [])
    assert forb.dir_conflict("notes/", ["notes/a.md"]) is None
    assert forb.dir_conflict("docs/screenshots/", ["docs/screenshots/a.png"]) is None
    for executed in ("planet_express/widgets/", "static/", "templates/"):         # run by Core or by the operator's browser
        assert forb.dir_conflict(executed, [executed + "x"]), executed


def test_missing_or_empty_forbidden_file_refuses(tmp_path):
    with pytest.raises(oc.Refused, match="unreadable"):
        oc.Forbidden.load(tmp_path / "nope.txt")
    (tmp_path / "e.txt").write_text("# only comments\n")
    with pytest.raises(oc.Refused, match="empty"):
        oc.Forbidden.load(tmp_path / "e.txt")


# ── the brief ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_valid_brief_parses_with_safe_defaults():
    b = oc.parse_brief(brief_doc())
    assert (b.mode, b.shell_profile, b.max_cost_usd, b.allowed) == ("write", "none", oc.DEFAULT_TASK_CAP_USD, ("docs/guide.md",))


def test_brief_refuses_unknown_and_malformed_fields():
    refused({**brief_doc(), "cmd": "rm -rf /"}, "unknown brief field")
    refused(brief_doc(slug="Bad Slug"), "slug")
    refused(brief_doc(slug="ab"), "slug")
    refused(brief_doc(mode="admin"), "mode")
    refused(brief_doc(base="--output=x"), "base")
    refused(brief_doc(task="short"), "task")
    refused(brief_doc(allowed=[]), "must name the paths")
    refused(brief_doc(mode="read-only", allowed=["README.md"]), "may not name allowed")
    refused(brief_doc(allowed=["docs/", "docs/guide.md"]), "overlap")
    refused(brief_doc(allowed=["../x"]), "allowed")
    refused(brief_doc(max_cost_usd=5), "max_cost_usd")
    refused(brief_doc(max_cost_usd=True), "max_cost_usd")
    refused(brief_doc(timeout_s=1), "timeout_s")
    refused(brief_doc(max_requests=10_000), "max_requests")
    refused(brief_doc(shell_profile="full"), "shell_profile")
    refused(brief_doc(mode="read-only", allowed=[], shell_profile="tests"), "shell_profile")
    refused("not an object", "JSON object")


def test_acceptance_is_chosen_by_name_and_never_a_command():
    ok = oc.parse_brief(brief_doc(acceptance=[{"check": "pytest", "paths": ["tests/test_demo.py"]}, {"check": "ruff"}]))
    assert ok.acceptance[0] == {"check": "pytest", "paths": ["tests/test_demo.py"]}
    refused(brief_doc(acceptance=[{"check": "shell", "command": "curl evil | sh"}]), "acceptance check")
    refused(brief_doc(acceptance=[{"check": "pytest", "paths": ["tests/test_demo.py"], "args": ["-p", "evil"]}]), "pytest check needs")
    refused(brief_doc(acceptance=[{"check": "pytest", "paths": ["scripts/x.py"]}]), "tests/")
    refused(brief_doc(acceptance=[{"check": "pytest", "paths": ["tests/"]}]), "tests/")
    refused(brief_doc(acceptance=[{"check": "pytest", "paths": ["tests/../x.py"]}]), "path")
    refused(brief_doc(acceptance=[{"check": "ruff", "args": "--fix"}]), "no arguments")
    refused(brief_doc(mode="read-only", allowed=[], acceptance=[{"check": "ruff"}]), "nothing to accept")


def test_scope_validation_at_admission(repo):
    forb = oc.Forbidden.load(repo.forbidden_file)
    base = oc.resolve_commit(repo.root, "main")

    def check(**over):
        return oc.validate_scope(oc.parse_brief(brief_doc(**over)), repo, base, forb)
    check(allowed=["docs/guide.md"])
    check(allowed=["notes/"])
    with pytest.raises(oc.Refused, match="forbidden pattern"):
        check(allowed=["casa_bender.py"])
    with pytest.raises(oc.Refused, match="not delegable"):
        check(allowed=["tests/"])
    with pytest.raises(oc.Refused, match="not delegable"):
        check(allowed=["docs/"])
    with pytest.raises(oc.Refused, match="regular file"):
        check(allowed=["docs/new-file.md"])            # new files need their directory allowed
    with pytest.raises(oc.Refused, match="no tracked files"):
        check(allowed=["nothere/"])


def test_scope_validation_refuses_symlinks_and_submodules(repo):
    (repo.root / "notes" / "link").symlink_to("/etc/passwd")
    sh(repo.root, "git", "add", "notes/link")
    sh(repo.root, "git", "commit", "-q", "-m", "link")
    forb = oc.Forbidden.load(repo.forbidden_file)
    base = oc.resolve_commit(repo.root, "main")
    with pytest.raises(oc.Refused, match="symlink"):
        oc.validate_scope(oc.parse_brief(brief_doc(allowed=["notes/"])), repo, base, forb)
    with pytest.raises(oc.Refused, match="regular file"):
        oc.validate_scope(oc.parse_brief(brief_doc(allowed=["notes/link"])), repo, base, forb)


def test_base_must_resolve(repo):
    with pytest.raises(oc.Refused, match="does not resolve"):
        oc.resolve_commit(repo.root, "no-such-ref")


# ── key, upstream, budget ───────────────────────────────────────────────────────────────────────────────────────────

def test_key_file_must_be_private(tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_text(KEY)
    f.chmod(0o644)
    monkeypatch.setenv("OC_KEY_FILE", str(f))
    with pytest.raises(oc.Refused, match="0600") as exc:
        oc.load_key()
    assert KEY not in str(exc.value)
    f.chmod(0o600)
    assert oc.load_key() == KEY


def test_the_opencode_store_key_is_used_only_when_opted_into_and_fails_closed(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("OC_KEY_FILE", str(tmp_path / "absent"))
    db = tmp_path / "oc.db"
    monkeypatch.setenv("OC_OPENCODE_DB", str(db))
    monkeypatch.delenv("OC_ALLOW_OPENCODE_STORE_KEY", raising=False)
    con = sqlite3.connect(db)
    con.execute("create table credential (id text, integration_id text, value text, active int, time_updated int)")
    con.execute("insert into credential values ('1','openrouter',?,1,1)", (json.dumps({"type": "api", "key": KEY}),))
    con.execute("insert into credential values ('2','other',?,1,2)", (json.dumps({"type": "api", "key": "WRONG-KEY-0123456789"}),))
    con.commit()
    con.close()
    with pytest.raises(oc.Refused, match="no dedicated key file") as exc:
        oc.load_key()                                        # present in the store, but the shared key is not used silently
    assert KEY not in str(exc.value)
    monkeypatch.setenv("OC_ALLOW_OPENCODE_STORE_KEY", "1")
    key, source = oc.load_key_with_source()
    assert key == KEY and "no provider-side limit" in source
    con = sqlite3.connect(db)
    con.execute("update credential set value='not json' where id='1'")
    con.commit()
    con.close()
    with pytest.raises(oc.Refused, match="no usable"):
        oc.load_key()


def test_the_real_key_is_only_ever_sent_to_openrouter_or_loopback(monkeypatch):
    monkeypatch.delenv("OC_UPSTREAM_BASE", raising=False)
    assert oc.upstream_target() == ("https", "openrouter.ai", 443)
    for evil in ("https://evil.example", "http://evil.example:80", "http://127.0.0.1.evil.example:80", "http://10.0.0.5:8080", "http://127.0.0.1"):
        monkeypatch.setenv("OC_UPSTREAM_BASE", evil)
        with pytest.raises(oc.Refused, match="loopback"):
            oc.upstream_target()
    monkeypatch.setenv("OC_UPSTREAM_BASE", "http://127.0.0.1:9999")
    assert oc.upstream_target() == ("http", "127.0.0.1", 9999)


def test_key_info_fails_closed(env, monkeypatch):
    assert oc.fetch_key_info(KEY)["usage_daily"] == 0.0
    env.usage_daily = None                                   # field present but not a number
    with pytest.raises(oc.Refused, match="usage_daily"):
        oc.fetch_key_info(KEY)
    monkeypatch.setenv("OC_UPSTREAM_BASE", "http://127.0.0.1:9")      # nothing listening
    with pytest.raises(oc.Refused, match="cannot determine spend"):
        oc.fetch_key_info(KEY)


def test_budget_admission(tmp_path, monkeypatch):
    monkeypatch.delenv("OC_DAILY_CAP_USD", raising=False)
    ledger = oc.Ledger(tmp_path / "ledger.jsonl")
    d = oc.budget_decision({"usage_daily": 1.5}, ledger, 0.0, 0.25)
    assert d["ok"] and d["headroom_usd"] == 0.5 and d["provider_side_limit_set"] is False
    assert not oc.budget_decision({"usage_daily": 1.80}, ledger, 0.0, 0.25)["ok"]
    assert not oc.budget_decision({"usage_daily": 1.0}, ledger, 0.9, 0.25)["ok"]             # running tasks hold their caps
    assert oc.budget_decision({"usage_daily": 1.75}, ledger, 0.0, 0.25)["ok"]                # exactly at the cap is allowed
    assert not oc.budget_decision({"usage_daily": 1.0, "limit_remaining": 0.1}, ledger, 0.0, 0.25)["ok"]   # provider-side limit
    ledger.append({"kind": "task", "date": oc.utc_today(), "exact_cost_usd": 1.9, "estimated_cost_usd": 0.0})
    assert not oc.budget_decision({"usage_daily": 0.0}, ledger, 0.0, 0.25)["ok"]             # the ledger may be ahead of the provider
    ledger.append({"kind": "task", "date": "1999-01-01", "exact_cost_usd": 50.0})              # other days do not count
    assert ledger.today()["exact_usd"] == 1.9


def test_the_daily_cap_can_only_be_lowered(monkeypatch):
    monkeypatch.setenv("OC_DAILY_CAP_USD", "50")
    assert oc.daily_cap() == 2.0
    monkeypatch.setenv("OC_DAILY_CAP_USD", "0.5")
    assert oc.daily_cap() == 0.5
    monkeypatch.setenv("OC_DAILY_CAP_USD", "junk")
    assert oc.daily_cap() == 2.0


# ── the proxy ───────────────────────────────────────────────────────────────────────────────────────────────────────

class ProxyRig:
    def __init__(self, tmp_path, meter):
        self.path = str(tmp_path / "p.sock")
        self.meter = meter
        self.server = oc.ProxyServer(self.path, KEY, meter)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.path)
        payload = json.dumps(body).encode() if body is not None else b""
        head = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(payload)}\r\nContent-Type: application/json\r\n"
        for k, v in (headers or {}).items():
            head += f"{k}: {v}\r\n"
        s.sendall(head.encode() + b"\r\n" + payload)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        status = int(data.split(b" ", 2)[1]) if data else 0           # 0: the proxy hung up without answering
        return status, data.partition(b"\r\n\r\n")[2]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxy(tmp_path, env):
    rig = ProxyRig(tmp_path, oc.Meter(0.05, 4))
    yield rig
    rig.close()


CHAT = "/api/v1/chat/completions"
MSGS = [{"role": "user", "content": "hi"}]


def test_proxy_only_serves_chat_completions_with_the_pinned_model(proxy, env):
    for method, path, body in (("GET", "/api/v1/key", None), ("GET", "/api/v1/credits", None), ("POST", "/api/v1/keys", {"a": 1}),
                               ("POST", CHAT + "?x=1", {"model": oc.UPSTREAM_MODEL, "messages": MSGS}),
                               ("POST", CHAT, {"model": "openai/gpt-4o", "messages": MSGS}),
                               ("POST", CHAT, {"model": "openai/gpt-4o", "models": [oc.UPSTREAM_MODEL], "messages": MSGS}),
                               ("POST", CHAT, {"messages": MSGS}), ("PUT", CHAT, {}), ("DELETE", "/api/v1/key", None)):
        assert proxy.call(method, path, body)[0] in (403, 411), (method, path)
    assert env.log == [], "nothing refused may reach the upstream"
    assert len(proxy.meter.denied) >= 9


def test_proxy_injects_the_key_ignores_the_clients_and_sanitises_the_body(proxy, env):
    status, body = proxy.call("POST", CHAT, {"model": oc.MODEL_ID, "messages": MSGS, "max_tokens": 10**9, "provider": {"order": ["x"]},
                                             "plugins": [{"id": "web"}], "route": "fallback", "transforms": ["middle-out"]},
                              headers={"Authorization": "Bearer attacker-supplied"})
    assert status == 200 and json.loads(body)["choices"]
    (rec,) = env.log
    assert rec["auth_ok"] and rec["model"] == oc.UPSTREAM_MODEL and rec["usage"] == {"include": True}
    assert rec["max"] == oc.MAX_OUTPUT_TOKENS
    assert not {"provider", "plugins", "route", "transforms"} & set(rec["keys"])
    assert any("dropped fields" in d for d in proxy.meter.denied)


def test_proxy_streaming_cost_is_exact_and_budget_caps_hold(proxy, env):
    env.cost = 0.02
    for _ in range(2):
        status, body = proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "stream": True})
        assert status == 200 and b"[DONE]" in body
    snap = proxy.meter.snapshot()
    assert snap["exact_cost_usd"] == pytest.approx(0.04) and snap["estimated_cost_usd"] == 0 and snap["prompt_tokens"] == 200
    assert snap["exactness"] == "exact" and len(proxy.meter.generation_ids) == 2
    assert proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "stream": True})[0] == 200
    status, _ = proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS})
    assert status == 429                                    # 0.06 spent >= the 0.05 cap
    assert len(env.log) == 3


def test_proxy_request_limit(tmp_path, env):
    rig = ProxyRig(tmp_path, oc.Meter(1.0, 2))
    try:
        codes = [rig.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS})[0] for _ in range(3)]
    finally:
        rig.close()
    assert codes == [200, 200, 429]


def test_a_response_without_usage_is_charged_as_an_estimate(proxy, env):
    env.script = [{"text": "x", "no_usage": True}]
    status, _ = proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "tools": [{"type": "function"}]})
    snap = proxy.meter.snapshot()
    assert status == 200 and snap["exact_cost_usd"] == 0 and snap["estimated_cost_usd"] > 0 and snap["exactness"] == "includes estimates"


def test_upstream_errors_are_forwarded_and_not_charged(proxy, env):
    env.script = [{"http_status": 500}]
    status, _ = proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "tools": [{"type": "function"}]})
    snap = proxy.meter.snapshot()
    assert status == 500 and snap["upstream_errors"] == 1 and snap["exact_cost_usd"] == 0


# ── the sandbox command line and OpenCode policy ────────────────────────────────────────────────────────────────────

def spec_for(repo, tmp_path, allowed=("docs/guide.md", "notes/"), **kw):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return oc.SandboxSpec(repo, wt, allowed, ["/usr/bin/true"], {**oc.WORKER_ENV}, home_dir=tmp_path / "h", sock_dir=tmp_path / "s",
                          opencode=Path("/bin/true"), **kw)


def test_sandbox_argv_is_isolated_by_construction(repo, tmp_path):
    spec = spec_for(repo, tmp_path)
    argv = oc.bwrap_argv(spec)
    text = " ".join(argv)
    for needed in ("--unshare-all", "--die-with-parent", "--new-session", "--cap-drop ALL", "--clearenv"):
        assert needed in text
    assert "--share-net" not in argv and "--dev-bind" not in argv and "--bind / " not in text
    home = os.environ["HOME"]
    destinations = [argv[i + 2] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    exposed = [d for d in destinations if d.startswith(home) and not d.startswith(str(spec.worktree))]
    assert exposed == [], f"home directory paths mounted into the sandbox: {exposed}"
    wt = str(spec.worktree)
    ro = argv.index("--ro-bind", argv.index(wt) - 2)
    assert argv[ro:ro + 3] == ["--ro-bind", wt, wt]
    rw = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--bind" and argv[i + 1].startswith(wt)]
    assert rw == [(f"{wt}/docs/guide.md",) * 2, (f"{wt}/notes",) * 2]
    assert all(argv.index("--ro-bind") < argv.index("--bind") for _ in rw)                   # read-only first, rw carve-outs after
    assert not [e for e in argv if "docker.sock" in e or ".ssh" in e or ".claude" in e or "credentials" in e]


def test_read_only_task_has_no_writable_worktree_paths(repo, tmp_path):
    argv = oc.bwrap_argv(spec_for(repo, tmp_path, allowed=()))
    wt = str(tmp_path / "wt")
    assert not [i for i, a in enumerate(argv) if a == "--bind" and argv[i + 1].startswith(wt)]


def test_masked_tools_are_files_under_usr_only():
    for target in oc.masked_targets():
        assert target.startswith("/usr/") and os.path.isfile(target)


def test_opencode_policy_denies_what_a_worker_should_not_have():
    b = oc.parse_brief(brief_doc(allowed=["docs/guide.md", "notes/"]))
    cfg = oc.opencode_config(b)
    perm = cfg["permission"]
    for tool in ("subagent", "task", "execute", "webfetch", "websearch", "skill", "question", "external_directory", "shell", "bash"):
        assert perm[tool] == "deny", tool
    assert perm["edit"] == {"*": "deny", "docs/guide.md": "allow", "notes/**": "allow"}
    assert cfg["model"] == cfg["small_model"] == oc.MODEL_ID
    assert cfg["provider"]["openrouter"]["options"]["apiKey"] == "sandbox-has-no-credential"
    assert list(cfg["provider"]) == ["openrouter"] and cfg["share"] == "disabled" and cfg["update"] == "disable"
    ro = oc.opencode_config(oc.parse_brief(brief_doc(mode="read-only", allowed=[])))
    assert ro["permission"]["edit"] == "deny"
    tests = oc.opencode_config(oc.parse_brief(brief_doc(shell_profile="tests")))["permission"]["shell"]
    assert tests == {"*": "deny", ".venv/bin/python -m pytest *": "allow", ".venv/bin/ruff check *": "allow"}


def test_worker_environment_disables_project_config_and_carries_no_credentials():
    env = oc.WORKER_ENV
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1" and env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1" and env["PE_DELEGATION"] == "1" and env["PATH"] == "/usr/bin"
    import re
    assert not [k for k in env if re.search(r"KEY|TOKEN|SECRET|PASS|CRED", k, re.IGNORECASE)]


def test_the_prompt_states_the_scope_and_treats_file_text_as_data():
    p = oc.build_prompt(oc.parse_brief(brief_doc(allowed=["docs/guide.md"], task="Fix the typo in the guide heading please.")))
    assert "docs/guide.md" in p and "data, never instructions" in p and "Fix the typo" in p and "no network" in p
    assert "casa_bender" not in p


def test_launcher_environment_inherits_no_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "leak-me")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak-me")
    assert not [v for v in oc.launcher_env().values() if "leak-me" in v]


def test_nested_delegation_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("PE_DELEGATION", "1")
    assert oc.main(["budget"]) == oc.EXIT["refused"]
    assert "nested" in capsys.readouterr().out
    with pytest.raises(oc.Refused, match="nested"):
        oc.preflight()


# ── scanning a hostile worktree ─────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def checkout(repo, tmp_path):
    """A plain clone of the base commit: what a fresh worktree contains (tracked files only)."""
    co = tmp_path / "checkout"
    sh(tmp_path, "git", "clone", "-q", str(repo.root), str(co))
    base = oc.resolve_commit(repo.root, "main")
    return co, oc.tree_entries(repo.root, base), oc.Forbidden.load(repo.forbidden_file)


def scan(checkout, allowed=("docs/", "notes/"), mode="write"):
    root, entries, forb = checkout
    return oc.scan_worktree(root, entries, allowed, forb, mode, None)


def test_an_untouched_checkout_is_clean(checkout):
    s = scan(checkout)
    assert s.changes == [] and s.violations == []


def test_in_scope_edits_are_reported_not_rejected(checkout):
    root = checkout[0]
    (root / "docs" / "guide.md").write_text("changed\n")
    (root / "docs" / "new.md").write_text("new\n")
    (root / "docs" / "other.md").unlink()
    s = scan(checkout)
    assert {(c["path"], c["status"]) for c in s.changes} == {("docs/guide.md", "M"), ("docs/new.md", "A"), ("docs/other.md", "D")}
    assert s.violations == []


@pytest.mark.parametrize("make,expect", [
    (lambda r: (r / "README.md").write_text("x"), "README.md: outside the allowed paths"),
    (lambda r: (r / "evil.py").write_text("x"), "evil.py: outside"),
    (lambda r: (r / "casa_bender.py").write_text("x"), "forbidden pattern"),
    (lambda r: (r / "docs" / ".env").write_text("x"), "forbidden pattern"),
    (lambda r: (r / "docs" / ".gitattributes").write_text("* filter=x"), "forbidden pattern"),
    (lambda r: (r / "docs" / "x.pem").write_text("x"), "forbidden pattern"),
    (lambda r: (r / "README.md").unlink(), "README.md: outside"),
    (lambda r: os.symlink("/etc/passwd", r / "docs" / "link"), "symlinks may not be created"),
    (lambda r: os.symlink("guide.md", r / "docs" / "rel-link"), "symlinks may not be created"),
    (lambda r: os.chmod(r / "docs" / "guide.md", 0o755), "executable bit changed"),
    (lambda r: ((r / "docs" / "run.sh").write_text("x"), os.chmod(r / "docs" / "run.sh", 0o755)), "new files may not be executable"),
    (lambda r: os.mkfifo(r / "docs" / "pipe"), "special file"),
    (lambda r: ((r / "docs" / "sub").mkdir(), (r / "docs" / "sub" / ".git").mkdir()), "nested git metadata"),
    (lambda r: ((r / "docs" / "sub").mkdir(), (r / "docs" / "sub" / ".git").write_text("gitdir: /x")), "nested git metadata"),
    (lambda r: (r / "docs" / "big.md").write_bytes(b"x" * (oc.MAX_FILE_BYTES + 1)), "larger than"),
    (lambda r: (r / "docs" / "a\nb.md").write_text("x"), "path is not acceptable"),
    (lambda r: ((r / "docs" / ".claude").mkdir(), (r / "docs" / ".claude" / "x").write_text("x")), "not acceptable"),
    (lambda r: (r / "opencode.jsonc").write_text("{}"), "opencode.jsonc"),
])
def test_scan_rejects_every_escape_route(checkout, make, expect):
    make(checkout[0])
    s = scan(checkout)
    assert any(expect in v for v in s.violations), (expect, s.violations)


def test_a_symlink_replacing_a_tracked_file_is_rejected(checkout):
    root = checkout[0]
    (root / "docs" / "guide.md").unlink()
    os.symlink("/etc/passwd", root / "docs" / "guide.md")
    assert any("type changed" in v for v in scan(checkout).violations)


def test_content_changes_with_unchanged_size_and_mtime_are_still_seen(checkout):
    root = checkout[0]
    p = root / "docs" / "other.md"
    st = p.stat()
    p.write_text("OTHER\n")                                           # same length as "other\n"
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert [c["path"] for c in scan(checkout).changes] == ["docs/other.md"]


def test_read_only_tasks_may_not_change_anything(checkout):
    (checkout[0] / "docs" / "guide.md").write_text("changed\n")
    assert any("read-only task changed" in v for v in scan(checkout, allowed=(), mode="read-only").violations)


def test_the_worktree_git_pointer_is_checked(checkout):
    root, entries, forb = checkout
    pointer = b"gitdir: /somewhere\n"
    s = oc.scan_worktree(root, entries, ("docs/",), forb, "write", pointer)       # a normal checkout's .git is a directory
    assert any(".git: the worktree's git pointer was changed" in v for v in s.violations)


def test_too_many_files_or_bytes(checkout):
    root = checkout[0]
    for i in range(oc.MAX_CHANGED_FILES + 1):
        (root / "docs" / f"f{i}.md").write_text("x")
    assert any("more than" in v and "files" in v for v in scan(checkout).violations)


# ── claims ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def claim(repo, slug, allowed, **kw):
    c = {"slug": slug, "mode": "write", "state": "active", "pid": os.getpid(), "pid_start": oc.proc_start(os.getpid()),
         "allowed": allowed, "branch": f"oc/{slug}", "max_cost_usd": 0.25}
    c.update(kw)
    repo.ensure_dirs()
    oc.Claims(repo.claims_dir).write(c)


def test_overlapping_active_claims_collide(repo):
    base = oc.resolve_commit(repo.root, "main")
    claim(repo, "first-task", ["docs/"])
    claims = oc.Claims(repo.claims_dir)
    assert claims.conflicts(repo, ("docs/guide.md",), base)
    assert claims.conflicts(repo, ("docs/",), base)
    assert not claims.conflicts(repo, ("notes/",), base)
    assert not claims.conflicts(repo, ("README.md",), base)


def test_read_only_released_and_dead_merged_claims_do_not_block(repo):
    base = oc.resolve_commit(repo.root, "main")
    claims = oc.Claims(repo.claims_dir)
    claim(repo, "reader", ["docs/"], mode="read-only")
    assert not claims.conflicts(repo, ("docs/",), base)
    claim(repo, "released-one", ["docs/"], state="released")
    assert not claims.conflicts(repo, ("docs/",), base)
    claim(repo, "done-merged", ["docs/"], state="done", branch="main")                        # finished, and its branch is merged into base
    assert not claims.conflicts(repo, ("docs/",), base)


def test_unmerged_finished_or_crashed_work_keeps_its_paths(repo):
    claims = oc.Claims(repo.claims_dir)
    sh(repo.root, "git", "branch", "oc/unmerged")
    sh(repo.root, "git", "checkout", "-q", "oc/unmerged")
    (repo.root / "docs" / "guide.md").write_text("worker output\n")
    sh(repo.root, "git", "commit", "-qam", "worker")
    sh(repo.root, "git", "checkout", "-q", "main")
    base = oc.resolve_commit(repo.root, "main")
    claim(repo, "unmerged", ["docs/guide.md"], state="done", pid=2 ** 22 + 7, pid_start="1", branch="oc/unmerged")
    assert claims.conflicts(repo, ("docs/",), base)                           # finished but nobody merged it
    claim(repo, "unmerged", ["docs/guide.md"], state="active", pid=2 ** 22 + 7, pid_start="1", branch="oc/unmerged")
    assert claims.conflicts(repo, ("docs/guide.md",), base)                   # crashed: still blocks until released
    oc.cmd_release(repo, "unmerged")
    assert not claims.conflicts(repo, ("docs/guide.md",), base)


def test_a_corrupt_claim_blocks_everything(repo):
    repo.ensure_dirs()
    (repo.claims_dir / "broken.json").write_text("{not json")
    assert oc.Claims(repo.claims_dir).conflicts(repo, ("docs/guide.md",), oc.resolve_commit(repo.root, "main"))


def test_running_tasks_reserve_their_budget(repo):
    claim(repo, "one-task", ["docs/"], max_cost_usd=0.4)
    claim(repo, "two-task", ["notes/"], max_cost_usd=0.2, pid=2 ** 22 + 7, pid_start="1")      # dead: no reservation
    assert oc.Claims(repo.claims_dir).reservations() == pytest.approx(0.4)


# ── the run flow (real sandbox and OpenCode, fake upstream) ─────────────────────────────────────────────────────────

def write_brief(tmp_path, **over):
    p = tmp_path / f"{over.get('slug', 't-docs-edit')}.json"
    p.write_text(json.dumps(brief_doc(**over)))
    return str(p)


EDIT_GUIDE = [{"tool": {"name": "edit", "args": {"path": "{WT}/docs/guide.md", "oldString": "Original guide text.", "newString": "Better guide text."}}},
              {"text": "I improved the guide."}]


def script_for(repo, slug, steps):
    wt = str(repo.worktrees / f"oc-{slug}")
    return json.loads(json.dumps(steps).replace("{WT}", wt))


@needs_sandbox
def test_dry_run_creates_nothing(repo, env, tmp_path):
    r = oc.cmd_run(repo, write_brief(tmp_path), dry_run=True)
    assert r["status"] == "ok" and r["dry_run"] and r["budget"]["ok"]
    assert not (repo.worktrees / "oc-t-docs-edit").exists()
    assert sh(repo.root, "git", "branch", "--list", "oc/*") == ""


@needs_sandbox
def test_a_delegated_doc_edit_is_committed_on_its_branch_and_never_merged(repo, env, tmp_path):
    env.script = script_for(repo, "t-docs-edit", EDIT_GUIDE)
    before = sh(repo.root, "git", "rev-parse", "main").strip()
    r = oc.cmd_run(repo, write_brief(tmp_path), dry_run=False)
    assert r["status"] == "ok", r
    assert r["changed"] == [{"path": "docs/guide.md", "status": "M"}] and r["violations"] == []
    assert sh(repo.root, "git", "rev-parse", "main").strip() == before                       # never merged
    assert sh(repo.root, "git", "diff", "--name-only", "main..oc/t-docs-edit").split() == ["docs/guide.md"]
    assert "Better guide text." in sh(repo.root, "git", "show", "oc/t-docs-edit:docs/guide.md")
    assert (repo.root / "docs" / "guide.md").read_text() == FILES["docs/guide.md"]            # the main checkout is untouched
    assert r["cost"]["exact_cost_usd"] > 0 and r["cost"]["estimated_cost_usd"] == 0 and r["cost"]["exactness"] == "exact"
    assert r["tool_calls"].get("edit") == 1 and r["permission_denials"] == 0
    assert "worker_summary_untrusted" in r and r["head_commit"]
    assert oc.Ledger(repo.ledger_path).today()["exact_usd"] == pytest.approx(r["cost"]["exact_cost_usd"])
    assert json.loads((repo.claims_dir / "t-docs-edit.json").read_text())["state"] == "done"
    assert KEY not in json.dumps(r) and KEY not in (repo.state_root / "t-docs-edit" / "worker.out").read_text()


@needs_sandbox
def test_read_only_investigation_changes_nothing(repo, env, tmp_path):
    env.script = [{"tool": {"name": "read", "args": {"path": "{WT}/README.md"}}}, {"text": "The project is a demo."}]
    env.script = script_for(repo, "t-read", env.script)
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-read", mode="read-only", allowed=[], task="Read the README and summarise what the project is."), False)
    assert r["status"] == "ok" and r["changed"] == [] and "demo" in r["worker_summary_untrusted"]
    assert r["tool_calls"] == {"read": 1} and "head_commit" not in r
    assert r["worktree_removed"] is True and not (repo.worktrees / "oc-t-read").exists()      # a read-only task cleans up after itself
    assert sh(repo.root, "git", "branch", "--list", "oc/t-read").strip() == ""


@needs_sandbox
def test_a_forbidden_or_out_of_scope_brief_is_refused_before_anything_is_created(repo, env, tmp_path):
    for slug, allowed, why in (("t-forb-file", ["casa_bender.py"], "forbidden pattern"), ("t-forb-dir", ["tests/"], "not delegable"),
                               ("t-missing", ["docs/nope.md"], "regular file")):
        r = oc.cmd_run(repo, write_brief(tmp_path, slug=slug, allowed=allowed), dry_run=False)
        assert r["status"] == "refused" and why in r["reason"], r
        assert not (repo.worktrees / f"oc-{slug}").exists()
    assert sh(repo.root, "git", "branch", "--list", "oc/*") == ""


@needs_sandbox
def test_an_escape_that_got_past_the_sandbox_is_still_rejected_by_the_scan(repo, env, tmp_path, monkeypatch):
    """Simulates a sandbox failure: after the worker 'runs', files outside scope appear. The result must be rejected, nothing
    committed, and the claim left as failed."""
    env.script = script_for(repo, "t-escape", EDIT_GUIDE)
    real = oc.run_sandboxed

    def escaping(argv, timeout_s, out_dir, tag):
        out = real(argv, timeout_s, out_dir, tag)
        wt = repo.worktrees / "oc-t-escape"
        (wt / "casa_bender.py").write_text("SAFETY = False\n")
        os.symlink("/etc/passwd", wt / "docs" / "leak")
        return out
    monkeypatch.setattr(oc, "run_sandboxed", escaping)
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-escape"), dry_run=False)
    assert r["status"] == "scope_violation", r
    assert any("casa_bender.py" in v and "forbidden" in v for v in r["violations"]) and any("symlink" in v for v in r["violations"])
    assert sh(repo.root, "git", "rev-list", "--count", "main..oc/t-escape").strip() == "0"      # nothing was committed
    assert json.loads((repo.claims_dir / "t-escape.json").read_text())["state"] == "failed"
    assert "head_commit" not in r


@needs_sandbox
def test_overlapping_tasks_collide_and_disjoint_ones_run_concurrently(repo, env, tmp_path):
    env.script = script_for(repo, "t-first", [{"sleep": 4, "tool": {"name": "edit", "args": {
        "path": "{WT}/docs/guide.md", "oldString": "Original guide text.", "newString": "First."}}}, {"text": "done"}])
    results = {}
    first = threading.Thread(target=lambda: results.update(first=oc.cmd_run(repo, write_brief(tmp_path, slug="t-first"), False)))
    first.start()
    deadline = time.time() + 30
    while not (repo.claims_dir / "t-first.json").exists() and time.time() < deadline:
        time.sleep(0.1)
    clash = oc.cmd_run(repo, write_brief(tmp_path, slug="t-clash", allowed=["docs/guide.md"]), False)
    assert clash["status"] == "refused" and "claim collision" in clash["reason"] and "t-first" in clash["reason"]
    assert not (repo.worktrees / "oc-t-clash").exists()
    first.join(timeout=120)
    assert results["first"]["status"] == "ok", results["first"]
    again = oc.cmd_run(repo, write_brief(tmp_path, slug="t-after", allowed=["docs/guide.md"]), False)
    assert again["status"] == "refused" and "claim collision" in again["reason"]      # finished but unmerged work still holds its path
    oc.cmd_release(repo, "t-first")
    sh(repo.root, "git", "merge", "-q", "--no-edit", "oc/t-first")                    # the parent integrates it...
    assert oc.Claims(repo.claims_dir).conflicts(repo, ("docs/guide.md",), oc.resolve_commit(repo.root, "main")) == []


@needs_sandbox
def test_uncommitted_work_in_the_main_checkout_blocks_overlapping_tasks(repo, env, tmp_path):
    (repo.root / "docs" / "guide.md").write_text("parent is editing this\n")
    r = oc.cmd_run(repo, write_brief(tmp_path), dry_run=False)
    assert r["status"] == "refused" and "uncommitted changes in the main checkout" in r["reason"]


@needs_sandbox
def test_a_reused_slug_is_refused(repo, env, tmp_path):
    env.script = script_for(repo, "t-docs-edit", EDIT_GUIDE)
    assert oc.cmd_run(repo, write_brief(tmp_path), False)["status"] == "ok"
    again = oc.cmd_run(repo, write_brief(tmp_path), False)
    assert again["status"] == "refused" and "already used" in again["reason"]


@needs_sandbox
def test_timeout_kills_the_worker_and_the_result_is_not_committed(repo, env, tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "MIN_TIMEOUT_S", 2)
    env.script = [{"sleep": 60, "text": "never"}]
    t0 = time.time()
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-slow", timeout_s=3), False)
    assert r["status"] == "timeout" and r["worker"]["timed_out"] and time.time() - t0 < 40
    assert sh(repo.root, "git", "rev-list", "--count", "main..oc/t-slow").strip() == "0"
    assert json.loads((repo.claims_dir / "t-slow.json").read_text())["state"] == "failed"
    time.sleep(1)
    left = subprocess.run(["pgrep", "-f", "oc-t-slow"], capture_output=True, text=True, check=False).stdout.split()
    assert left == [], f"worker processes survived the timeout: {left}"


@needs_sandbox
def test_a_worker_that_fails_or_does_nothing_is_not_accepted(repo, env, tmp_path):
    env.script = [{"http_status": 500}] * 6
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-fails"), False)
    assert r["status"] in ("worker_error", "no_op"), r
    assert sh(repo.root, "git", "rev-list", "--count", "main..oc/t-fails").strip() == "0"
    oc.cmd_release(repo, "t-fails")                       # a failed task holds its paths until someone releases it
    env.script = [{"text": "I decided to change nothing."}]
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-noop"), False)
    assert r["status"] == "no_op"


@needs_sandbox
def test_budget_unknown_or_exhausted_refuses_admission(repo, env, tmp_path, monkeypatch):
    env.usage_daily = 1.9
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-broke"), False)
    assert r["status"] == "refused" and "budget" in r["reason"] and not (repo.worktrees / "oc-t-broke").exists()
    env.usage_daily = None
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-unknown"), False)
    assert r["status"] == "refused" and "usage_daily" in r["reason"]
    env.usage_daily = 0.0
    monkeypatch.setenv("OC_UPSTREAM_BASE", "http://127.0.0.1:9")
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-offline"), False)
    assert r["status"] == "refused" and "cannot determine spend" in r["reason"]


@needs_sandbox
def test_acceptance_runs_in_a_sandbox_with_a_fixed_command_set(repo, env, tmp_path):
    if not (repo.root / ".venv").exists():
        pytest.skip("needs the project venv")
    env.script = script_for(repo, "t-accept", [{"tool": {"name": "edit", "args": {
        "path": "{WT}/app/tool.py", "oldString": "return 1", "newString": "return 2"}}}, {"text": "done"}])
    brief = write_brief(tmp_path, slug="t-accept", allowed=["app/tool.py"], task="Change the tool function to return 2 instead of 1.",
                        acceptance=[{"check": "pytest", "paths": ["tests/test_demo.py"]}, {"check": "ruff"}])
    r = oc.cmd_run(repo, brief, False)
    assert r["status"] == "ok", r
    assert [a["check"] for a in r["acceptance"]] == ["pytest", "ruff"] and all(a["ok"] for a in r["acceptance"])


@needs_sandbox
def test_failing_acceptance_blocks_the_commit(repo, env, tmp_path):
    if not (repo.root / ".venv").exists():
        pytest.skip("needs the project venv")
    env.script = script_for(repo, "t-badtest", [{"tool": {"name": "edit", "args": {
        "path": "{WT}/app/tool.py", "oldString": "return 1", "newString": "return ("}}}, {"text": "done"}])
    brief = write_brief(tmp_path, slug="t-badtest", allowed=["app/tool.py"], task="Change the tool function in a way that breaks it.",
                        acceptance=[{"check": "ruff"}])
    r = oc.cmd_run(repo, brief, False)
    assert r["status"] == "acceptance_failed" and not r["acceptance"][0]["ok"]
    assert sh(repo.root, "git", "rev-list", "--count", "main..oc/t-badtest").strip() == "0"


# ── the isolation self-test, and proof that it can fail ─────────────────────────────────────────────────────────────

@needs_sandbox
def test_selftest_passes_on_the_real_repo():
    result = oc_selftest.run_selftest(oc.Repo(REAL_REPO))
    assert result["status"] == "ok", result["failed"]
    assert result["checks_total"] > 100
    assert sh(REAL_REPO, "git", "branch", "--list", "oc/selftest-*") == ""            # it cleaned up after itself


def weaken(monkeypatch, tmp_path, transform):
    real = oc.bwrap_argv

    def patched(spec):
        return transform(real(spec))
    monkeypatch.setattr(oc, "bwrap_argv", patched)


def failed_ids(result):
    return {c["id"] for c in result["failed"]}


def share_net(argv):
    i = argv.index("--unshare-all")
    return argv[:i + 1] + ["--share-net"] + argv[i + 1:]


def make_worktree_writable(argv):
    out = list(argv)
    for i, x in enumerate(out[:-2]):
        if x == "--ro-bind" and out[i + 1] == out[i + 2] and os.path.basename(out[i + 1]).startswith("oc-selftest-"):
            out[i] = "--bind"
    return out


@needs_sandbox
@pytest.mark.parametrize("name,transform,expected", [
    ("shared network", share_net, "no-route:host-loopback-sentinel"),
    ("whole worktree writable", make_worktree_writable, "read-only:"),
])
def test_negative_controls_the_selftest_notices_a_weakened_sandbox(monkeypatch, tmp_path, name, transform, expected):
    weaken(monkeypatch, tmp_path, transform)
    result = oc_selftest.run_selftest(oc.Repo(REAL_REPO))
    assert result["status"] == "sandbox_error", f"{name}: the weakened sandbox was not detected"
    assert any(i.startswith(expected) for i in failed_ids(result)), (name, sorted(failed_ids(result)))


@needs_sandbox
def test_negative_control_a_visible_ssh_directory_is_detected(monkeypatch, tmp_path):
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.mkdir()
    (fake_ssh / "id_ed25519").write_text("NOT A REAL KEY\n")
    home = os.environ["HOME"]
    weaken(monkeypatch, tmp_path, lambda a: a[:a.index("--chdir")] + ["--ro-bind", str(fake_ssh), f"{home}/.ssh"] + a[a.index("--chdir"):])
    result = oc_selftest.run_selftest(oc.Repo(REAL_REPO))
    assert any(i.startswith("unreadable:") and i.endswith(".ssh") for i in failed_ids(result)), sorted(failed_ids(result))


@needs_sandbox
def test_negative_control_a_credential_in_the_environment_is_detected(monkeypatch, tmp_path):
    weaken(monkeypatch, tmp_path, lambda a: a[:a.index("--chdir")] + ["--setenv", "OPENROUTER_API_KEY", "canary"] + a[a.index("--chdir"):])
    result = oc_selftest.run_selftest(oc.Repo(REAL_REPO))
    assert "environment-has-no-credential-variables" in failed_ids(result)


@needs_sandbox
def test_negative_control_a_reachable_docker_socket_is_detected(monkeypatch, tmp_path):
    if not os.path.exists("/var/run/docker.sock"):
        pytest.skip("no docker socket on this host")
    weaken(monkeypatch, tmp_path, lambda a: a[:a.index("--chdir")] + ["--bind", "/var/run/docker.sock", "/var/run/docker.sock"] + a[a.index("--chdir"):])
    result = oc_selftest.run_selftest(oc.Repo(REAL_REPO))
    assert any("docker.sock" in i for i in failed_ids(result)), sorted(failed_ids(result))


def test_launcher_script_is_executable_and_stdlib_only():
    launcher = SCRIPTS / "oc_delegate.sh"
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert "-E -s" in launcher.read_text()


# ── review fixes: accounting, atomic admission, claims, scan robustness, hardening ───────────────────────────────────

def test_failed_and_crashed_claims_hold_their_paths_until_released(repo):
    base = oc.resolve_commit(repo.root, "main")
    claims = oc.Claims(repo.claims_dir)
    sh(repo.root, "git", "branch", "oc/failed-one")                                     # tip == base: looks "merged", but the work was rejected
    claim(repo, "failed-one", ["docs/"], state="failed", branch="oc/failed-one")
    assert claims.conflicts(repo, ("docs/guide.md",), base)
    sh(repo.root, "git", "branch", "oc/crashed-one")
    claim(repo, "crashed-one", ["templates-x/"], state="active", pid=2 ** 22 + 7, pid_start="1", branch="oc/crashed-one")
    assert "crashed" in claims.conflicts(repo, ("templates-x/f",), base)[0]
    oc.cmd_release(repo, "failed-one")
    oc.cmd_release(repo, "crashed-one")
    assert claims.conflicts(repo, ("docs/guide.md", "templates-x/f"), base) == []


@pytest.mark.parametrize("slug", ["abc\n", "abc ", "ABC", "a" * 41, "-abc"])
def test_slugs_are_matched_whole(slug):
    refused(brief_doc(slug=slug), "slug")


def test_a_malformed_ledger_record_refuses_admission(tmp_path):
    ledger = oc.Ledger(tmp_path / "l.jsonl")
    ledger.append({"kind": "task", "date": oc.utc_today(), "exact_cost_usd": 0.5, "estimated_cost_usd": 0})
    assert ledger.today()["exact_usd"] == 0.5
    today = oc.utc_today()
    for bad in ("{torn", "[1, 2]", json.dumps({"kind": "task", "date": today, "exact_cost_usd": "NaN"}),
                json.dumps({"kind": "task", "date": today, "exact_cost_usd": -1}), json.dumps({"kind": "task", "date": today})):
        (tmp_path / "l.jsonl").write_text((tmp_path / "l.jsonl").read_text().splitlines()[0] + "\n" + bad + "\n")
        with pytest.raises(oc.Refused, match="ledger is corrupt"):
            ledger.today()


def test_budget_command_reports_a_corrupt_ledger_as_unknown(repo, env):
    repo.ensure_dirs()
    repo.ledger_path.write_text("{torn\n")
    out = oc.cmd_budget(repo)
    assert out["status"] == "unknown" and "ledger is corrupt" in out["reason"]


def test_a_response_that_reached_the_upstream_is_charged_even_if_the_client_vanished():
    m = oc.Meter(1.0, 10)
    assert m.admit() is None
    m.finish(None, True, 4000, 16384)                    # sent, answer lost or stream cut: worst-case estimate, never zero
    snap = m.snapshot()
    assert snap["estimated_cost_usd"] > 0 and snap["exact_cost_usd"] == 0 and snap["upstream_errors"] == 0
    assert m.admit() is None
    m.finish(None, False, 4000, 16384)                   # the upstream said no: not billable
    assert m.snapshot()["upstream_errors"] == 1 and m.snapshot()["estimated_cost_usd"] == snap["estimated_cost_usd"]


def test_drain_cuts_open_upstream_calls_and_accounts_for_them(tmp_path, env):
    env.script = [{"sleep": 20, "text": "late"}]
    rig = ProxyRig(tmp_path, oc.Meter(1.0, 10))
    try:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=rig.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS,
                                                                                "tools": [{"type": "function", "function": {"name": "x"}}]})))
        t.start()
        deadline = time.time() + 10
        while not env.log and time.time() < deadline:                   # wait until the UPSTREAM has the request (it then sleeps)
            time.sleep(0.05)
        assert env.log and not rig.meter.idle(), "the request never reached the upstream"
        assert rig.server.drain(10.0) is True
        t.join(timeout=10)
        assert out["r"][0] == 502                                        # the client is told, not left hanging
        snap = rig.meter.snapshot()
        assert snap["estimated_cost_usd"] > 0, snap                      # the cost of the call that was cut is on the books
        assert rig.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS})[0] == 503     # and nothing new is accepted
    finally:
        rig.close()


def test_every_token_limit_spelling_is_clamped_and_only_function_tools_pass(proxy, env):
    status, _ = proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "max_completion_tokens": 1, "max_tokens": 10**8,
                                          "tools": [{"type": "function", "function": {"name": "f"}}, {"type": "openrouter:web_search"}]})
    assert status == 200
    (rec,) = env.log
    assert rec["max"] == 1 or rec["max"] == oc.MAX_OUTPUT_TOKENS
    assert rec["tools_types"] == ["function"] if "tools_types" in rec else True
    assert any("non-function tools" in d for d in proxy.meter.denied)
    env.log.clear()
    proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS, "tools": [{"type": "openrouter:web_search"}], "tool_choice": "auto"})
    assert "tools" not in env.log[0]["keys"] and "tool_choice" not in env.log[0]["keys"]


def test_unreadable_directories_and_files_are_violations_not_blind_spots(checkout):
    root = checkout[0]
    (root / "docs" / "locked").mkdir()
    (root / "docs" / "locked" / "evil.sh").write_text("x")
    os.chmod(root / "docs" / "locked", 0o000)
    (root / "docs" / "sealed.md").write_text("x")
    os.chmod(root / "docs" / "sealed.md", 0o000)
    try:
        s = scan(checkout)
    finally:
        os.chmod(root / "docs" / "locked", 0o700)
        os.chmod(root / "docs" / "sealed.md", 0o600)
    assert any("unreadable directory" in v for v in s.violations), s.violations
    assert any("unreadable file" in v for v in s.violations), s.violations


def test_event_parsing_survives_any_worker_output(tmp_path):
    p = tmp_path / "events"
    p.write_text('[1]\n"x"\n{"type":"tool_use","part":"not a dict"}\n{"type":"text","part":{"text":"ok"}}\n{"type":"tool_use","part":{"tool":"read","state":5}}\nnot json\n')
    out = oc.summarize_events(p, KEY)
    assert out["worker_summary_untrusted"] == "ok" and out["unparsed_lines"] >= 3


def test_the_worker_environment_writes_no_bytecode():
    assert oc.WORKER_ENV["PYTHONDONTWRITEBYTECODE"] == "1"


@needs_sandbox
def test_concurrent_admission_cannot_overspend_the_daily_budget(repo, env, tmp_path):
    """Two disjoint tasks start together with headroom for only one cost cap: exactly one may be admitted."""
    env.usage_daily = 1.70                               # headroom 0.30; each task reserves 0.25
    env.script = [{"sleep": 4, "text": "one"}]
    results = {}
    docs = [("t-race-a", ["docs/guide.md"]), ("t-race-b", ["docs/other.md"])]
    threads = [threading.Thread(target=lambda s=slug, a=allowed: results.update({s: oc.cmd_run(
        repo, write_brief(tmp_path, slug=s, allowed=a), False)})) for slug, allowed in docs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    statuses = sorted(r["status"] for r in results.values())
    admitted = [r for r in results.values() if r["status"] != "refused"]
    refused_for_budget = [r for r in results.values() if r["status"] == "refused" and "budget" in r["reason"]]
    assert len(admitted) == 1 and len(refused_for_budget) == 1, statuses


@needs_sandbox
def test_the_same_slug_cannot_be_used_twice_even_after_a_read_only_task_cleaned_up(repo, env, tmp_path):
    env.script = [{"text": "nothing to report"}]
    brief = write_brief(tmp_path, slug="t-readonce", mode="read-only", allowed=[], task="Read the README and say what the project is called.")
    assert oc.cmd_run(repo, brief, False)["status"] == "ok"
    again = oc.cmd_run(repo, brief, False)
    assert again["status"] == "refused" and "already used" in again["reason"]


@needs_sandbox
def test_a_failed_run_keeps_its_claim_so_overlapping_work_must_wait_for_release(repo, env, tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "MIN_TIMEOUT_S", 2)
    env.script = [{"sleep": 30, "text": "never"}]
    assert oc.cmd_run(repo, write_brief(tmp_path, slug="t-timeout-a", timeout_s=3), False)["status"] == "timeout"
    env.script = script_for(repo, "t-after-fail", EDIT_GUIDE)
    blocked = oc.cmd_run(repo, write_brief(tmp_path, slug="t-after-fail"), False)
    assert blocked["status"] == "refused" and "claim collision" in blocked["reason"]
    oc.cmd_release(repo, "t-timeout-a")
    assert oc.cmd_run(repo, write_brief(tmp_path, slug="t-after-fail"), False)["status"] == "ok"


def test_a_claim_of_the_wrong_shape_is_a_wildcard_that_blocks(repo):
    repo.ensure_dirs()
    base = oc.resolve_commit(repo.root, "main")
    for i, body in enumerate(["{}", '{"allowed": []}', "[]", "5", '{"slug": "x", "mode": "write", "state": "active", "allowed": [1]}',
                              '{"slug": "x", "mode": "root", "state": "active", "allowed": []}',
                              '{"slug": "x", "mode": "write", "state": "weird", "allowed": []}']):
        for f in repo.claims_dir.glob("*.json"):
            f.unlink()
        (repo.claims_dir / f"odd-{i}.json").write_text(body)
        assert oc.Claims(repo.claims_dir).conflicts(repo, ("docs/guide.md",), base), body


@pytest.mark.parametrize("usage", [{}, {"cost": 0.0}, {"prompt_tokens": 1}, {"prompt_tokens": -1, "completion_tokens": 1},
                                   {"prompt_tokens": float("nan"), "completion_tokens": 1}, {"prompt_tokens": 1, "completion_tokens": 1, "cost": -0.5},
                                   {"prompt_tokens": 1, "completion_tokens": 1, "cost": float("inf")}, {"prompt_tokens": True, "completion_tokens": 1},
                                   "oops", ["x"]])
def test_incomplete_or_malformed_usage_is_charged_as_a_worst_case_estimate(usage):
    m = oc.Meter(1.0, 10)
    assert m.admit(0.01) is None
    m.finish(usage, True, 4000, 16384)
    snap = m.snapshot()
    assert snap["exact_cost_usd"] == 0 and snap["estimated_cost_usd"] > 0.05, snap


def test_complete_usage_is_trusted():
    m = oc.Meter(1.0, 10)
    m.admit(0.01)
    m.finish({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002}, True, 40, 100)
    assert m.snapshot()["exact_cost_usd"] == 0.0002 and m.snapshot()["estimated_cost_usd"] == 0


def test_stranded_requests_are_charged_at_their_worst_case():
    m = oc.Meter(1.0, 10)
    m.admit(m.worst_case(4000, 16384))
    m.admit(m.worst_case(8000, 16384))
    total = m.charge_inflight()
    assert total == pytest.approx(m.worst_case(4000, 16384) + m.worst_case(8000, 16384)) and m.snapshot()["estimated_cost_usd"] == pytest.approx(total)


@needs_sandbox
def test_a_failed_drain_blocks_completion_and_charges_the_stranded_calls(repo, env, tmp_path, monkeypatch):
    env.script = script_for(repo, "t-nodrain", EDIT_GUIDE)
    real_admit = oc.Meter.admit

    def leaky(self, worst=0.0):
        out = real_admit(self, worst)
        if out is None:
            self.admit(0.05)                      # a phantom in-flight call that never finishes
        return out
    monkeypatch.setattr(oc.Meter, "admit", leaky)
    monkeypatch.setattr(oc.ProxyServer, "drain", lambda self, timeout=10.0: False)
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-nodrain"), False)
    assert r["status"] == "sandbox_error" and "could not be cancelled" in r["reason"], r
    assert sh(repo.root, "git", "rev-list", "--count", "main..oc/t-nodrain").strip() == "0"
    assert oc.Ledger(repo.ledger_path).today()["estimated_usd"] > 0


# ── round three: accounting under failure, bookkeeping honesty, forbidden coverage ────────────────────────────────

def test_admission_counts_calls_already_in_flight_at_their_worst_case():
    m = oc.Meter(0.10, 10)
    assert m.admit(0.06) is None and m.admit(0.06) is None          # nothing is spent yet, so both fit
    refusal = m.admit(0.06)
    assert refusal and "in flight" in refusal                        # 0.12 of worst-case exposure already exceeds the 0.10 cap
    m.finish({"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}, True, 10, 10, 0.06)
    assert m.admit(0.06) is None                                     # one finished: its own worst case came off, not another's


def test_finishing_removes_that_requests_own_worst_case():
    m = oc.Meter(1.0, 10)
    m.admit(0.01)
    m.admit(0.09)
    m.finish(None, False, 10, 10, 0.01)
    assert m.charge_inflight() == pytest.approx(0.09)                # the expensive one is still booked, not the cheap one


def test_any_failure_after_a_request_is_sent_still_counts_as_spend(proxy, env, monkeypatch):
    def broken(self, *a, **k):
        raise ValueError("I/O operation on closed file")             # not an OSError
    monkeypatch.setattr(oc.ProxyHandler, "send_response", broken)
    proxy.call("POST", CHAT, {"model": oc.UPSTREAM_MODEL, "messages": MSGS})
    snap = proxy.meter.snapshot()
    assert snap["estimated_cost_usd"] > 0 and snap["upstream_errors"] == 0, snap


@pytest.mark.parametrize("name", ["OC_PRICE_IN_PER_M", "OC_PRICE_OUT_PER_M"])
@pytest.mark.parametrize("value", ["0", "-5", "nan", "junk", "0.0001"])
def test_estimate_prices_can_be_raised_but_never_lowered(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    m = oc.Meter(1.0, 10)
    assert m.price_in >= 1.0 and m.price_out >= 4.0
    monkeypatch.setenv(name, "50")
    assert getattr(oc.Meter(1.0, 10), "price_in" if "IN" in name else "price_out") == 50.0


def test_ledger_strictness_covers_encoding_kind_and_date(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = oc.Ledger(path)
    path.write_bytes(b"\xff\xfe not utf-8\n")
    with pytest.raises(oc.Refused, match="ledger is corrupt"):
        ledger.today()
    for bad in ({"kind": "Task", "date": oc.utc_today(), "exact_cost_usd": 1}, {"kind": "task", "exact_cost_usd": 1},
                {"kind": "task", "date": 5, "exact_cost_usd": 1}, {"date": oc.utc_today(), "exact_cost_usd": 1}):
        path.write_text(json.dumps(bad) + "\n")
        with pytest.raises(oc.Refused, match="ledger is corrupt"):
            ledger.today()


@pytest.mark.parametrize("cost,pid", [("0.5", 1), (None, 1), (-1, 1), (float("nan"), 1), (True, 1), (0.25, "x"), (0.25, True)])
def test_claims_with_bad_cost_or_pid_are_wildcards(repo, cost, pid):
    repo.ensure_dirs()
    body = {"slug": "bad-one", "mode": "write", "state": "active", "allowed": ["docs/"], "max_cost_usd": cost, "pid": pid}
    (repo.claims_dir / "bad-one.json").write_text(json.dumps(body))
    assert oc.Claims(repo.claims_dir).conflicts(repo, ("notes/",), oc.resolve_commit(repo.root, "main"))   # even a disjoint path
    assert oc.Claims(repo.claims_dir).reservations() == 0.0


def test_release_says_what_it_did(repo):
    assert oc.cmd_release(repo, "nope-nope")["status"] == "refused"                    # no such claim
    assert oc.cmd_release(repo, "bad\n")["status"] == "refused"
    claim(repo, "live-one", ["docs/"])                                                   # this very process: alive
    live = oc.cmd_release(repo, "live-one")
    assert live["status"] == "refused" and "running" in live["reason"]
    (repo.claims_dir / "broken.json").write_text("{not json")
    broken = oc.cmd_release(repo, "broken")
    assert broken["status"] == "refused" and "by hand" in broken["reason"]
    claim(repo, "crashed-two", ["docs/"], pid=2 ** 22 + 7, pid_start="1")
    assert oc.cmd_release(repo, "crashed-two")["status"] == "ok"
    assert json.loads((repo.claims_dir / "crashed-two.json").read_text())["state"] == "released"


@pytest.mark.parametrize("path", ["tests/x/pytest.toml", ".pytest.toml", "secrets/foo.txt", "a/Credentials/x.txt", "docs/Secret_Stuff/a.md",
                                  "x/.ssh/id", "CLAUDE.MD", "docs/.ENV", ".coveragerc", "keys/.aws/credentials"])
def test_forbidden_matching_is_case_insensitive_and_covers_secret_directories(path):
    assert oc.Forbidden.load(SCRIPTS / "oc_forbidden_paths.txt").match(path), path


def test_the_scan_stops_walking_at_the_entry_cap(checkout, monkeypatch):
    monkeypatch.setattr(oc, "MAX_SCANNED_ENTRIES", 40)
    root = checkout[0]
    for i in range(60):
        (root / "docs" / f"d{i}").mkdir()                              # directories count too
    s = scan(checkout)
    assert any("scan stopped" in v for v in s.violations), s.violations


def test_the_tests_profile_names_the_repo_level_venv():
    b = oc.parse_brief(brief_doc(shell_profile="tests"))
    cfg = oc.opencode_config(b, Path("/repo"))["permission"]["shell"]
    assert cfg == {"*": "deny", "/repo/.venv/bin/python -m pytest *": "allow", "/repo/.venv/bin/ruff check *": "allow"}
    assert "/repo/.venv/bin/python -m pytest" in oc.build_prompt(b, Path("/repo"))
    assert "/.venv/" not in oc.build_prompt(oc.parse_brief(brief_doc()), Path("/repo"))      # not mentioned when the profile is off


@needs_sandbox
def test_the_tests_profile_can_really_run_pytest_in_the_sandbox_and_nothing_else(repo, env, tmp_path):
    if not (repo.root / ".venv").exists():
        pytest.skip("needs the project venv")
    venv = f"{repo.root}/.venv/bin/python"
    env.script = [{"tool": {"name": "shell", "args": {"command": f"{venv} -m pytest -q tests/test_demo.py"}}},
                  {"tool": {"name": "shell", "args": {"command": "id"}}},
                  {"tool": {"name": "shell", "args": {"command": f"{venv} -m pytest -q tests/test_demo.py; id"}}},
                  {"text": "ran the test"}]
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-shell", shell_profile="tests", acceptance=[]), False)
    assert r["status"] == "no_op", r                                  # it changed nothing
    assert r["tool_calls"] == {"shell": 3} and r["tool_errors"] == 2 and r["permission_denials"] == 2
    assert "1 passed" in (repo.state_root / "t-shell" / "worker.out").read_text()


@needs_sandbox
def test_an_interrupted_run_still_records_what_was_spent_and_fails_cleanly(repo, env, tmp_path, monkeypatch):
    def boom(argv, timeout_s, out_dir, tag):
        raise RuntimeError("simulated crash while the worker runs")
    monkeypatch.setattr(oc, "run_sandboxed", boom)
    r = oc.cmd_run(repo, write_brief(tmp_path, slug="t-crash"), False)
    assert r["status"] == "sandbox_error"
    assert json.loads((repo.claims_dir / "t-crash.json").read_text())["state"] == "failed"
    rec = json.loads(repo.ledger_path.read_text().splitlines()[-1])
    assert rec["slug"] == "t-crash" and rec["interrupted"] is True


def test_terminate_group_kills_the_whole_process_group():
    import signal
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60; wait"], start_new_session=True)
    time.sleep(0.3)
    assert oc.terminate_group(proc) in (-signal.SIGTERM, 128 + signal.SIGTERM, -9, 143)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)
