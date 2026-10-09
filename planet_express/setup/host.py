"""The host as `apply` handlers see it: a small set of operations, each safe by construction.

Handlers never touch the filesystem or run anything directly; they call these. That keeps every
dangerous primitive in one reviewable place, and lets the same handlers run against `FakeHost` (an
in-memory tree with fault injection) in tests.

The rules `RealHost` enforces for every mutating operation:

* **No symlink is ever followed at the final component**, and the parent is opened once, after its
  whole ancestor chain is validated, then every operation is relative to that open directory
  (`dir_fd`). A path swapped for a symlink after validation cannot redirect a write.
* **Ancestors must be trustworthy**: each owned by root or a user the plan names, and not writable by
  group or other (a sticky directory such as /tmp is fine: others cannot rename our entries in it).
* **Temp files are created exclusively with mode 0600**, so a secret is never world-readable, even for
  an instant, and are renamed into place only after fsync.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat as stat_module
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass

_MINIMAL_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
TEMP_PREFIX = ".pe-setup-"


def new_temp_name() -> str:
    return f"{TEMP_PREFIX}{os.urandom(6).hex()}.tmp"


def is_temp_name(name: str) -> bool:
    return name.startswith(TEMP_PREFIX) and name.endswith(".tmp") and "/" not in name


class HostError(Exception):
    """An operation could not be done safely. Nothing was changed by the failing operation."""


@dataclass(frozen=True)
class Stat:
    mode: int
    uid: int
    gid: int
    dev: int
    ino: int
    size: int

    @property
    def kind(self) -> str:
        if stat_module.S_ISLNK(self.mode):
            return "symlink"
        if stat_module.S_ISDIR(self.mode):
            return "dir"
        if stat_module.S_ISREG(self.mode):
            return "file"
        return "other"

    @property
    def perms(self) -> int:
        return stat_module.S_IMODE(self.mode)

    @property
    def identity(self) -> dict:
        """Device and inode: proof a file is the one we made, which its content alone is not."""
        return {"dev": self.dev, "ino": self.ino}


@dataclass(frozen=True)
class RunResult:
    rc: int
    out: str
    err: str = ""


def _split(path: str) -> tuple[str, str]:
    if not path.startswith("/"):
        raise HostError(f"not an absolute path: {path!r}")
    parent, _, name = path.rstrip("/").rpartition("/")
    if not name or name in (".", "..") or "/" in name:
        raise HostError(f"not a usable file name: {path!r}")
    return parent or "/", name


class RealHost:
    def __init__(self, trusted_uids: frozenset[int] | set[int] = frozenset({0})):
        # Root, plus whoever the plan makes an owner. The effective uid is always trusted: it can already
        # do anything it could do through its own directories.
        self.trusted_uids = frozenset(trusted_uids) | {os.geteuid()}

    # -- reads ------------------------------------------------------------------------------------
    def lstat(self, path: str) -> Stat | None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        return Stat(st.st_mode, st.st_uid, st.st_gid, st.st_dev, st.st_ino, st.st_size)

    def read_bytes(self, path: str, limit: int = 1024 * 1024) -> bytes:
        """The file's bytes. Refuses a symlink, a non-regular file, or one over `limit`."""
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=False) as fd:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                handle = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot open {path}: {exc.strerror}") from exc
            try:
                # Checked on the open descriptor, before wrapping it: what we read is what we checked.
                st = os.fstat(handle)
                if not stat_module.S_ISREG(st.st_mode):
                    raise HostError(f"{path} is not a regular file")
                if st.st_size > limit:
                    raise HostError(f"{path} is larger than {limit} bytes")
                with os.fdopen(handle, "rb") as stream:
                    handle = None
                    return stream.read(limit + 1)
            finally:
                if handle is not None:
                    os.close(handle)

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except FileNotFoundError:
            return []

    def lookup_user(self, name: str) -> tuple[int, int] | None:
        import pwd
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        return entry.pw_uid, entry.pw_gid

    def lookup_group(self, name: str) -> int | None:
        import grp
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    def user_groups(self, name: str) -> set[int] | None:
        """Every group id the account belongs to (primary included), or None if there is no such user."""
        ids = self.lookup_user(name)
        return None if ids is None else set(os.getgrouplist(name, ids[1]))

    _UNSTABLE_INODES = frozenset({"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "cifs", "smb3"})

    def stable_inodes(self, path: str) -> bool:
        """False on filesystems that invent inode numbers at mount time (FAT, NTFS, SMB): a file's inode there
        proves nothing after a reboot, so identity checks must fall back to content and metadata."""
        real = os.path.realpath(path)
        best, fstype = "", ""
        try:
            lines = open("/proc/self/mounts", encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            return True
        for line in lines:
            fields = line.split()
            if len(fields) < 3:
                continue
            mount = fields[1].replace("\\040", " ")
            if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                best, fstype = mount, fields[2]
        return fstype not in self._UNSTABLE_INODES

    def _group_is_private(self, gid: int) -> bool:
        """True if every account in group `gid` (as primary or supplementary group) is one this plan trusts."""
        import grp
        import pwd
        try:
            members = set(grp.getgrgid(gid).gr_mem)
            accounts = pwd.getpwall()
        except (KeyError, OSError):
            return False
        trusted_names = {a.pw_name for a in accounts if a.pw_uid in self.trusted_uids}
        if not members <= trusted_names:
            return False
        return all(a.pw_uid in self.trusted_uids for a in accounts if a.pw_gid == gid)

    # -- safe parent handling -----------------------------------------------------------------------
    @contextmanager
    def _parent_fd(self, parent: str, *, mutating: bool):
        """An open directory fd for `parent`, after validating every ancestor of its real path."""
        real = os.path.realpath(parent)
        components = [c for c in real.split("/") if c]
        chain = ["/"] + ["/" + "/".join(components[: i + 1]) for i in range(len(components))]
        last = None
        for directory in chain:
            try:
                st = os.lstat(directory)
            except OSError as exc:
                raise HostError(f"{directory}: {exc.strerror}") from exc
            if not stat_module.S_ISDIR(st.st_mode):
                raise HostError(f"{directory} is not a directory")
            if mutating:
                if st.st_uid not in self.trusted_uids:
                    raise HostError(f"{directory} is owned by uid {st.st_uid}, which this plan does not trust")
                sticky = st.st_mode & stat_module.S_ISVTX
                if st.st_mode & 0o002 and not sticky:
                    raise HostError(f"{directory} is writable by group or other, so a path through it could be redirected")
                # Group-write is harmless when the group holds only accounts this plan already trusts: a stock
                # Ubuntu clone is 775 with a one-person private group, and refusing it would stop every install.
                if st.st_mode & 0o020 and not sticky and not self._group_is_private(st.st_gid):
                    raise HostError(f"{directory} is writable by group or other, so a path through it could be redirected")
            last = st
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(real, flags)
        except OSError as exc:
            raise HostError(f"cannot open {parent}: {exc.strerror}") from exc
        try:
            opened = os.fstat(fd)
            if last is not None and (opened.st_dev, opened.st_ino) != (last.st_dev, last.st_ino):
                raise HostError(f"{parent} changed while it was being opened")
            yield fd
        finally:
            os.close(fd)

    def open_private_dir(self, path: str, *, create: bool = False) -> int:
        """An open descriptor for a directory this tool may trust with its own state, or `HostError`.

        The whole real path must be trustworthy (every ancestor owned by a trusted uid, none writable by
        group or other, the directory itself included, with no sticky exception) and it must be opened
        without following a symlink at the end. `create` makes missing components (0700 for the last,
        0755 for intermediate ones). The caller closes the descriptor and does everything relative to it."""
        if create:
            self._make_missing(path)
        try:
            final = os.lstat(path.rstrip("/") or "/")
        except OSError as exc:
            raise HostError(f"cannot use {path}: {exc.strerror}") from exc
        if stat_module.S_ISLNK(final.st_mode):
            # realpath() below would quietly follow it to wherever it points.
            raise HostError(f"{path} is a symlink; refusing to keep state behind one")
        with self._parent_fd(path, mutating=True) as fd:
            st = os.fstat(fd)
            if st.st_mode & 0o022:
                raise HostError(f"{path} is writable by group or other; refusing to keep state in it")
            return os.dup(fd)

    def _make_missing(self, path: str, private: bool = True) -> None:
        """Create `path` and any missing parents. Only the final component is private (0700); a parent made on
        the way is just a route to it (0755)."""
        if os.path.lexists(path):
            return
        parent = os.path.dirname(path.rstrip("/")) or "/"
        if not os.path.lexists(parent):
            self._make_missing(parent, private=False)
        self.mkdir(path, 0o700 if private else 0o755, os.geteuid(), os.getegid())

    # -- mutations ----------------------------------------------------------------------------------
    def mkdir(self, path: str, mode: int, uid: int, gid: int) -> Stat:
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                os.mkdir(name, mode, dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot create {path}: {exc.strerror}") from exc
            try:
                os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)   # umask must not narrow it silently
                os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                os.rmdir(name, dir_fd=fd)
                raise HostError(f"cannot set owner or mode on {path}: {exc.strerror}") from exc
            st = os.lstat(name, dir_fd=fd)
            os.fsync(fd)
        return Stat(st.st_mode, st.st_uid, st.st_gid, st.st_dev, st.st_ino, st.st_size)

    def stage_file(self, parent: str, data: bytes, mode: int, uid: int, gid: int,
                   name: str | None = None) -> tuple[str, Stat]:
        """Create a temp file in `parent`, exclusively and 0600, write and fsync it, then give it its final
        mode and owner. Returns (temp name, its stat). The final name is untouched until `commit_staged`.

        A caller that must journal the name before the file exists passes `name` (it must fit the reserved
        pattern); `O_EXCL` then guarantees that if this succeeds, this call created it."""
        with self._parent_fd(parent, mutating=True) as fd:
            name = name or new_temp_name()
            if not is_temp_name(name):
                raise HostError(f"{name!r} is not a staged-file name")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                handle = os.open(name, flags, 0o600, dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot stage a file in {parent}: {exc.strerror}") from exc
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), mode)
                    os.fchown(stream.fileno(), uid, gid)
                st = os.lstat(name, dir_fd=fd)
            except OSError as exc:
                os.unlink(name, dir_fd=fd)
                raise HostError(f"cannot write the staged file in {parent}: {exc.strerror}") from exc
        return name, Stat(st.st_mode, st.st_uid, st.st_gid, st.st_dev, st.st_ino, st.st_size)

    def commit_staged(self, parent: str, temp_name: str, name: str) -> None:
        """Atomically rename the staged file onto `name`, then fsync the directory."""
        if "/" in temp_name or "/" in name:
            raise HostError("a commit names files, not paths")
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                existing = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat_module.S_ISREG(existing.st_mode):
                raise HostError(f"{parent}/{name} is not a regular file; refusing to replace it")
            os.replace(temp_name, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)

    def discard_staged(self, parent: str, temp_name: str) -> None:
        if not is_temp_name(temp_name):
            raise HostError("only this tool's own staged files can be discarded")
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                os.unlink(temp_name, dir_fd=fd)
            except FileNotFoundError:
                pass

    def copy_private(self, source: str, dest_parent: str, dest_name: str, uid: int, gid: int) -> Stat:
        """Copy a regular file to a new 0600 file (a backup). The destination must not exist."""
        data = self.read_bytes(source)
        with self._parent_fd(dest_parent, mutating=True) as fd:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                handle = os.open(dest_name, flags, 0o600, dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot create the backup {dest_parent}/{dest_name}: {exc.strerror}") from exc
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchown(stream.fileno(), uid, gid)
                st = os.lstat(dest_name, dir_fd=fd)
                os.fsync(fd)
            except OSError as exc:
                try:
                    os.unlink(dest_name, dir_fd=fd)
                except OSError:
                    pass
                raise HostError(f"cannot write the backup {dest_parent}/{dest_name}: {exc.strerror}") from exc
        return Stat(st.st_mode, st.st_uid, st.st_gid, st.st_dev, st.st_ino, st.st_size)

    def unlink(self, path: str) -> None:
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                st = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                return
            if stat_module.S_ISDIR(st.st_mode):
                raise HostError(f"{path} is a directory; use rmdir")
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)

    def rmdir(self, path: str) -> None:
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot remove {path}: {exc.strerror}") from exc
            os.fsync(fd)

    def tree_digest(self, path: str, limit: int = 500_000) -> str:
        """A fingerprint of a directory tree's shape: every entry's relative path, kind and (for files) size,
        without following symlinks. Bytecode caches are left out because running the code creates them. Used to
        prove, later, that a tree is still exactly what setup left."""
        parent, name = _split(path)
        digest, seen = hashlib.sha256(), [0]
        with self._parent_fd(parent, mutating=False) as fd:
            try:
                top = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot open {path}: {exc.strerror}") from exc

            def walk(handle: int, prefix: str) -> None:
                for entry in sorted(os.listdir(handle)):
                    if entry == "__pycache__" or entry.endswith(".pyc"):
                        continue
                    st = os.lstat(entry, dir_fd=handle)
                    seen[0] += 1
                    if seen[0] > limit:
                        raise HostError(f"{path} has too many entries to fingerprint")
                    kind = ("dir" if stat_module.S_ISDIR(st.st_mode) else "link" if stat_module.S_ISLNK(st.st_mode)
                            else "file" if stat_module.S_ISREG(st.st_mode) else "special")
                    if kind == "file":
                        detail = self._file_hash(entry, handle)
                    elif kind == "link":
                        detail = os.readlink(entry, dir_fd=handle)
                    elif kind == "special":
                        detail = f"{stat_module.S_IFMT(st.st_mode):o}"          # a FIFO or socket is never opened
                    else:
                        detail = ""
                    digest.update(f"{prefix}{entry}\0{kind}\0{detail}\n".encode("utf-8", "surrogateescape"))
                    if kind == "dir":
                        child = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=handle)
                        try:
                            walk(child, f"{prefix}{entry}/")
                        finally:
                            os.close(child)
            try:
                walk(top, "")
            except OSError as exc:
                raise HostError(f"cannot read {path}: {exc.strerror}") from exc
            finally:
                os.close(top)
        return digest.hexdigest()

    @staticmethod
    def _file_hash(name: str, dir_fd: int) -> str:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
        try:
            digest = hashlib.sha256()
            while chunk := os.read(handle, 1 << 20):
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(handle)

    def remove_tree(self, path: str, identity: dict) -> None:
        """Delete a directory tree that this tool created: `identity` (device and inode) must still be the
        directory's, nothing is followed through a symlink, and the walk never leaves that filesystem. Missing
        is fine (already gone)."""
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                st = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise HostError(f"cannot inspect {path}: {exc.strerror}") from exc
            if not stat_module.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != (identity.get("dev"), identity.get("ino")):
                raise HostError(f"{path} is not the directory this tool created; leaving it alone")
            try:
                self._empty(fd, name, st.st_dev)
                os.rmdir(name, dir_fd=fd)
                os.fsync(fd)
            except OSError as exc:
                raise HostError(f"cannot remove {path}: {exc.strerror}") from exc

    def _empty(self, parent_fd: int, name: str, device: int) -> None:
        handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        try:
            for entry in os.listdir(handle):
                st = os.lstat(entry, dir_fd=handle)
                if stat_module.S_ISDIR(st.st_mode):
                    if st.st_dev != device:
                        raise OSError(errno.EXDEV, "crosses a filesystem boundary")
                    self._empty(handle, entry, device)
                    os.rmdir(entry, dir_fd=handle)
                else:
                    os.unlink(entry, dir_fd=handle)
        finally:
            os.close(handle)

    def chmod_dir(self, path: str, mode: int) -> Stat:
        """Set the permission bits of an existing directory, opened without following a symlink."""
        parent, name = _split(path)
        with self._parent_fd(parent, mutating=True) as fd:
            try:
                handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            except OSError as exc:
                raise HostError(f"cannot open {path}: {exc.strerror}") from exc
            try:
                os.fchmod(handle, mode)
                st = os.fstat(handle)
            except OSError as exc:
                raise HostError(f"cannot change the mode of {path}: {exc.strerror}") from exc
            finally:
                os.close(handle)
            os.fsync(fd)
        return Stat(st.st_mode, st.st_uid, st.st_gid, st.st_dev, st.st_ino, st.st_size)

    # -- commands -----------------------------------------------------------------------------------
    def run(self, argv: list[str], *, timeout: float = 60, as_user: str | None = None,
            env: dict[str, str] | None = None, cwd: str | None = None, umask: int | None = None) -> RunResult:
        """An argv, never a shell, with a minimal environment. `as_user` drops privileges first; `umask` fixes
        the mask the child creates files with (a virtualenv built under a root shell's 077 would be unreadable
        to the services that must run from it)."""
        if not argv or not all(isinstance(a, str) for a in argv):
            raise HostError("a command is a non-empty list of strings")
        environment = dict(_MINIMAL_ENV)
        environment.update(env or {})
        command = list(argv)
        if as_user is not None:
            ids = self.lookup_user(as_user)
            if ids is None:
                raise HostError(f"no such user: {as_user}")
            if os.geteuid() == 0:
                command = ["setpriv", f"--reuid={ids[0]}", f"--regid={ids[1]}", "--init-groups", "--", *command]
            elif ids[0] != os.geteuid():
                raise HostError(f"cannot run as {as_user}: not root")
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                                  env=environment, cwd=cwd, stdin=subprocess.DEVNULL,
                                  preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)
        except FileNotFoundError:
            return RunResult(127, "", f"{argv[0]}: not found")
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"{argv[0]}: timed out after {timeout:g}s")
        except OSError as exc:
            return RunResult(126, "", str(exc))
        return RunResult(done.returncode, done.stdout, done.stderr)
