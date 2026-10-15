"""The on-disk checkpoint store (contract §7).

Layout: `<root>/<id>/{files/<n>, manifest.json, COMMITTED, post.json, drift/<n>}`. The root and every
directory are mode 0700 and every file 0600, Core-owned: the files hold secrets at rest.

Commit order (a crash anywhere before the marker leaves garbage, never a half-usable checkpoint):
fsync every payload file, fsync every directory in the tree, write and fsync the manifest, write and fsync
the COMMITTED marker, fsync the checkpoint directory and its parent. `COMMITTED` is last and only then is the
checkpoint usable. `trace` receives (op, path) for every write/fsync/rename so a test can assert the order.
"""

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^C-[0-9a-f]{16}$")
MARKER = "COMMITTED"
MANIFEST = "manifest.json"
POST = "post.json"
QUARANTINE = ".quarantine"
Trace = Callable[[str, str], None]


class StoreError(Exception):
    pass


class CheckpointStore:
    def __init__(self, root: Path | str, *, trace: Trace | None = None):
        self.root = Path(root)
        self._trace = trace

    # ── plumbing ────────────────────────────────────────────────────────────
    def _t(self, op: str, path: Path | str) -> None:
        if self._trace is not None:
            self._trace(op, str(path))

    def ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def dir_for(self, checkpoint_id: str) -> Path:
        if not ID_RE.fullmatch(checkpoint_id or ""):
            raise StoreError(f"{checkpoint_id!r} is not a checkpoint id")
        return self.root / checkpoint_id

    def _write_new(self, path: Path, data: bytes) -> None:
        self._t("write", path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    def _fsync_file(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._t("fsync_file", path)

    def _fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._t("fsync_dir", path)

    def _mkdir(self, path: Path) -> None:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)

    # ── create ──────────────────────────────────────────────────────────────
    def new_checkpoint(self, checkpoint_id: str) -> Path:
        self.ensure_root()
        directory = self.dir_for(checkpoint_id)
        self._mkdir(directory)
        self._mkdir(directory / "files")
        return directory

    def write_payload(self, checkpoint_id: str, index: int, data: bytes) -> None:
        self._write_new(self.dir_for(checkpoint_id) / "files" / str(index), data)

    def commit(self, checkpoint_id: str, manifest: dict[str, Any]) -> None:
        directory = self.dir_for(checkpoint_id)
        payloads = sorted((directory / "files").iterdir())
        for payload in payloads:
            self._fsync_file(payload)
        for tree_dir in (directory / "files", directory):
            self._fsync_dir(tree_dir)
        self._write_new(directory / MANIFEST, _dump(manifest))
        self._fsync_file(directory / MANIFEST)
        self._write_new(directory / MARKER, b"1\n")
        self._fsync_file(directory / MARKER)
        self._fsync_dir(directory)
        self._fsync_dir(self.root)

    def discard(self, checkpoint_id: str) -> None:
        """Remove an uncommitted build. Never removes a committed checkpoint (purge does)."""
        directory = self.dir_for(checkpoint_id)
        if directory.exists() and not (directory / MARKER).exists():
            shutil.rmtree(directory, ignore_errors=True)

    # ── read ────────────────────────────────────────────────────────────────
    def is_committed(self, checkpoint_id: str) -> bool:
        directory = self.dir_for(checkpoint_id)
        return (directory / MARKER).is_file() and not (directory / MARKER).is_symlink()

    def read_manifest(self, checkpoint_id: str) -> dict[str, Any]:
        if not self.is_committed(checkpoint_id):
            raise StoreError(f"{checkpoint_id} is not committed")
        return json.loads((self.dir_for(checkpoint_id) / MANIFEST).read_bytes())

    def read_payload(self, checkpoint_id: str, index: int) -> bytes:
        return (self.dir_for(checkpoint_id) / "files" / str(index)).read_bytes()

    def read_post(self, checkpoint_id: str) -> dict[str, Any] | None:
        path = self.dir_for(checkpoint_id) / POST
        return json.loads(path.read_bytes()) if path.is_file() else None

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if ID_RE.fullmatch(p.name) and p.is_dir())

    def total_bytes(self) -> int:
        """Bytes of live checkpoints. The quarantine is counted separately (`quarantine_bytes`)."""
        total = 0
        for base, dirs, names in os.walk(self.root):
            if base == str(self.root):
                dirs[:] = [d for d in dirs if d != QUARANTINE]
            for name in names:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        return total

    def quarantine_bytes(self) -> int:
        total = 0
        for base, _dirs, names in os.walk(self.root / QUARANTINE):
            for name in names:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        return total

    def purge_quarantine(self, now: float, max_age_s: int = 24 * 3600) -> list[str]:
        """Delete quarantined directories not touched for `max_age_s` (rename sets ctime, so this is time since
        quarantine). Returns what was removed."""
        quarantine = self.root / QUARANTINE
        if not quarantine.is_dir():
            return []
        removed = []
        for entry in sorted(quarantine.iterdir()):
            try:
                age = now - max(entry.lstat().st_ctime, entry.lstat().st_mtime)
            except OSError:
                continue
            if age >= max_age_s:
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    removed.append(entry.name)
        return removed

    def pending_removals(self) -> list[str]:
        """Directories a purge renamed away but could not finish deleting."""
        return sorted(p.name for p in self.root.glob(".purging-*")) if self.root.is_dir() else []

    # ── post manifest and drift ─────────────────────────────────────────────
    def write_post(self, checkpoint_id: str, post: dict[str, Any]) -> None:
        """Atomic replace: the latest post-change manifest wins (it is retaken at each tripwire pause)."""
        directory = self.dir_for(checkpoint_id)
        temp = directory / f".{POST}.tmp"
        if temp.exists():
            temp.unlink()
        self._write_new(temp, _dump(post))
        self._fsync_file(temp)
        self._t("rename", directory / POST)
        os.rename(temp, directory / POST)
        self._fsync_dir(directory)

    def save_drift(self, checkpoint_id: str, index: int, sha256: str, data: bytes) -> str:
        directory = self.dir_for(checkpoint_id) / "drift"
        if not directory.exists():
            self._mkdir(directory)
        path = directory / f"{index}.{sha256[:16]}"
        if not path.exists():
            self._write_new(path, data)
            self._fsync_file(path)
        self._fsync_dir(directory)
        return str(path)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def quarantine_uncommitted(self) -> list[str]:
        """Move every checkpoint directory without a COMMITTED marker out of the usable set."""
        moved = []
        for checkpoint_id in self.list_ids():
            if self.is_committed(checkpoint_id):
                continue
            quarantine = self.root / QUARANTINE
            if not quarantine.exists():
                self._mkdir(quarantine)
            target = quarantine / checkpoint_id
            n = 0
            while target.exists():
                n += 1
                target = quarantine / f"{checkpoint_id}.{n}"
            os.rename(self.dir_for(checkpoint_id), target)
            moved.append(checkpoint_id)
        for leftover in self.root.glob(".purging-*") if self.root.is_dir() else ():
            shutil.rmtree(leftover, ignore_errors=True)      # a purge that crashed or failed after the rename
        if moved:
            self._fsync_dir(self.root)
        return moved

    def remove(self, checkpoint_id: str) -> bool:
        """True when nothing is left. A directory that could not be fully deleted stays as `.purging-<id>`
        (unusable, listed by `pending_removals`) and is retried at the next startup scan."""
        directory = self.dir_for(checkpoint_id)
        if not directory.exists():
            return True
        doomed = self.root / f".purging-{checkpoint_id}"
        if doomed.exists():
            shutil.rmtree(doomed, ignore_errors=True)
        os.rename(directory, doomed)          # unusable the instant it leaves the id namespace
        self._fsync_dir(self.root)
        shutil.rmtree(doomed, ignore_errors=True)
        return not doomed.exists()


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, indent=1).encode("utf-8")
