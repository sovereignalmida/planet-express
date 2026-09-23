"""Writing a compose file safely (slice 5b-4, design §4.6).

The rules this module exists to keep, in one place:

* **Compare-and-swap.** The expectation recorded at proposal time is re-checked immediately before
  the write. A file that changed since then is never overwritten.
* **No symlink anywhere on the path**, checked component by component rather than by `resolve()`:
  a broken symlink still "resolves", and `is_file()` reads False for one, which is how a write can
  land outside `stacks_root` (the same class of bug an earlier review caught in `apply_pending_diff`).
* **Atomic replacement.** Temp file in the same directory, fsync, mode/owner/ACL of the file being
  replaced, rename, fsync the directory. A crash leaves either the old file or the new one.
* **`.env` is never written.** Secrets stay human-only.
"""

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

COMPOSE_FILENAME = "docker-compose.yml"
MAX_COMPOSE_BYTES = 256 * 1024


class ComposeWriteError(Exception):
    """The write cannot proceed. The step fails without touching the file."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose_path_for(stacks_root: Path, stack: str) -> Path:
    return Path(stacks_root) / stack / COMPOSE_FILENAME


def check_path(stacks_root: Path, path: Path) -> None:
    """The path must sit directly under `stacks_root`, be the compose file, and have no symlink in
    any component (including itself). Raises ComposeWriteError."""
    root = Path(stacks_root)
    if path.name != COMPOSE_FILENAME:
        raise ComposeWriteError(f"{path.name} is not {COMPOSE_FILENAME}")
    if path.parent.parent != root:
        raise ComposeWriteError(f"{path} is not a stack directory directly under {root}")
    if root.is_symlink():
        raise ComposeWriteError(f"{root} is a symlink")
    for component in (path.parent, path):
        if component.is_symlink():
            raise ComposeWriteError(f"{component} is a symlink")


def read_current(path: Path) -> str | None:
    """The file's text, or None if it is absent. A file too large to be a compose file, or one that
    is not text, is an error rather than a silent overwrite."""
    if not path.exists():
        return None
    if not path.is_file():
        raise ComposeWriteError(f"{path} is not a regular file")
    if path.stat().st_size > MAX_COMPOSE_BYTES:
        raise ComposeWriteError(f"{path} is larger than {MAX_COMPOSE_BYTES} bytes")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ComposeWriteError(f"{path} cannot be read as text: {exc}") from None


@dataclass(frozen=True)
class WriteRecord:
    """What the inverse needs: what was there, and what this write created."""

    backup_path: str | None      # a copy of the replaced file, or None for a new file
    previous_sha256: str | None
    created_file: bool
    created_directory: bool
    directory_inode: int | None


def write_compose(
    path: Path, content: str, *, stacks_root: Path, expected_sha256: str | None,
    expect_absent: bool, backup_suffix: str,
) -> WriteRecord:
    """Compare-and-swap one compose file, atomically. Raises ComposeWriteError, having written
    nothing, if the expectation no longer holds."""
    check_path(stacks_root, path)
    if len(content.encode("utf-8")) > MAX_COMPOSE_BYTES:
        raise ComposeWriteError(f"the proposed content is larger than {MAX_COMPOSE_BYTES} bytes")

    current = read_current(path)
    if expect_absent:
        if current is not None:
            raise ComposeWriteError(f"{path} exists now, but this step was approved to create it")
    else:
        if current is None:
            raise ComposeWriteError(f"{path} no longer exists, but this step was approved to edit it")
        if sha256_text(current) != expected_sha256:
            raise ComposeWriteError(f"{path} changed since this step was approved")

    directory = path.parent
    created_directory = False
    if not directory.exists():
        directory.mkdir(parents=True)
        created_directory = True
    elif not directory.is_dir():
        raise ComposeWriteError(f"{directory} is not a directory")

    backup_path = None
    if current is not None:
        backup_path = path.with_name(path.name + backup_suffix)
        shutil.copy2(path, backup_path)     # copy2 keeps mode and times; the backup is the inverse

    descriptor, temporary = tempfile.mkstemp(dir=str(directory), prefix=".compose-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if current is not None:
            # The replacement keeps the identity of the file it replaces: mode, owner and ACL.
            stat = path.stat()
            os.chmod(temporary, stat.st_mode & 0o7777)
            try:
                os.chown(temporary, stat.st_uid, stat.st_gid)
            except PermissionError:
                pass                        # not root and not our file: keep our own ownership
            _copy_acl(path, Path(temporary))
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    _fsync_directory(directory)
    return WriteRecord(
        backup_path=str(backup_path) if backup_path else None,
        previous_sha256=sha256_text(current) if current is not None else None,
        created_file=current is None,
        created_directory=created_directory,
        directory_inode=directory.stat().st_ino,
    )


def restore(path: Path, record: WriteRecord, *, stacks_root: Path, written_sha256: str) -> str:
    """The inverse of one write. Returns a sentence describing what it did.

    Refuses unless the file still holds exactly what the write left there: anything else means a
    human (or another process) has been here since, and reverting would destroy their work.
    """
    check_path(stacks_root, path)
    current = read_current(path)
    if current is None or sha256_text(current) != written_sha256:
        raise ComposeWriteError(f"{path} has changed since the step wrote it")

    if record.created_file:
        path.unlink()
        _fsync_directory(path.parent)
        removed = "removed the file this step created"
        directory = path.parent
        if (record.created_directory and directory.exists()
                and directory.stat().st_ino == record.directory_inode
                and not any(directory.iterdir())):
            directory.rmdir()
            removed += " and the empty directory it created"
        return removed

    if not record.backup_path:
        raise ComposeWriteError("no backup was recorded for this write")
    backup = Path(record.backup_path)
    previous = read_current(backup)
    if previous is None or sha256_text(previous) != record.previous_sha256:
        raise ComposeWriteError("the backup of the previous content is missing or changed")
    write_compose(path, previous, stacks_root=stacks_root,
                  expected_sha256=written_sha256, expect_absent=False,
                  backup_suffix=".undo")
    return f"restored the previous content ({record.previous_sha256[:12]})"


def _copy_acl(source: Path, target: Path) -> None:
    """Best effort: POSIX ACLs are only present on some filesystems, and a compose file usually has
    none. A missing ACL is normal; a failure to copy one is not worth failing the write over."""
    try:
        import subprocess
        result = subprocess.run(["getfacl", "-c", "--", str(source)],
                                capture_output=True, text=True, check=False, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            subprocess.run(["setfacl", "--set-file=-", "--", str(target)],
                           input=result.stdout, capture_output=True, text=True,
                           check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _fsync_directory(directory: Path) -> None:
    fd = os.open(str(directory), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
