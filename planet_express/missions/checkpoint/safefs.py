"""Descriptor-relative file access for checkpoints (contract §7).

Every read and write of an in-scope file walks from a directory fd opened on the stacks root, one component
at a time, with O_NOFOLLOW. A path that was a plain file when it was validated and is a symlink (or a
different file) when it is captured or restored is refused, never followed: a path string only NAMES a file,
the fd is what is trusted.
"""

import errno
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass

_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class UnsafePath(Exception):
    """The path cannot be captured or restored safely. The message names the path and the reason, never contents."""


@dataclass(frozen=True)
class FileMeta:
    dev: int
    ino: int
    size: int
    mode: int
    uid: int
    gid: int


def real_root(path: str) -> str:
    """The stacks root after realpath: the one trusted anchor every other path is measured against."""
    return os.path.realpath(path)


def resolve_in_root(root_real: str, path: str) -> tuple[str, bool]:
    """(real path, passed through a symlink). Refuses relative paths, `..` components, NUL, and a real path
    outside the root. A symlink that stays inside the root is allowed and recorded; one that leaves is refused."""
    if not isinstance(path, str) or not path or "\0" in path or not path.startswith("/"):
        raise UnsafePath(f"{path!r}: not an absolute path")
    if ".." in path.split("/"):
        raise UnsafePath(f"{path}: '..' components are refused")
    real = os.path.realpath(path)
    if real == root_real or not real.startswith(root_real.rstrip("/") + "/"):
        raise UnsafePath(f"{path}: resolves outside the stacks root")
    return real, real != os.path.normpath(path)


def _parts(root_real: str, real: str) -> list[str]:
    if not real.startswith(root_real.rstrip("/") + "/"):
        raise UnsafePath(f"{real}: outside the stacks root")
    return [p for p in real[len(root_real):].split("/") if p]


def _open_dir_nofollow(root_real: str, parts: list[str]) -> int:
    fd = os.open(root_real, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _meta(st: os.stat_result) -> FileMeta:
    return FileMeta(st.st_dev, st.st_ino, st.st_size, st.st_mode & 0o7777, st.st_uid, st.st_gid)


def open_regular(root_real: str, real: str, *, max_bytes: int) -> tuple[int, FileMeta]:
    """Open an existing regular file without following any symlink. The caller closes the fd.
    Refuses symlinks, devices, FIFOs, sockets, directories, unreadable files and files over `max_bytes`.
    The kind is checked with fstatat BEFORE the open so a device or FIFO is never opened at all."""
    parts = _parts(root_real, real)
    try:
        dfd = _open_dir_nofollow(root_real, parts[:-1])
    except OSError as exc:
        raise UnsafePath(f"{real}: parent directory cannot be opened without following links "
                         f"({_why(exc)})") from None
    try:
        try:
            pre = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except OSError as exc:
            raise UnsafePath(f"{real}: cannot be inspected ({_why(exc)})") from None
        if not stat.S_ISREG(pre.st_mode):
            raise UnsafePath(f"{real}: not a regular file ({_kind(pre.st_mode)})")
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        except OSError as exc:
            raise UnsafePath(f"{real}: cannot be opened ({_why(exc)})") from None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            raise UnsafePath(f"{real}: changed while it was opened")
        if st.st_size > max_bytes:
            raise UnsafePath(f"{real}: {st.st_size} bytes exceeds the {max_bytes} byte limit")
    except BaseException:
        os.close(fd)
        raise
    return fd, _meta(st)


def read_all(fd: int, max_bytes: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise UnsafePath("file grew past the size limit while it was read")
        chunks.append(chunk)


def read_file(root_real: str, real: str, *, max_bytes: int) -> tuple[bytes, FileMeta]:
    fd, meta = open_regular(root_real, real, max_bytes=max_bytes)
    try:
        return read_all(fd, max_bytes), meta
    finally:
        os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_absent(root_real: str, real: str) -> bool:
    """True only when the parent directory opens cleanly and the name does not exist there."""
    parts = _parts(root_real, real)
    try:
        dfd = _open_dir_nofollow(root_real, parts[:-1])
    except OSError:
        return False
    try:
        os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


def file_state(root_real: str, real: str, *,
               max_bytes: int) -> tuple[str | None, bytes | None, FileMeta | None]:
    """(sha256, bytes, meta) of the file now; all None if it is absent. Raises UnsafePath for anything present
    that cannot be read safely (a symlink, a non-regular file, an oversize file, a permission error)."""
    try:
        data, meta = read_file(root_real, real, max_bytes=max_bytes)
    except UnsafePath:
        if is_absent(root_real, real):
            return None, None, None
        raise
    return sha256_bytes(data), data, meta


def _proc_path(fd: int) -> str:
    # `/proc/self` would name the getfacl/setfacl child; our own pid names our fd table.
    return f"/proc/{os.getpid()}/fd/{fd}"


def _acl_run(argv: list[str], text: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, input=text, capture_output=True, text=True, check=False, timeout=10,
                              env={"PATH": _PATH}, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None


def read_acl(fd: int) -> str | None:
    """The ACL text when the file has an EXTENDED ACL (named entries, a mask or defaults), else None."""
    done = _acl_run(["getfacl", "-c", "--", _proc_path(fd)])
    if done is None or done.returncode != 0:
        return None
    lines = [ln.strip() for ln in done.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    extended = any(ln.startswith(("mask::", "default:"))
                   or (ln.split(":")[0] in ("user", "group") and len(ln.split(":")) > 1 and ln.split(":")[1] != "")
                   for ln in lines)
    return "\n".join(lines) + "\n" if extended else None


def write_acl(fd: int, acl: str) -> bool:
    done = _acl_run(["setfacl", "--set-file=-", "--", _proc_path(fd)], acl)
    return bool(done and done.returncode == 0)


def write_atomic(root_real: str, real: str, data: bytes, *, mode: int, uid: int, gid: int,
                 acl: str | None) -> list[str]:
    """Replace (or create) one file: temp file in the same directory, fsync, owner/mode/ACL, rename, fsync of
    the directory, all through a directory fd opened without following links. Returns what could not be
    restored (`owner`, `acl`) so the caller can report it; the bytes and mode are always written or this raises."""
    parts = _parts(root_real, real)
    try:
        dfd = _open_dir_nofollow(root_real, parts[:-1])
    except OSError as exc:
        raise UnsafePath(f"{real}: parent directory cannot be opened without following links "
                         f"({_why(exc)})") from None
    name, skipped = parts[-1], []
    temp: str | None = f".pe-restore-{os.getpid()}-{os.urandom(4).hex()}.tmp"
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(f"{real}: not a regular file ({_kind(st.st_mode)}); refusing to replace it")
        except FileNotFoundError:
            pass
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
                try:
                    os.fchown(fd, uid, gid)
                except PermissionError:
                    now = os.fstat(fd)
                    if (now.st_uid, now.st_gid) != (uid, gid):
                        skipped.append("owner")
                os.fchmod(fd, mode)
                if acl is not None:
                    if write_acl(fd, acl):
                        if os.fstat(fd).st_mode & 0o7777 != mode:
                            os.fchmod(fd, mode)
                    else:
                        skipped.append("acl")
            finally:
                os.close(fd)
            os.rename(temp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            temp = None
        finally:
            if temp is not None:
                try:
                    os.unlink(temp, dir_fd=dfd)
                except OSError:
                    pass
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return skipped


def _kind(mode: int) -> str:
    for check, label in ((stat.S_ISLNK, "symlink"), (stat.S_ISDIR, "directory"), (stat.S_ISFIFO, "FIFO"),
                         (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "character device"),
                         (stat.S_ISBLK, "block device")):
        if check(mode):
            return label
    return "special file"


def _why(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "a symlink is in the way"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if exc.errno == errno.ENOENT:
        return "does not exist"
    if exc.errno == errno.ENOTDIR:
        return "not a directory"
    return os.strerror(exc.errno) if exc.errno else "error"
