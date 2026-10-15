"""Tripwire signal collectors (contract 9): one per signal, each a pure read of host state.

A collector returns a small JSON value made of hashes, counts and names. It never returns file contents, environment
values or label text beyond the ingress-route labels, and it never runs a shell: argv lists only. Each read has a
timeout (`Scanner.signal_timeout_s`, default 3 s) and the whole scan a deadline (default 10 s); a signal that raises,
times out or has not answered by the deadline makes the scan FAIL, and the monitor treats a failed scan as a pause.

Every host path is resolved under `ScanEnv.fs_root` ("/" in production), so tests run against a temp tree.
"""

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from planet_express.missions.checkpoint.provider import CheckpointProvider
from planet_express.missions.protected import ProtectedSet
from planet_express.missions.resources import canon, split

SIGNAL_TIMEOUT_S = 3.0
SCAN_DEADLINE_S = 10.0
MAX_HASH_ENTRIES = 4000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_VOLUME_ENTRIES = 200_000
MAX_TREE_ENTRIES = 50_000
MAX_INSPECT_BYTES = 8 * 1024 * 1024
MAX_CONTAINERS = 500
MAX_ROUTE_LABELS = 64
DOCKER_TIMEOUT_S = 3

Runner = Callable[[list[str], int], tuple[int, str, str]]
BoundedRunner = Callable[[list[str], int, int], tuple[int, str, str, bool]]

DEFAULT_PACKAGE_FILES = ("/var/lib/dpkg/status", "/var/log/dpkg.log", "/var/log/apt/history.log",
                         "/var/lib/dpkg", "/usr/lib/node_modules", "/usr/local/lib/node_modules",
                         "/usr/lib/python3/dist-packages", "/usr/local/lib/python3")
_NO_FILES = frozenset({"packages", "data_volumes", "reboot", "publishing", "vcs"})


class SignalError(RuntimeError):
    """A signal could not be read completely. The monitor fails closed on it."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ScanEnv:
    runner: Runner
    bounded: BoundedRunner
    provider: CheckpointProvider
    protected: ProtectedSet
    fs_root: str = "/"
    package_paths: Sequence[str] = DEFAULT_PACKAGE_FILES
    volume_paths: Sequence[tuple[str, str]] = ()          # (resource id, host path) of data volumes to watch
    clock: Callable[[], float] = time.monotonic
    deadline: float | None = None                          # absolute (clock) time the current read must finish by
    _files: list[tuple[str, str]] | None = field(default=None, repr=False)

    def real(self, path: str) -> str:
        return os.path.join(self.fs_root, path.lstrip("/")) if self.fs_root != "/" else path

    def check_time(self) -> None:
        if self.deadline is not None and self.clock() > self.deadline:
            raise SignalError("signal read ran past its deadline")

    def watched_files(self) -> list[tuple[str, str]]:
        """(resource id, host path) for every protected path to hash, and every readable cross-host system."""
        if self._files is None:
            skip = {r for d in _NO_FILES for r in self.protected.domains.get(d, ())}
            out = []
            for rid in self.protected.resources:
                _host, kind, name = split(rid)
                if kind == "path" and rid not in skip:
                    out.append((rid, name))
            for system, path in sorted(self.protected.cross_host_paths.items()):
                out.append((canon(f"system:{system}", self.protected.host), path))
            self._files = sorted(out)
        return self._files


# ── hashing ───────────────────────────────────────────────────────────────────────────────────────

def _meta(st: os.stat_result) -> str:
    return f"{stat.S_IMODE(st.st_mode):o}:{st.st_uid}:{st.st_gid}"


def _file_digest(path: str, size: int) -> str:
    if size > MAX_FILE_BYTES:
        raise SignalError("a watched file is too large to hash")
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _blind_token(real: str) -> str:
    """What a path Core is not permitted to read looks like: a stat signature, so a replaced or touched file still
    shows as a change, but its CONTENT is not watched. The baseline names every blind resource and a mission does not
    start over one unless the operator accepted it."""
    try:
        st = os.lstat(real)
    except OSError:
        return "blind:denied"
    return f"blind:{stat.S_IFMT(st.st_mode):o}:{st.st_size}:{st.st_mtime_ns}:{st.st_ino}:{_meta(st)}"


def hash_path(env: ScanEnv, path: str) -> str:
    """A digest of a file, symlink, special file or directory tree, without following symlinks. 'absent' if missing;
    `blind:<stat signature>` where Core lacks permission to read it."""
    real = env.real(path)
    try:
        st = os.lstat(real)
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "blind:denied"
    except OSError as exc:
        raise SignalError(f"cannot stat a watched path ({exc.strerror})") from None
    h = hashlib.sha256()
    count = 0
    stack = [(real, "", st)]
    while stack:
        env.check_time()
        current, rel, st = stack.pop()
        count += 1
        if count > MAX_HASH_ENTRIES:
            raise SignalError("a watched directory has too many entries to hash")
        try:
            if stat.S_ISLNK(st.st_mode):
                h.update(f"l\0{rel}\0{os.readlink(current)}\0{_meta(st)}\n".encode(errors="replace"))
            elif stat.S_ISREG(st.st_mode):
                h.update(f"f\0{rel}\0{_file_digest(current, st.st_size)}\0{_meta(st)}\n".encode())
            elif stat.S_ISDIR(st.st_mode):
                h.update(f"d\0{rel}\0{_meta(st)}\n".encode())
                children = sorted(os.listdir(current), reverse=True)
                for name in children:
                    child = os.path.join(current, name)
                    stack.append((child, f"{rel}/{name}", os.lstat(child)))
            else:
                h.update(f"s\0{rel}\0{stat.S_IFMT(st.st_mode):o}\0{_meta(st)}\n".encode())
        except FileNotFoundError:
            h.update(f"x\0{rel}\n".encode())       # raced with a delete: the next scan will see the settled state
        except PermissionError:
            return _blind_token(real)
        except OSError as exc:
            raise SignalError(f"cannot read a watched path ({exc.strerror})") from None
    return h.hexdigest()


def _stat_sig(env: ScanEnv, path: str) -> str:
    try:
        st = os.lstat(env.real(path))
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise SignalError(f"cannot stat a package path ({exc.strerror})") from None
    return f"{stat.S_IFMT(st.st_mode):o}:{st.st_size}:{st.st_mtime_ns}:{st.st_ino}"


def _read_small(path: str, limit: int = 1 << 20) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SignalError(f"cannot read a watched file ({exc.strerror})") from None


# ── collectors ────────────────────────────────────────────────────────────────────────────────────

_ROUTE_LABEL = re.compile(r"^(traefik\.|caddy|nginx\.|virtual\.host)", re.IGNORECASE)


def _container_resource(name: str, labels: dict[str, str]) -> str:
    project, service = labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")
    for candidate in ((f"service:{project}/{service}" if project and service else None), f"container:{name}"):
        if candidate:
            try:
                return canon(candidate)
            except ValueError:
                continue
    return canon(f"container:c{sha(name.encode())[:12]}")


def collect_containers(env: ScanEnv) -> dict[str, Any]:
    rc, out, _err = env.runner(["docker", "ps", "-aq", "--no-trunc"], DOCKER_TIMEOUT_S)
    if rc != 0:
        raise SignalError("docker ps failed")
    ids = sorted({line.strip() for line in out.splitlines() if re.fullmatch(r"[0-9a-f]{12,64}", line.strip())})
    if not ids:
        return {}
    if len(ids) > MAX_CONTAINERS:
        raise SignalError(f"{len(ids)} containers: more than the {MAX_CONTAINERS} the tripwire reads")
    rc, out, _err, truncated = env.bounded(["docker", "inspect", *ids], DOCKER_TIMEOUT_S, MAX_INSPECT_BYTES)
    if rc != 0 or truncated:
        raise SignalError("docker inspect failed or was too large")
    try:
        docs = json.loads(out)
    except ValueError:
        raise SignalError("docker inspect returned something unreadable") from None
    if not isinstance(docs, list):
        raise SignalError("docker inspect returned something unreadable")
    result: dict[str, Any] = {}
    for doc in docs:
        try:
            name = str(doc.get("Name", "")).lstrip("/") or str(doc.get("Id", ""))[:12]
            cfg = doc.get("Config") or {}
            labels = {str(k): str(v) for k, v in (cfg.get("Labels") or {}).items()}
            mounts = sorted([m.get("Type", ""), m.get("Source", ""), m.get("Destination", ""), bool(m.get("RW", True))]
                            for m in doc.get("Mounts") or [])
            nets = sorted(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            routes = sorted(f"{k}={v}"[:200] for k, v in labels.items() if _ROUTE_LABEL.match(k))[:MAX_ROUTE_LABELS]
            result[name] = {
                "resource": _container_resource(name, labels), "image": str(doc.get("Image", "")),
                "mounts": mounts, "networks": [str(n) for n in nets],
                "labels_hash": sha(json.dumps(sorted(labels.items())).encode()), "routes": routes,
            }
        except (AttributeError, TypeError):
            raise SignalError("docker inspect returned something unreadable") from None
    return result


def collect_ports(env: ScanEnv) -> list[str]:
    return sorted(env.provider.listening_ports())


def collect_mounts(env: ScanEnv) -> list[str]:
    return sorted(env.provider.mount_table())


def collect_files(env: ScanEnv) -> dict[str, str]:
    return {rid: hash_path(env, path) for rid, path in env.watched_files()}


_COUNTERS = re.compile(r"\[\d+:\d+\]")


def collect_firewall(env: ScanEnv) -> dict[str, Any]:
    for argv in (["nft", "list", "ruleset"], ["iptables-save"]):
        rc, out, _err = env.runner(argv, DOCKER_TIMEOUT_S)
        if rc == 127:
            continue
        if rc != 0:
            # Not root, or the tool refused. Say so in the baseline instead of hashing an error as if it were rules:
            # the firewall domain is then blind and the baseline event says it is.
            return {"tool": argv[0], "unreadable": rc}
        text = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
        return {"tool": argv[0], "sha": sha(_COUNTERS.sub("", text).encode())}
    return {"tool": "none"}


def collect_boot(env: ScanEnv) -> dict[str, Any]:
    boot = _read_small(env.real("/proc/sys/kernel/random/boot_id"), 256)
    up = _read_small(env.real("/proc/uptime"), 256)
    if boot is None or up is None:
        raise SignalError("boot id or uptime unreadable")
    try:
        uptime = int(float(up.split()[0]))
    except (ValueError, IndexError):
        raise SignalError("uptime unreadable") from None
    return {"boot_id": boot.decode(errors="replace").strip(), "uptime": uptime}


def collect_packages(env: ScanEnv) -> dict[str, str]:
    paths = list(env.package_paths)
    paths += [split(r)[2] for r in env.protected.domains.get("packages", ()) if split(r)[1] == "path"]
    return {p: _stat_sig(env, p) for p in sorted(set(paths))}


def collect_volumes(env: ScanEnv) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rid, path in env.volume_paths:
        real = env.real(path)
        try:
            vfs = os.statvfs(real)
        except FileNotFoundError:
            out[rid] = {"absent": True}
            continue
        except OSError as exc:
            raise SignalError(f"cannot stat a data volume ({exc.strerror})") from None
        used = (vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize
        size = entries = 0
        capped = False
        try:
            top = len(os.listdir(real))
        except PermissionError:
            out[rid] = {"blind": True}
            continue
        except OSError as exc:
            raise SignalError(f"cannot list a data volume ({exc.strerror})") from None
        stack = [real]
        while stack:
            # A big volume cannot be walked inside the signal timeout: stop early and say so (`capped`), and the diff
            # compares the cheap whole-filesystem figures instead. Running out of time is not a failed signal.
            if entries >= MAX_VOLUME_ENTRIES or (env.deadline is not None and env.clock() > env.deadline - 0.5):
                capped = True
                break
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        entries += 1
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                size += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        out[rid] = {"size": size, "entries": entries, "capped": capped, "used": used, "top": top}
    return out


def _git_dir(tree: str) -> str | None:
    dot = os.path.join(tree, ".git")
    try:
        if os.path.isdir(dot) and not os.path.islink(dot):
            return dot
        if os.path.isfile(dot):
            text = (_read_small(dot, 4096) or b"").decode(errors="replace").strip()
            if text.startswith("gitdir:"):
                target = text[7:].strip()
                return target if os.path.isabs(target) else os.path.normpath(os.path.join(tree, target))
    except OSError:
        return None
    return None


def _tree_hash(root: str, env: ScanEnv, *, skip_git: bool = False) -> str:
    h = hashlib.sha256()
    count = 0
    stack = [root]
    while stack:
        env.check_time()
        current = stack.pop()
        try:
            names = sorted(os.listdir(current), reverse=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SignalError(f"cannot list a watched tree ({exc.strerror})") from None
        for name in names:
            if skip_git and name == ".git" and current == root:
                continue
            child = os.path.join(current, name)
            try:
                st = os.lstat(child)
            except OSError:
                continue
            count += 1
            if count > MAX_TREE_ENTRIES:
                h.update(b"truncated")
                return h.hexdigest()
            h.update(f"{os.path.relpath(child, root)}\0{st.st_size}\0{st.st_mtime_ns}\0{stat.S_IMODE(st.st_mode):o}\n".encode(errors="replace"))
            if stat.S_ISDIR(st.st_mode):
                stack.append(child)
    return h.hexdigest()


def _remote_sections(config: bytes) -> str:
    keep, active = [], False
    for line in config.decode(errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            active = stripped.lower().startswith("[remote ")
        if active and stripped:
            keep.append(stripped)
    return sha("\n".join(keep).encode())


def collect_vcs(env: ScanEnv) -> dict[str, Any]:
    """Per live tree: HEAD, refs and reflogs (destructive operations), remotes and remote-tracking refs (publishing,
    pushes), and a stat hash of the working tree. Files are read directly: git is never executed in a tree the
    session could have written a hostile config into."""
    out: dict[str, Any] = {}
    for tree in env.protected.live_trees:
        real = env.real(tree)
        gitdir = _git_dir(real)
        if gitdir is None:
            out[tree] = {"git": False, "worktree": _tree_hash(real, env) if os.path.isdir(real) else "absent"}
            continue

        def digest(rel: str, gitdir: str = gitdir) -> str:
            base = os.path.join(gitdir, rel)
            if not os.path.exists(base):
                return "absent"
            if os.path.isfile(base):
                return sha(_read_small(base) or b"")
            h = hashlib.sha256()
            count = 0
            for dirpath, dirs, files in os.walk(base):
                env.check_time()
                dirs.sort()
                for name in sorted(files):
                    count += 1
                    if count > MAX_HASH_ENTRIES:
                        raise SignalError("a git directory has too many refs to hash")
                    path = os.path.join(dirpath, name)
                    h.update(f"{os.path.relpath(path, base)}\0".encode(errors="replace"))
                    h.update(_read_small(path) or b"")
            return h.hexdigest()

        config = _read_small(os.path.join(gitdir, "config")) or b""
        out[tree] = {
            "git": True, "head": digest("HEAD"),
            "refs": sha(f"{digest('refs/heads')}{digest('refs/tags')}{digest('refs/stash')}{digest('packed-refs')}".encode()),
            "reflog": sha(f"{digest('logs/HEAD')}{digest('logs/refs/heads')}{digest('logs/refs/stash')}".encode()),
            "remotes": _remote_sections(config),
            "tracking": sha(f"{digest('refs/remotes')}{digest('logs/refs/remotes')}".encode()),
            "worktree": _tree_hash(real, env, skip_git=True),
        }
    return out


SIGNALS: dict[str, Callable[[ScanEnv], Any]] = {
    "containers": collect_containers, "ports": collect_ports, "mounts": collect_mounts, "files": collect_files,
    "firewall": collect_firewall, "boot": collect_boot, "packages": collect_packages, "volumes": collect_volumes,
    "vcs": collect_vcs,
}


# ── the scan ──────────────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    snapshot: dict[str, Any]
    errors: dict[str, str]
    started: float
    finished: float

    @property
    def ok(self) -> bool:
        return not self.errors


class Scanner:
    """Runs every collector on its own daemon thread with a per-signal timeout and a scan deadline. A collector that
    does not answer in time is abandoned (its thread cannot be killed, but it is a daemon and its late answer is
    ignored) and the scan reports the signal as failed."""

    def __init__(self, env: ScanEnv, signals: dict[str, Callable[[ScanEnv], Any]] | None = None, *,
                 signal_timeout_s: float = SIGNAL_TIMEOUT_S, deadline_s: float = SCAN_DEADLINE_S,
                 clock: Callable[[], float] | None = None):
        self.env = env
        self.signals = dict(SIGNALS if signals is None else signals)
        self.signal_timeout_s = signal_timeout_s
        self.deadline_s = deadline_s
        self.clock = clock or env.clock

    def scan(self) -> ScanResult:
        started = self.clock()
        wall_start = time.monotonic()
        deadline = min(self.signal_timeout_s, self.deadline_s)
        self.env.watched_files()               # computed once, before the collectors share the environment
        slots: dict[str, dict[str, Any]] = {}
        threads: dict[str, threading.Thread] = {}
        for name, fn in self.signals.items():
            slot: dict[str, Any] = {"done": threading.Event()}
            slots[name] = slot

            def run(fn=fn, slot=slot) -> None:
                try:
                    slot["value"] = fn(self._env_with_deadline())
                except BaseException as exc:      # noqa: BLE001 - any failure is a failed signal, never a crash
                    slot["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                finally:
                    slot["done"].set()

            thread = threading.Thread(target=run, daemon=True, name=f"tripwire-{name}")
            threads[name] = thread
            thread.start()
        snapshot: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, slot in slots.items():
            remaining = deadline - (time.monotonic() - wall_start)
            if not slot["done"].wait(max(0.0, remaining)):
                errors[name] = "timeout"
            elif "error" in slot:
                errors[name] = slot["error"]
            else:
                snapshot[name] = slot["value"]
        return ScanResult(snapshot, errors, started, self.clock())

    def _env_with_deadline(self) -> ScanEnv:
        env = ScanEnv(**{**self.env.__dict__, "_files": self.env._files})
        env.deadline = self.clock() + self.signal_timeout_s
        return env


__all__ = [
    "SCAN_DEADLINE_S",
    "SIGNALS",
    "SIGNAL_TIMEOUT_S",
    "ScanEnv",
    "ScanResult",
    "Scanner",
    "SignalError",
    "blind_resources",
    "hash_path",
    "sha",
    "volume_paths",
]


def blind_resources(snapshot: dict[str, Any]) -> list[str]:
    """Resources the snapshot could only stat, not read: the tripwire is blind to their content."""
    out = [rid for rid, value in snapshot.get("files", {}).items() if isinstance(value, str) and value.startswith("blind:")]
    out += [rid for rid, value in snapshot.get("volumes", {}).items() if isinstance(value, dict) and value.get("blind")]
    if isinstance(snapshot.get("firewall"), dict) and snapshot["firewall"].get("unreadable") is not None:
        out.append(canon("detector:firewall"))
    return sorted(out)


def volume_paths(protected: ProtectedSet) -> list[tuple[str, str]]:
    """(resource id, host path) of every protected data volume the volume signal should size."""
    out = []
    for rid in protected.domains.get("data_volumes", ()):
        _host, kind, name = split(rid)
        if kind in ("path", "mount"):
            out.append((rid, name))
    return sorted(out)
