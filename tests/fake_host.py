"""An in-memory host with the same interface and the same safety rules as `RealHost`, plus fault injection.

Test support, not shipped. `tests/test_setup_host.py` runs one contract suite against both this and
`RealHost`, so the fake cannot drift from the real thing without a test failing.
"""
from __future__ import annotations

import stat as stat_module

from planet_express.setup.host import TEMP_PREFIX, HostError, RunResult, Stat, _split, is_temp_name


class Crash(BaseException):
    """Simulated power loss. A BaseException, like a real process death: no `except Exception` may swallow it."""


class _Node:
    def __init__(self, kind, mode, uid, gid, ino, data=b"", target=None):
        self.kind, self.mode, self.uid, self.gid, self.ino = kind, mode, uid, gid, ino
        self.data, self.target = data, target


class FakeHost:
    DEV = 64769

    def __init__(self, trusted_uids=frozenset({0}), users=None, groups=None, euid=0):
        self.trusted_uids = frozenset(trusted_uids) | {euid}
        self.users = {"root": (0, 0), **(users or {})}
        self.groups = {"root": 0, **(groups or {})}
        self.memberships: dict[str, set[int]] = {}
        self.nodes: dict[str, _Node] = {}
        self._ino = 100
        self.commands: list[list[str]] = []
        self.command_results: dict[tuple, RunResult] = {}
        self.run_as: list[str | None] = []
        self.envs: list[dict] = []
        self.matchers: list = []
        self.umasks: list = []
        self.cwds: list = []
        self.mutations = 0
        self.nodes["/"] = _Node("dir", 0o755, 0, 0, self._next())

    def _next(self) -> int:
        self._ino += 1
        return self._ino

    # -- test setup helpers ---------------------------------------------------------------------------
    def add_dir(self, path, mode=0o755, uid=0, gid=0):
        parts = [p for p in path.split("/") if p]
        built = ""
        for part in parts:
            built += "/" + part
            self.nodes.setdefault(built, _Node("dir", mode, uid, gid, self._next()))
        self.nodes[path].mode, self.nodes[path].uid, self.nodes[path].gid = mode, uid, gid

    def add_file(self, path, data=b"", mode=0o644, uid=0, gid=0):
        parent, _ = _split(path)
        if parent not in self.nodes:
            self.add_dir(parent)
        self.nodes[path] = _Node("file", mode, uid, gid, self._next(), data=data)

    def add_symlink(self, path, target):
        self.nodes[path] = _Node("symlink", 0o777, 0, 0, self._next(), target=target)

    def on(self, match, result):
        """Script every command `match(argv)` accepts: a RunResult, or a function (host, argv) -> RunResult."""
        self.matchers.insert(0, (match, result))      # the latest registration wins, so a test can override a default

    def tree(self) -> dict:
        """A comparable snapshot: path -> (kind, perms, uid, gid, data)."""
        return {p: (n.kind, n.mode, n.uid, n.gid, n.data if n.kind == "file" else None)
                for p, n in sorted(self.nodes.items())}

    # -- resolution ------------------------------------------------------------------------------------
    def _resolve(self, path: str, depth=0) -> str:
        if depth > 20:
            raise HostError(f"too many symlinks: {path}")
        parts, built = [p for p in path.split("/") if p], ""
        for i, part in enumerate(parts):
            built += "/" + part
            node = self.nodes.get(built)
            if node is not None and node.kind == "symlink":
                rest = "/".join(parts[i + 1:])
                target = node.target if node.target.startswith("/") else f"{built.rsplit('/', 1)[0]}/{node.target}"
                return self._resolve(target + ("/" + rest if rest else ""), depth + 1)
        return built or "/"

    def _parent(self, parent: str, *, mutating: bool) -> str:
        real = self._resolve(parent)
        parts = [p for p in real.split("/") if p]
        chain = ["/"] + ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]
        for directory in chain:
            node = self.nodes.get(directory)
            if node is None or node.kind != "dir":
                raise HostError(f"{directory} is not a directory")
            if mutating:
                if node.uid not in self.trusted_uids:
                    raise HostError(f"{directory} is owned by uid {node.uid}, which this plan does not trust")
                if node.mode & 0o022 and not node.mode & stat_module.S_ISVTX:
                    raise HostError(f"{directory} is writable by group or other, so a path through it could be redirected")
        return real

    def _stat(self, node) -> Stat:
        kind_bits = {"dir": stat_module.S_IFDIR, "file": stat_module.S_IFREG, "symlink": stat_module.S_IFLNK}[node.kind]
        return Stat(kind_bits | node.mode, node.uid, node.gid, self.DEV, node.ino, len(node.data))

    # -- reads -------------------------------------------------------------------------------------------
    def lstat(self, path):
        parent, name = _split(path)
        try:
            real = self._resolve(parent)
        except HostError:
            return None
        node = self.nodes.get(f"{real.rstrip('/')}/{name}")
        return self._stat(node) if node else None

    def read_bytes(self, path, limit=1024 * 1024):
        parent, name = _split(path)
        node = self.nodes.get(f"{self._parent(parent, mutating=False).rstrip('/')}/{name}")
        if node is None:
            raise HostError(f"cannot open {path}: No such file or directory")
        if node.kind != "file":
            raise HostError(f"{path} is not a regular file")
        if len(node.data) > limit:
            raise HostError(f"{path} is larger than {limit} bytes")
        return node.data

    def listdir(self, path):
        real = self._resolve(path).rstrip("/")
        return sorted(p[len(real) + 1:] for p in self.nodes if p.startswith(real + "/") and "/" not in p[len(real) + 1:])

    def lookup_user(self, name):
        return self.users.get(name)

    def lookup_group(self, name):
        return self.groups.get(name)

    def user_groups(self, name):
        if name not in self.users:
            return None
        return {self.users[name][1]} | self.memberships.get(name, set())

    # -- mutations ---------------------------------------------------------------------------------------
    def _mutate(self):
        self.mutations += 1

    def mkdir(self, path, mode, uid, gid):
        parent, name = _split(path)
        real = self._parent(parent, mutating=True)
        key = f"{real.rstrip('/')}/{name}"
        if key in self.nodes:
            raise HostError(f"cannot create {path}: File exists")
        self._mutate()
        self.nodes[key] = _Node("dir", mode, uid, gid, self._next())
        return self._stat(self.nodes[key])

    def stage_file(self, parent, data, mode, uid, gid, name=None):
        real = self._parent(parent, mutating=True)
        name = name or f"{TEMP_PREFIX}{self._next():x}.tmp"
        if not is_temp_name(name):
            raise HostError(f"{name!r} is not a staged-file name")
        if f"{real.rstrip('/')}/{name}" in self.nodes:
            raise HostError(f"cannot stage a file in {parent}: File exists")
        self._mutate()
        node = _Node("file", mode, uid, gid, self._next(), data=bytes(data))
        self.nodes[f"{real.rstrip('/')}/{name}"] = node
        return name, self._stat(node)

    def commit_staged(self, parent, temp_name, name):
        real = self._parent(parent, mutating=True).rstrip("/")
        existing = self.nodes.get(f"{real}/{name}")
        if existing is not None and existing.kind != "file":
            raise HostError(f"{parent}/{name} is not a regular file; refusing to replace it")
        self._mutate()
        self.nodes[f"{real}/{name}"] = self.nodes.pop(f"{real}/{temp_name}")

    def discard_staged(self, parent, temp_name):
        if not is_temp_name(temp_name):
            raise HostError("only this tool's own staged files can be discarded")
        real = self._parent(parent, mutating=True).rstrip("/")
        self._mutate()
        self.nodes.pop(f"{real}/{temp_name}", None)

    def copy_private(self, source, dest_parent, dest_name, uid, gid):
        data = self.read_bytes(source)
        real = self._parent(dest_parent, mutating=True).rstrip("/")
        if f"{real}/{dest_name}" in self.nodes:
            raise HostError(f"cannot create the backup {dest_parent}/{dest_name}: File exists")
        self._mutate()
        node = _Node("file", 0o600, uid, gid, self._next(), data=data)
        self.nodes[f"{real}/{dest_name}"] = node
        return self._stat(node)

    def unlink(self, path):
        parent, name = _split(path)
        real = self._parent(parent, mutating=True).rstrip("/")
        node = self.nodes.get(f"{real}/{name}")
        if node is None:
            return
        if node.kind == "dir":
            raise HostError(f"{path} is a directory; use rmdir")
        self._mutate()
        del self.nodes[f"{real}/{name}"]

    def rmdir(self, path):
        parent, name = _split(path)
        real = self._parent(parent, mutating=True).rstrip("/")
        key = f"{real}/{name}"
        node = self.nodes.get(key)
        if node is None or node.kind != "dir":
            raise HostError(f"cannot remove {path}: Not a directory")
        if any(p.startswith(key + "/") for p in self.nodes):
            raise HostError(f"cannot remove {path}: Directory not empty")
        self._mutate()
        del self.nodes[key]

    def tree_digest(self, path, limit=500_000):
        import hashlib
        parent, name = _split(path)
        real = self._parent(parent, mutating=False).rstrip("/")
        key = f"{real}/{name}"
        if self.nodes.get(key) is None or self.nodes[key].kind != "dir":
            raise HostError(f"cannot open {path}: Not a directory")
        digest = hashlib.sha256()
        for other in sorted(n for n in self.nodes if n.startswith(key + "/")):
            rel = other[len(key) + 1:]
            if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
                continue
            node = self.nodes[other]
            digest.update(f"{rel}\0{node.kind}\0{len(node.data) if node.kind == 'file' else 0}\n".encode())
        return digest.hexdigest()

    def remove_tree(self, path, identity):
        parent, name = _split(path)
        real = self._parent(parent, mutating=True).rstrip("/")
        key = f"{real}/{name}"
        node = self.nodes.get(key)
        if node is None:
            return
        if node.kind != "dir" or (self.DEV, node.ino) != (identity.get("dev"), identity.get("ino")):
            raise HostError(f"{path} is not the directory this tool created; leaving it alone")
        self._mutate()
        for other in [n for n in self.nodes if n == key or n.startswith(key + "/")]:
            del self.nodes[other]

    def chmod_dir(self, path, mode):
        parent, name = _split(path)
        real = self._parent(parent, mutating=True).rstrip("/")
        node = self.nodes.get(f"{real}/{name}")
        if node is None or node.kind != "dir":
            raise HostError(f"cannot open {path}: Not a directory")
        self._mutate()
        node.mode = mode
        return self._stat(node)

    # -- commands ------------------------------------------------------------------------------------------
    def run(self, argv, *, timeout=60, as_user=None, env=None, cwd=None, umask=None):
        if not argv or not all(isinstance(a, str) for a in argv):
            raise HostError("a command is a non-empty list of strings")
        self.commands.append(list(argv))
        self.run_as.append(as_user)
        self.envs.append(dict(env or {}))
        self.umasks.append(umask)
        self.cwds.append(cwd)
        self.mutations += 1
        result = next((r for match, r in self.matchers if match(list(argv))), None)
        if result is None:
            result = self.command_results.get(tuple(argv), RunResult(0, ""))
        # A scripted command may be a function, so it can have side effects on the fake (create a venv, ...).
        return result(self, list(argv)) if callable(result) else result


class FaultyHost:
    """Wraps a host and raises `Crash` when the Nth mutating operation is about to happen (`before`) or
    has just happened (`after`), so a test can stop the world between any two operations."""

    MUTATING = ("mkdir", "stage_file", "commit_staged", "discard_staged", "copy_private", "unlink", "rmdir", "chmod_dir", "remove_tree", "run")

    def __init__(self, inner, *, crash_at: int | None = None, after: bool = False):
        self._inner, self._crash_at, self._after, self.ops = inner, crash_at, after, 0

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name not in self.MUTATING:
            return attribute

        def call(*args, **kwargs):
            self.ops += 1
            if self._crash_at == self.ops and not self._after:
                raise Crash(f"crash before op {self.ops}: {name}")
            result = attribute(*args, **kwargs)
            if self._crash_at == self.ops and self._after:
                raise Crash(f"crash after op {self.ops}: {name}")
            return result
        return call
