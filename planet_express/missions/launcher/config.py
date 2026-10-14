"""launch.conf: the root-owned file the helper loads itself (contract section 22.2).

Core (unprivileged on Ubuntu) usually cannot read it (0600 root), so Core derives its own copy of the uid range
from `missions.*` in its config, and preflight proves the two agree by asking the helper to launch a probe. This
module renders and parses the file for setup and for a Core that IS root (MOS), and holds the fixed paths.
"""

import json
import os
import stat
from dataclasses import asdict, dataclass

CONF_PATH = "/etc/planetexpress/launch.conf"
HELPER_PATH = "/usr/libexec/planetexpress/pe-launch"
SUDOERS_PATH = "/etc/sudoers.d/planetexpress-launch"
DEFAULT_UID_RANGE = (61000, 61999)
DEFAULT_PIDS_MAX = 512


class ConfError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchConf:
    uid_min: int
    uid_max: int
    core_uid: int
    core_gid: int
    cgroup_root: str
    workspace_root: str
    sock_root: str
    pids_max: int = DEFAULT_PIDS_MAX
    version: int = 1

    def __post_init__(self):
        for name in ("uid_min", "uid_max", "core_uid", "core_gid", "pids_max"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ConfError(f"{name} must be a non-negative integer")
        if not 0 < self.uid_min <= self.uid_max < 2**31:
            raise ConfError("the uid range must be positive and ordered")
        if self.uid_min <= self.core_uid <= self.uid_max:
            raise ConfError("the Core uid must be outside the session uid range")
        paths = (self.cgroup_root, self.workspace_root, self.sock_root)
        for path in paths:
            if (not isinstance(path, str) or not path.startswith("/") or path == "/" or "\0" in path
                    or os.path.normpath(path) != path):
                raise ConfError("conf paths must be normalised absolute paths")
        if len(set(paths)) != 3:
            raise ConfError("cgroup_root, workspace_root and sock_root must differ")

    @property
    def probe_uid(self) -> int:
        """The top of the range is reserved for preflight; `start_session` never allocates it."""
        return self.uid_max

    def render(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def parse(cls, text: str) -> "LaunchConf":
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ConfError("launch.conf is not JSON") from exc
        if not isinstance(raw, dict):
            raise ConfError("launch.conf must be an object")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise ConfError("launch.conf has missing or unknown keys") from exc


def load(path: str = CONF_PATH) -> LaunchConf:
    """Read it as Core (needs read access). Refuses a symlink and a file others can write."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ConfError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ConfError(f"{path} is not a regular file that only its owner can write")
        return LaunchConf.parse(os.read(fd, 16_384).decode("utf-8", errors="replace"))
    finally:
        os.close(fd)


__all__ = ["CONF_PATH", "DEFAULT_UID_RANGE", "HELPER_PATH", "SUDOERS_PATH", "ConfError", "LaunchConf", "load"]
