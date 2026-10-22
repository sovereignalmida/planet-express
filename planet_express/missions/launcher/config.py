"""launch.conf: the root-owned file the helper loads itself (contract section 22.2).

Core (unprivileged on Ubuntu) usually cannot read it (0600 root), so Core derives its own copy of the uid range
from `missions.*` in its config, and preflight proves the two agree by asking the helper to launch a probe. This
module renders and parses the file for setup and for a Core that IS root (MOS), and holds the fixed paths.
"""

import json
import os
import stat
from dataclasses import asdict, dataclass

CONF_PATH = "/etc/planetexpress-launch/launch.conf"   # a root-owned directory of its own (0755); pe_launch.py duplicates it
HELPER_PATH = "/usr/libexec/planetexpress/pe-launch"
SUDOERS_PATH = "/etc/sudoers.d/planetexpress-launch"
SYSTEMD_DYNAMIC_UIDS = (61184, 65519)  # systemd DynamicUser= allocates here; a session uid in it could be another service's
DEFAULT_UID_RANGE = (200000, 200999)   # clear of systemd's DynamicUser range and of normal accounts: the ONE default
DEFAULT_PIDS_MAX = 512


def mos_conf_path(home: str) -> str:
    """MOS root is RAM, so launch.conf lives under the data pool, in a `launch` directory setup makes root-owned 0755.
    The helper installed on MOS has this path written into it; Core is told it as `missions.launch_conf`."""
    return f"{home.rstrip('/')}/launch/launch.conf"


def mos_helper_store_path(home: str) -> str:
    """The persistent copy of the helper on MOS (root is RAM): beside launch.conf, installed into HELPER_PATH at boot."""
    return f"{home.rstrip('/')}/launch/pe-launch"


def mos_launcher_paths(home: str) -> dict:
    """The three MOS-only launcher paths, derived from PE_HOME in one place (the setup plan and the CLI both use it)."""
    home = home.rstrip("/")
    return {"boot_script": f"{home}/launcher-boot.sh", "conf": mos_conf_path(home),
            "helper_store": mos_helper_store_path(home)}


def ancestor_problem(path: str, uid: int, mode: int, is_link: bool = False) -> str | None:
    """Why a directory above a trusted file cannot be trusted by the helper (None if it can), with the exact fix. The rule
    is the helper's own: a real directory, owned by root, not group/world writable. Nothing here relaxes it."""
    if is_link:
        return f"{path} is a symlink; the helper refuses a symlink anywhere in the chain. Replace it with a real directory"
    if uid != 0 or mode & 0o022:
        return (f"{path} is owned by uid {uid} with mode {mode & 0o7777:04o}, but the helper trusts only root-owned "
                f"directories nobody else can write. Fix: chown root:root {path} && chmod 755 {path}")
    return None


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


__all__ = ["CONF_PATH", "DEFAULT_UID_RANGE", "SYSTEMD_DYNAMIC_UIDS", "HELPER_PATH", "SUDOERS_PATH", "mos_conf_path", "mos_helper_store_path", "mos_launcher_paths", "ancestor_problem", "ConfError", "LaunchConf", "load"]
