#!/usr/bin/python3
"""Runs INSIDE the delegation sandbox (stdlib only) and tries to escape it. Prints one JSON document of checks.

Each check says whether the protection HELD (the attempt failed). The host side (scripts/oc_selftest.py) adds the checks that
only it can make: what the fake upstream received, whether the host sentinel was touched, what the worktree scan flags.
"""
import base64
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys

spec = json.loads(sys.argv[1])
checks = []
scanned = 0


def add(cid: str, group: str, held: bool, observed: object) -> None:
    checks.append({"id": cid, "group": group, "held": bool(held), "observed": str(observed)[:220]})


def accessible(path: str) -> str | None:
    """None if the path cannot be read; otherwise a description of what was readable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        if stat.S_ISDIR(st.st_mode):
            return f"directory listing: {os.listdir(path)[:5]}"
        with open(path, "rb") as h:
            return f"read {len(h.read(32))} bytes"
    except OSError:
        return None


# ── files: credentials, private data, host-level resources ───────────────────────────────────────────────────────────
for path in spec["deny_paths"]:
    seen = accessible(path)
    add(f"unreadable:{path}", "filesystem", seen is None, seen or "not visible or not readable")
home_parent = os.path.dirname(spec["home"])
add("home-parent-lists-only-the-user", "filesystem", sorted(os.listdir(home_parent)) == [os.path.basename(spec["home"])],
    sorted(os.listdir(home_parent)))
chain_top = spec["wt"][len(spec["home"]):].lstrip("/").split("/")[0]
add("home-lists-only-the-path-to-the-worktree", "filesystem", sorted(os.listdir(spec["home"])) == [chain_top],
    sorted(os.listdir(spec["home"])))
add("repo-lists-only-the-worktree-chain", "filesystem", sorted(os.listdir(spec["repo"])) == [".claude"], sorted(os.listdir(spec["repo"])))
wt_parent = os.path.dirname(spec["wt"])
add("worktrees-dir-lists-only-this-worktree", "filesystem", os.listdir(wt_parent) == [os.path.basename(spec["wt"])], os.listdir(wt_parent))

# ── environment and the credential itself ────────────────────────────────────────────────────────────────────────────
suspicious = [k for k in os.environ if re.search(r"KEY|TOKEN|SECRET|PASS|CRED|AUTH", k, re.IGNORECASE)]
add("environment-has-no-credential-variables", "secrets", not suspicious, suspicious or "none")
canary = base64.b64decode(spec["canary_b64"]) if spec.get("canary_b64") else b""
hits = []
if canary:
    roots = ["/sandbox/home", "/opt", "/tmp", "/run", "/etc", spec["wt"]]
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                try:
                    if os.path.islink(p) or os.path.getsize(p) > 8 * 1024 * 1024:
                        continue
                    with open(p, "rb") as h:
                        scanned += 1
                        if canary in h.read():
                            hits.append(p)
                except OSError:
                    pass
    for pid in [d for d in os.listdir("/proc") if d.isdigit()]:
        for leaf in ("environ", "cmdline"):
            try:
                with open(f"/proc/{pid}/{leaf}", "rb") as h:
                    scanned += 1
                    if canary in h.read():
                        hits.append(f"/proc/{pid}/{leaf}")
            except OSError:
                pass
    add("real-credential-is-nowhere-in-the-sandbox", "secrets", not hits, hits or f"searched {scanned} files, environments and command lines")
cfg_path = "/sandbox/home/.config/opencode/opencode.jsonc"
if os.path.exists(cfg_path):
    with open(cfg_path) as handle:
        cfg = handle.read()
    add("opencode-config-carries-only-the-placeholder", "secrets", "sandbox-has-no-credential" in cfg and not (canary and canary in cfg.encode()),
        "placeholder only")

# ── network: nothing reaches out except the loopback bridge to the proxy ─────────────────────────────────────────────
for host, port, label in spec["net_targets"]:
    try:
        socket.create_connection((host, port), timeout=2).close()
        add(f"no-route:{label}", "network", False, "CONNECTED")
    except OSError as exc:
        add(f"no-route:{label}", "network", True, type(exc).__name__)
try:
    socket.getaddrinfo("openrouter.ai", 443)
    add("no-dns", "network", False, "resolved")
except OSError as exc:
    add("no-dns", "network", True, type(exc).__name__)
for sock_path in spec["unix_sockets"]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path)
        add(f"no-socket:{sock_path}", "host-resources", False, "CONNECTED")
    except OSError as exc:
        add(f"no-socket:{sock_path}", "host-resources", True, type(exc).__name__)
    finally:
        s.close()


def http_call(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", spec["bridge_port"], timeout=30)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json", "Authorization": "Bearer sandbox-has-no-credential"})
        resp = conn.getresponse()
        return resp.status, resp.read(400).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        return -1, type(exc).__name__
    finally:
        conn.close()


chat = "/api/v1/chat/completions"
msg = [{"role": "user", "content": "ping"}]
for cid, method, path, body in (
        ("proxy-refuses-the-key-endpoint", "GET", "/api/v1/key", None),
        ("proxy-refuses-key-management", "POST", "/api/v1/keys", {"name": "x"}),
        ("proxy-refuses-credits-endpoint", "GET", "/api/v1/credits", None),
        ("proxy-refuses-other-models", "POST", chat, {"model": "openai/gpt-4o", "messages": msg}),
        ("proxy-refuses-a-fallback-model-list", "POST", chat, {"model": "openai/gpt-4o", "models": ["deepseek/deepseek-v4-flash-0731"], "messages": msg})):
    code, text = http_call(method, path, body)
    add(cid, "proxy", code == 403, f"status {code}")
if spec.get("upstream_ok"):
    code, text = http_call("POST", chat, {"model": spec["model"], "messages": msg, "max_tokens": 5})
    add("proxy-forwards-the-pinned-model", "proxy", code == 200, f"status {code}")

# ── host tools, privileges ───────────────────────────────────────────────────────────────────────────────────────────
for tool in spec["tools"]:
    found = shutil.which(tool, path="/usr/bin:/usr/sbin:/bin:/sbin")
    if not found:
        add(f"cannot-run:{tool}", "host-tools", True, "not present")
        continue
    try:
        subprocess.run([found, "--version"], capture_output=True, timeout=5, check=False)
        add(f"cannot-run:{tool}", "host-tools", False, f"{found} executed")
    except (OSError, subprocess.SubprocessError) as exc:
        add(f"cannot-run:{tool}", "host-tools", True, f"{found}: {type(exc).__name__}")
with open("/proc/self/status") as handle:
    status = handle.read()
add("no-new-privileges", "privileges", "NoNewPrivs:\t1" in status, re.search(r"NoNewPrivs:\s*(\d)", status).group(1))
add("no-capabilities", "privileges", re.search(r"CapEff:\s*0+$", status, re.MULTILINE) is not None, re.search(r"CapEff:\s*(\w+)", status).group(1))
pids = [d for d in os.listdir("/proc") if d.isdigit()]
with open("/proc/1/cmdline") as handle:
    init_cmdline = handle.read().replace("\0", " ")
add("own-pid-namespace", "privileges", len(pids) <= 12 and "init.py" in init_cmdline, f"{len(pids)} processes")

# ── git metadata: the worktree's .git pointer must not lead anywhere writable ────────────────────────────────────────
gitfile = os.path.join(spec["wt"], ".git")
pointer = ""
if os.path.isfile(gitfile):
    with open(gitfile) as handle:
        pointer = handle.read().strip().removeprefix("gitdir:").strip()
add("gitdir-target-is-not-reachable", "git-metadata", bool(pointer) and accessible(pointer) is None, pointer or "no pointer")
add("main-git-config-is-not-reachable", "git-metadata", accessible(os.path.join(spec["repo"], ".git", "config")) is None, "config")
add("main-git-hooks-are-not-reachable", "git-metadata", accessible(os.path.join(spec["repo"], ".git", "hooks")) is None, "hooks")
try:
    with open(gitfile, "a"):
        pass
    add("worktree-git-pointer-is-read-only", "git-metadata", False, "opened for append")
except OSError as exc:
    add("worktree-git-pointer-is-read-only", "git-metadata", True, type(exc).__name__)

# ── write scope: read-only except the allowed paths ─────────────────────────────────────────────────────────────────
for rel in spec["readonly_files"]:
    path = os.path.join(spec["wt"], rel)
    try:
        with open(path, "a"):
            pass
        add(f"read-only:{rel}", "write-scope", False, "opened for append")
    except OSError as exc:
        add(f"read-only:{rel}", "write-scope", True, type(exc).__name__)
for rel in spec["readonly_dirs"]:
    path = os.path.join(spec["wt"], rel, "oc-probe-new.txt")
    try:
        with open(path, "w"):
            pass
        add(f"cannot-create-in:{rel}", "write-scope", False, "created")
    except OSError as exc:
        add(f"cannot-create-in:{rel}", "write-scope", True, type(exc).__name__)
for rel in spec["allowed_files"]:
    try:
        with open(os.path.join(spec["wt"], rel), "a") as h:
            h.write("")
        add(f"can-write-allowed:{rel}", "write-scope", True, "writable as designed")
    except OSError as exc:
        add(f"can-write-allowed:{rel}", "write-scope", False, type(exc).__name__)
for rel in spec["allowed_dirs"]:
    base = os.path.join(spec["wt"], rel)
    try:
        with open(os.path.join(base, "oc-probe-new.txt"), "w") as h:
            h.write("probe file\n")
        add(f"can-create-in:{rel}", "write-scope", True, "creatable as designed")
    except OSError as exc:
        add(f"can-create-in:{rel}", "write-scope", False, type(exc).__name__)
    try:
        os.symlink("/etc/passwd", os.path.join(base, "oc-probe-link"))
        add(f"symlink-creation-in:{rel}", "write-scope", True, "creatable; the host scan must flag it")
    except OSError as exc:
        add(f"symlink-creation-in:{rel}", "write-scope", True, type(exc).__name__)
    try:
        os.link("/etc/passwd", os.path.join(base, "oc-probe-hardlink"))
        add(f"hardlink-to-host-file-in:{rel}", "write-scope", False, "created")
    except OSError as exc:
        add(f"hardlink-to-host-file-in:{rel}", "write-scope", True, type(exc).__name__)
    for rel_file in spec["readonly_files"][:1]:
        try:
            os.rename(os.path.join(spec["wt"], rel_file), os.path.join(base, "oc-probe-moved"))
            add("cannot-move-a-read-only-file-into-scope", "write-scope", False, "moved")
        except OSError as exc:
            add("cannot-move-a-read-only-file-into-scope", "write-scope", True, type(exc).__name__)

# ── recursion ────────────────────────────────────────────────────────────────────────────────────────────────────────
add("delegation-marker-is-set", "recursion", os.environ.get("PE_DELEGATION") == "1", os.environ.get("PE_DELEGATION"))
launcher = os.path.join(spec["wt"], "scripts", "oc_delegate.sh")
if os.path.exists(launcher):
    run = subprocess.run([launcher, "budget"], capture_output=True, text=True, timeout=30, check=False, env={"PATH": "/usr/bin", "PE_DELEGATION": "1"})
    add("wrapper-refuses-to-run-nested", "recursion", run.returncode != 0 and "nested" in (run.stdout + run.stderr), (run.stdout + run.stderr)[:100])

print(json.dumps({"checks": checks, "scanned": scanned}))
