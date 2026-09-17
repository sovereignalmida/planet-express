"""Pause core work while root's backup jobs tear down and restore host stacks.

The root backup job writes the marker in a root-owned directory; core only reads it
and cannot clear another process's maintenance window. Staleness uses filesystem
mtime, not potentially malformed content, so a crashed backup cannot wedge core.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAINTENANCE_STALE_SECONDS = 6 * 3600
_UNREADABLE = "maintenance marker present (unreadable)"
log = logging.getLogger(__name__)
# Only warning deduplication is remembered; window status is always read afresh.
_warned_mtimes: set[tuple[Path, int]] = set()
_warning_lock = threading.Lock()


@dataclass(frozen=True)
class MaintenanceWindow:
    reason: str
    started_at: float | None
    path: Path


def marker_path() -> Path:
    return Path(os.environ.get("CASA_MAINTENANCE_MARKER", "/var/lib/planetexpress/maintenance"))


def active_window(
    path: Path | None = None, *, now: Callable[[], float] = time.time
) -> MaintenanceWindow | None:
    path = marker_path() if path is None else path
    unreadable = MaintenanceWindow(_UNREADABLE, None, path)
    try:
        stat = path.stat()
        if now() - stat.st_mtime > MAINTENANCE_STALE_SECONDS:
            key = (path, stat.st_mtime_ns)
            with _warning_lock:
                if key not in _warned_mtimes:
                    _warned_mtimes.add(key)
                    log.warning("Ignoring stale maintenance marker %s (mtime %s)", path, stat.st_mtime)
            return None
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (PermissionError, ValueError):
        return unreadable
    if not isinstance(data, dict) or not isinstance(data.get("reason"), str) or not data["reason"]:
        return unreadable
    started_at = data.get("started_at")
    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
        started_at = float(started_at)
    else:
        started_at = None
    return MaintenanceWindow(data["reason"], started_at, path)
