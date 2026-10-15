"""Backup freshness for the checkpoint floor (contract §4, §7).

A checkpoint does not capture data volumes: that is the backup's job. Before a mission may mutate in-scope
data, Core asks whether each data path has a recent verified backup. The answer comes from the configured
backup integration, and with none configured it is "unverifiable", which refuses unless the operator
acknowledged running without one.

The config-driven implementation reads one marker file per backup source (what the backup tooling touches
after a verified run) and takes its mtime as the last success. Which paths a source covers is configuration.
"""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

State = Literal["ok", "stale", "absent", "unverifiable"]


@dataclass(frozen=True)
class BackupStatus:
    state: State
    last_success_at: int | None = None
    source: str | None = None


class BackupFreshness(Protocol):
    def status(self, path: str, *, now: int, recent_s: int) -> BackupStatus: ...


@dataclass(frozen=True)
class BackupSource:
    path: str          # the directory tree this backup covers
    marker: str        # file whose mtime is the last verified success


class ConfiguredBackupFreshness:
    """`ok` when a source covering the path succeeded within `recent_s`; `stale` when it is older;
    `absent` when sources are configured but none covers the path; `unverifiable` when none is configured
    or the covering source's marker cannot be read."""

    def __init__(self, sources: Iterable[BackupSource] = (),
                 mtime: Callable[[str], float | None] | None = None):
        self.sources = tuple(sources)
        self._mtime = mtime or _file_mtime

    def status(self, path: str, *, now: int, recent_s: int) -> BackupStatus:
        if not self.sources:
            return BackupStatus("unverifiable")
        covering = [s for s in self.sources if _covers(s.path, path)]
        if not covering:
            return BackupStatus("absent")
        newest, newest_source = None, None
        for source in covering:
            seen = self._mtime(source.marker)
            if seen is not None and (newest is None or seen > newest):
                newest, newest_source = int(seen), source.path
        if newest is None:
            return BackupStatus("unverifiable", source=covering[0].path)
        state: State = "ok" if 0 <= now - newest <= recent_s else "stale"
        return BackupStatus(state, newest, newest_source)


def _covers(root: str, path: str) -> bool:
    root = os.path.normpath(root)
    path = os.path.normpath(path)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _file_mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None
