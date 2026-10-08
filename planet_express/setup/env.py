"""The host as the setup core sees it: a few read-only primitives, injectable so tests need no real host."""
from __future__ import annotations

import glob
import hashlib
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


MAX_HASHED_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RunResult:
    rc: int
    out: str


class SystemEnv:
    """The real host. Every method is read-only and never raises for an absent thing."""

    def read(self, path: str) -> str | None:
        try:
            return Path(path).read_text(errors="replace")
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def sha256(self, path: str) -> str | None:
        """Hash of the file's exact bytes, or None if it cannot be read or is too large to be one of
        ours. Text reads are lossy, so a compare-and-swap must never be built on them."""
        try:
            if os.path.getsize(path) > MAX_HASHED_BYTES:
                return None
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def glob(self, pattern: str) -> list[str]:
        return sorted(glob.glob(pattern))

    def run(self, argv: list[str], timeout: float = 10) -> RunResult:
        """argv only, never a shell. 127 = not found, 124 = timed out, 126 = could not run."""
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  check=False, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return RunResult(127, "")
        except subprocess.TimeoutExpired:
            return RunResult(124, "")
        except OSError:
            return RunResult(126, "")
        return RunResult(done.returncode, done.stdout)

    def euid(self) -> int:
        return os.geteuid()

    def hostname(self) -> str:
        return socket.gethostname()

    def machine(self) -> str:
        return platform.machine()

    def free_gib(self, path: str) -> float | None:
        try:
            return round(shutil.disk_usage(path).free / 2**30, 1)
        except OSError:
            return None
