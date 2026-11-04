"""Setup steps for the session launcher (contract 22.5): each named, each with a probe, a repair and an undo.

    session-uid-range   launch.conf: the reserved uid range (and Core's identity), checked free of accounts not PE's own
    launcher-helper     the root-owned pe-launch under a root-owned, non-symlink chain
    helper-store        (MOS) its persistent copy under the data pool, installed at every boot by the boot script
    launcher-sudoers    Ubuntu only: `Core ALL=(root) NOPASSWD: pe-launch ""`, checked with visudo
    session-access      Ubuntu only (trust mode): group `pe-session`, a `pe-s<N>` system account per session uid, and the
                        one session sudoers rule `%pe-session ALL=(ALL:ALL) NOPASSWD: ALL`, checked with visudo
    cgroup-tree         systemd: a Delegate=yes drop-in; MOS: a boot script on the data pool
    workspace-root      where each session's own directory is made (Core owned, traversable)
    socket-root         the per-session socket directories' parent (Core owned, 0711)

Same discipline as `planet_express/setup/` (probe reads only; repair compares what it saw and refuses over
anything that is not setup's; undo reverts from journaled evidence and refuses once the host has moved), written
against plain paths so they can run on a throwaway tree in tests. They are entries in the setup plan's
catalogue as `launcher.step` and `launcher.remove` (`planet_express/setup/handlers.py` wraps each of these in the plan's
check/act/verify/inverse discipline); `launcher_steps()` is the one place a plan builder (or
`python -m ...launcher.setup_steps`) takes them from. Removal (uninstall) takes away only what is byte-for-byte what
setup would write and only a directory that is empty; undoing a removal puts it back.

Nothing here executes a shell. `visudo -c`, and `groupadd`/`useradd`/`usermod`/`userdel`/`groupdel` for the session
accounts, are run as argv with a minimal environment.
"""

import hashlib
import os
import re
import shlex
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from config_schema import MissionsConfig
from planet_express.missions.launcher import pe_launch
from planet_express.missions.launcher.config import (
    CONF_PATH,
    DEFAULT_PIDS_MAX,
    HELPER_PATH,
    SESSION_GROUP,
    SESSION_SUDOERS_PATH,
    SUDOERS_PATH,
    ConfError,
    LaunchConf,
    ancestor_problem,
    mos_launcher_paths,
)

SOURCE = Path(pe_launch.__file__)
_SHELL_SAFE = re.compile(r"/[A-Za-z0-9._@+/-]*")
_UNIX_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
UNIT_DROPIN = "/etc/systemd/system/casa-planetexpress.service.d/launcher.conf"
_RUN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


class StepError(Exception):
    """The step cannot be repaired safely; the message says why and what to do."""


class StepRefused(Exception):
    """Undo found the host no longer matches what setup left; nothing was reverted."""


@dataclass
class Probe:
    ok: bool
    detail: str = ""
    blocked: bool = False            # not satisfied AND not safely repairable: an operator must look


_DEFAULTS = MissionsConfig()           # one source of truth: what Core reads is what launch.conf is written from


@dataclass
class Paths:
    """Everything a step touches. The cgroup root, socket root and uid range default to the `missions.*` defaults
    (and `Paths.from_missions` takes them from a real config), so Core and the helper cannot disagree; the
    workspace root has no default because Core's own default depends on where the state database lives. Tests point
    the rest at a temp tree."""
    core_uid: int
    core_gid: int
    workspace_root: str
    core_user: str = "planetexpress"
    uid_min: int = _DEFAULTS.session_uid_min
    uid_max: int = _DEFAULTS.session_uid_max
    pids_max: int = DEFAULT_PIDS_MAX
    log_groups: tuple = ()
    cgroup_root: str = _DEFAULTS.cgroup_root
    sock_root: str = _DEFAULTS.sock_root
    helper: str = HELPER_PATH
    conf: str = CONF_PATH
    helper_store: str = ""                        # MOS only: the persistent copy the boot script installs from
    sudoers: str = SUDOERS_PATH
    session_sudoers: str = SESSION_SUDOERS_PATH   # the one rule that gives every session sudo (trust mode)
    session_group: str = SESSION_GROUP
    groupadd: str = "/usr/sbin/groupadd"
    useradd: str = "/usr/sbin/useradd"
    usermod: str = "/usr/sbin/usermod"
    userdel: str = "/usr/sbin/userdel"
    groupdel: str = "/usr/sbin/groupdel"
    gpasswd: str = "/usr/bin/gpasswd"
    unit_dropin: str = UNIT_DROPIN
    boot_script: str = "/mnt/data/pe/launcher-boot.sh"
    interpreter: str = "/usr/bin/python3"
    flavour: str = "systemd"                       # or "mos"
    owner_uid: int = 0                             # who owns the root-owned files (0; tests use their own uid)
    owner_gid: int = 0
    below: str = "/"                               # tests anchor the trusted-path walk inside a temp tree
    passwd: str = "/etc/passwd"
    group: str = "/etc/group"
    visudo: str = "/usr/sbin/visudo"
    systemctl: str = "/usr/bin/systemctl"
    sudoers_main: str = "/etc/sudoers"
    sudoers_dir: str = "/etc/sudoers.d"
    setfacl: str = "/usr/bin/setfacl"
    getfacl: str = "/usr/bin/getfacl"
    mountinfo: str = "/proc/self/mountinfo"
    check_delegation: bool = True                 # require Core's cgroup to exist and be Core's (tests may turn it off)
    run: Callable = field(default=subprocess.run, repr=False)

    @classmethod
    def from_missions(cls, missions, *, state_dir: str, **kw) -> "Paths":
        """Build from the `missions` section of Core's config so the two sides share one set of values."""
        return cls(uid_min=missions.session_uid_min, uid_max=missions.session_uid_max, cgroup_root=missions.cgroup_root,
                   sock_root=missions.sock_root, log_groups=tuple(missions.session_groups),
                   workspace_root=missions.workspace_root or os.path.join(state_dir, "mission-workspaces"), **kw)

    def launch_conf(self) -> LaunchConf:
        return LaunchConf(uid_min=self.uid_min, uid_max=self.uid_max, core_uid=self.core_uid, core_gid=self.core_gid,
                          cgroup_root=self.cgroup_root, workspace_root=self.workspace_root, sock_root=self.sock_root,
                          pids_max=self.pids_max, log_groups=tuple(self.log_groups))


# ── small, careful file operations ─────────────────────────────────────────────────────────────────

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8", errors="replace")


def read_regular(path: str) -> bytes | None:
    """The bytes of a regular, non-symlink file, None if absent. A symlink or other kind raises StepError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StepError(f"{path} is not a plain file ({exc.strerror})") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StepError(f"{path} is not a regular file")
        return os.read(fd, 1 << 20)
    finally:
        os.close(fd)


def write_atomic(path: str, data: bytes, mode: int, uid: int, gid: int) -> None:
    """Temp file next to the target (O_EXCL, never following a link), fsync, set owner and mode, rename over."""
    directory = os.path.dirname(path)
    temp = os.path.join(directory, f".{os.path.basename(path)}.pe-tmp")
    try:
        os.unlink(temp)
    except FileNotFoundError:
        pass
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, data)
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def ensure_dir(path: str, mode: int, uid: int, gid: int, created: list[str]) -> None:
    """Create `path` (and any missing parents, each with `mode`), recording what was created for undo. A path that
    exists must be a real directory; its mode and owner are corrected only for the final component."""
    parts, missing = Path(path), []
    while not os.path.lexists(parts):
        missing.append(str(parts))
        parts = parts.parent
    for name in reversed(missing):
        os.mkdir(name, mode)
        os.chown(name, uid, gid)
        os.chmod(name, mode)
        created.append(name)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StepError(f"{path} exists and is not a directory")
    if stat.S_IMODE(info.st_mode) != mode or (info.st_uid, info.st_gid) != (uid, gid):
        os.chown(path, uid, gid)
        os.chmod(path, mode)


def ensure_parent(path: str, mode: int, uid: int, gid: int, created: list[str]) -> None:
    """Make `path` and any missing parents (each with `mode`), recording what was created. Unlike `ensure_dir` it never
    touches a directory that already exists: that may be somebody else's (the service's own config directory, say), so
    its owner and mode are left exactly as they are, and a probe of the chain decides whether it is acceptable."""
    parts, missing = Path(path), []
    while not os.path.lexists(parts):
        missing.append(str(parts))
        parts = parts.parent
    for name in reversed(missing):
        os.mkdir(name, mode)
        os.chown(name, uid, gid)
        os.chmod(name, mode)
        created.append(name)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise StepError(f"{path} exists and is not a directory")


def _chain_fix(path: str, paths: Paths, file_ok: bool) -> str:
    """When the helper's own walk refuses on ownership or mode, name the first directory it refuses and the exact fix."""
    parts = [p for p in path.split("/") if p]
    current = ""
    for index in range(len(parts) + 1):
        directory = current or "/"
        if index == len(parts) and file_ok:
            return ""
        if paths.below != "/" and (paths.below == directory or paths.below.startswith(directory.rstrip("/") + "/")):
            current = current + "/" + parts[index] if index < len(parts) else current
            continue
        try:
            info = os.lstat(directory)
        except OSError:
            return ""
        problem = ancestor_problem(directory, info.st_uid if info.st_uid != paths.owner_uid else 0, info.st_mode,
                                   stat.S_ISLNK(info.st_mode))
        if problem and (info.st_uid != paths.owner_uid or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode)):
            return f" ({problem})"
        if index < len(parts):
            current = current + "/" + parts[index]
    return ""


def trusted_chain(path: str, paths: Paths) -> str | None:
    """None if every ancestor and the file are root-owned, unwritable by others, and no symlink; else why not."""
    try:
        pe_launch.check_trusted_path(path, trusted_uid=paths.owner_uid, below=paths.below)
    except pe_launch.Refused as exc:
        return f"{path}: {exc.code}{_chain_fix(path, paths, True)}"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    return None


def trusted_dir_chain(path: str, paths: Paths) -> str | None:
    try:
        pe_launch.check_trusted_path(path, trusted_uid=paths.owner_uid, file_ok=False, below=paths.below)
    except pe_launch.Refused as exc:
        return f"{path}: {exc.code}{_chain_fix(path, paths, False)}"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    return None


class Step:
    name = ""
    ubuntu_only = False

    def __init__(self, paths: Paths):
        self.p = paths

    def applies(self) -> bool:
        return not (self.ubuntu_only and self.p.flavour != "systemd")

    def probe(self) -> Probe:
        raise NotImplementedError

    def repair(self, journal) -> dict:
        raise NotImplementedError

    def undo(self, journal, evidence: dict) -> str:
        raise NotImplementedError

    def preflight(self) -> str | None:
        """Why no launcher step should write anything on this host yet, or None."""
        return None

    def written(self) -> Probe:
        """Is what setup writes in place? Differs from `probe` only where a step needs more than a file (a restart)."""
        return self.probe()

    def owned_file(self) -> tuple[str, bytes, int] | None:
        """(path, content, mode) of the one file this step writes, or None if it writes none (or cannot say safely)."""
        return None

    def owned_dir(self) -> tuple[str, int, int, int] | None:
        """(path, mode, uid, gid) of the one directory this step makes, or None."""
        return None

    def removal(self) -> tuple[str | None, str]:
        """(what removal would take away, why nothing otherwise). Never anything that is not exactly what setup writes:
        a file with other content, or a directory with anything in it, stays. A symlink where a file belongs is an error."""
        file = self.owned_file()
        if file is not None:
            path, data, _ = file
            current = read_regular(path)
            if current is None:
                return None, f"{path}: already absent"
            if current != data:
                return None, f"{path}: left in place, it is not what setup writes"
            return path, ""
        directory = self.owned_dir()
        if directory is None:
            return None, "nothing to remove"
        path, mode, uid, gid = directory
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None, f"{path}: already absent"
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode:
            return None, f"{path}: left in place, it is not the directory setup makes"
        if os.listdir(path):
            return None, f"{path}: left in place, it is not empty"
        return path, ""

    def remove(self, journal) -> str:
        path, why = self.removal()
        if path is None:
            return why
        file = self.owned_file()
        if file is not None:
            _, data, mode = file
            journal.append("evidence", step=self.name, data={"removed": {"kind": "file", "path": path, "mode": mode,
                                                                          "content": data.decode("utf-8")}})
            os.unlink(path)
        else:
            _, mode, uid, gid = self.owned_dir()
            journal.append("evidence", step=self.name, data={"removed": {"kind": "dir", "path": path, "mode": mode,
                                                                          "uid": uid, "gid": gid}})
            os.rmdir(path)
        self._after_change()
        return f"{path}: removed"

    def restore(self, evidence: dict) -> str:
        """Undo a `remove`: put back what it took, only if nothing has taken its place."""
        gone = evidence.get("removed")
        if not gone:
            return "nothing was removed"
        path = gone["path"]
        if os.path.lexists(path):
            raise StepRefused(f"{path} exists again; left as it is")
        if gone["kind"] == "file":
            ensure_parent(os.path.dirname(path), 0o755, self.p.owner_uid, self.p.owner_gid, [])
            problem = trusted_dir_chain(os.path.dirname(path), self.p)
            if problem:
                raise StepRefused(f"not restoring {path}: {problem}")
            write_atomic(path, gone["content"].encode("utf-8"), gone["mode"], self.p.owner_uid, self.p.owner_gid)
        else:
            ensure_dir(path, gone["mode"], gone["uid"], gone["gid"], [])
        self._after_change()
        return f"{path}: put back"

    def _after_change(self) -> None:
        """Tell the host a file this step owns came or went, where it needs telling (the cgroup drop-in)."""

    # Directories made on the way are journaled as an intent BEFORE they exist, so a crash cannot leave one unrecorded.
    def dir_identity(self) -> tuple[int, int]:
        """(uid, mode) of the directories this step makes, which is how an interrupted creation is recognised."""
        return self.p.owner_uid, 0o755

    def _journal_intent(self, journal, directory: str) -> None:
        missing, probe = [], Path(directory)
        while not os.path.lexists(probe):
            missing.append(str(probe))
            probe = probe.parent
        if missing:
            journal.append("evidence", step=self.name, data={"intent_dirs": list(reversed(missing))})

    def _make_parents(self, journal, directory: str) -> list[str]:
        created: list[str] = []
        self._journal_intent(journal, directory)
        ensure_parent(directory, 0o755, self.p.owner_uid, self.p.owner_gid, created)
        return created

    def adopt_interrupted(self, evidence: dict) -> list[str]:
        """After a crash: the directories that were about to be made and now exist, exactly as this step makes them, are
        this step's, so undo removes them. A directory that was there before the intent is never in the list."""
        if "intent_dirs" not in evidence:
            return []
        uid, mode = self.dir_identity()
        found = []
        for directory in evidence["intent_dirs"]:
            try:
                info = os.lstat(directory)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == mode:
                found.append(directory)
        return found

    # a shared undo for a file this step wrote: remove it only if it is still byte-for-byte what we wrote
    def _undo_file(self, path: str, evidence: dict) -> str:
        wrote = evidence.get("sha256")
        if wrote is None:
            return f"{path}: setup did not write it"
        current = read_regular(path)
        if current is None:
            return f"{path}: already gone"
        if sha256(current) != wrote:
            raise StepRefused(f"{path} was changed after setup wrote it; left in place")
        os.unlink(path)
        return f"{path}: removed"

    def _undo_dirs(self, evidence: dict) -> list[str]:
        removed = []
        for directory in reversed(evidence.get("created_dirs", [])):
            try:
                os.rmdir(directory)
                removed.append(directory)
            except OSError:
                pass                       # not empty, or not ours any more: leave it
        return removed


# ── 1. the uid range and launch.conf ───────────────────────────────────────────────────────────────

class SessionUidRange(Step):
    name = "session-uid-range"

    def _collisions(self) -> list[str]:
        """Accounts and groups that already use a number in the range and are not PE's own: a passwd entry by uid OR by
        primary gid, a group by gid (the helper and the preflight refuse each of these, so setup must not report success
        over one). PE's own are what `session-access` makes: user `pe-s<N>` with uid and primary gid N, group `pe-s<N>` with
        gid N and no members."""
        out = []
        lo, hi = self.p.uid_min, self.p.uid_max
        for path, kind, fields_to_check in ((self.p.passwd, "user", (2, 3)), (self.p.group, "group", (2,))):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                fields = line.split(":")
                numbers = [int(fields[i]) for i in fields_to_check if len(fields) > i and fields[i].isdigit()]
                hit = [n for n in numbers if lo <= n <= hi]
                if hit and not _pe_entry(kind, fields):
                    out.append(f"{kind} {fields[0]} ({hit[0]})")
        return out

    def owned_file(self):
        try:
            return self.p.conf, self.p.launch_conf().render().encode(), 0o600
        except ConfError:
            return None

    def probe(self) -> Probe:
        try:
            expected = self.p.launch_conf()
        except ConfError as exc:
            return Probe(False, str(exc), blocked=True)
        hits = self._collisions()
        if hits:
            return Probe(False, f"the range {self.p.uid_min}-{self.p.uid_max} is already used by {', '.join(hits[:3])}",
                         blocked=True)
        directory = os.path.dirname(self.p.conf)
        problem = trusted_dir_chain(directory, self.p) if os.path.isdir(directory) else None
        if problem:
            return Probe(False, f"{problem}: launch.conf needs a root-owned directory that others cannot write", blocked=True)
        try:
            current = read_regular(self.p.conf)
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        if current is None:
            return Probe(False, f"{self.p.conf} is absent")
        if current.decode("utf-8", errors="replace") != expected.render():
            return Probe(False, f"{self.p.conf} differs from the intended range and paths")
        info = os.lstat(self.p.conf)
        if info.st_uid != self.p.owner_uid or info.st_mode & 0o077:
            return Probe(False, f"{self.p.conf} must be owned by root and mode 0600")
        problem = trusted_chain(self.p.conf, self.p)
        return Probe(problem is None, problem or f"{self.p.uid_min}-{self.p.uid_max}", blocked=problem is not None)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        data = self.p.launch_conf().render().encode()
        previous = read_regular(self.p.conf)
        created = self._make_parents(journal, os.path.dirname(self.p.conf))
        problem = trusted_dir_chain(os.path.dirname(self.p.conf), self.p)
        if problem:
            raise StepError(f"refusing to write launch.conf: {problem}")
        journal.append("evidence", step=self.name, data={"previous_sha256": None if previous is None else sha256(previous),
                                                         "previous": _text(previous),
                                                         "sha256": sha256(data), "created_dirs": created})
        write_atomic(self.p.conf, data, 0o600, self.p.owner_uid, self.p.owner_gid)
        if not self.probe().ok:
            raise StepError("launch.conf was written but does not read back as intended")
        return {"sha256": sha256(data), "created_dirs": created, "previous": None if previous is None else previous.decode()}

    def undo(self, journal, evidence: dict) -> str:
        previous = evidence.get("previous")
        detail = self._undo_file(self.p.conf, evidence)
        if previous is not None and not os.path.exists(self.p.conf):
            write_atomic(self.p.conf, previous.encode(), 0o600, self.p.owner_uid, self.p.owner_gid)
            detail += " (the previous file was restored)"
        self._undo_dirs(evidence)
        return detail


# ── 2. the helper ──────────────────────────────────────────────────────────────────────────────────

class LauncherHelper(Step):
    name = "launcher-helper"

    def target(self) -> str:
        return self.p.helper

    def wanted(self) -> bytes:
        lines = SOURCE.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines or not lines[0].startswith("#!"):
            raise StepError("the helper source has no interpreter line")
        # Two edits. The interpreter line names this host's python3, with -I (isolated) and -S (no site). And where the
        # trust file is not at the fixed default (MOS: root is RAM, so it is under the data pool), the one CONF_PATH line
        # names it: the helper never takes that path from its caller, only from its own root-owned text.
        body = "".join(lines[1:])
        if self.p.conf != CONF_PATH:
            if not _SHELL_SAFE.fullmatch(self.p.conf):
                raise StepError("the launch.conf path has characters the helper text will not carry")
            default_line = f'CONF_PATH = "{CONF_PATH}"'
            if body.count(default_line) != 1:
                raise StepError("the helper source has no single CONF_PATH line to rewrite")
            body = body.replace(default_line, f'CONF_PATH = "{self.p.conf}"')
        return (f"#!{self.p.interpreter} -IS\n" + body).encode("utf-8")

    def owned_file(self):
        try:
            return self.target(), self.wanted(), 0o755
        except StepError:
            return None

    def probe(self) -> Probe:
        try:
            current = read_regular(self.target())
            wanted = self.wanted()
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        problem = trusted_dir_chain(os.path.dirname(self.target()), self.p) if os.path.isdir(os.path.dirname(self.target())) else None
        if problem:
            return Probe(False, problem, blocked=True)
        if current is None:
            return Probe(False, f"{self.target()} is absent")
        if current != wanted:
            return Probe(False, f"{self.target()} differs from this version's helper")
        info = os.lstat(self.target())
        if info.st_uid != self.p.owner_uid or stat.S_IMODE(info.st_mode) != 0o755:
            return Probe(False, f"{self.target()} must be owned by root with mode 0755")
        problem = trusted_chain(self.target(), self.p)
        return Probe(problem is None, problem or self.target(), blocked=problem is not None)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        wanted, previous = self.wanted(), read_regular(self.target())
        created = self._make_parents(journal, os.path.dirname(self.target()))
        problem = trusted_dir_chain(os.path.dirname(self.target()), self.p)
        if problem:
            raise StepError(f"refusing to install the helper: {problem}; fix the directory chain first")
        journal.append("evidence", step=self.name, data={"previous_sha256": None if previous is None else sha256(previous),
                                                         "previous": _text(previous),
                                                         "sha256": sha256(wanted), "created_dirs": created})
        write_atomic(self.target(), wanted, 0o755, self.p.owner_uid, self.p.owner_gid)
        if not self.probe().ok:
            raise StepError("the helper was installed but does not pass its own checks")
        return {"sha256": sha256(wanted), "created_dirs": created,
                "previous": None if previous is None else previous.decode("utf-8", errors="replace")}

    def undo(self, journal, evidence: dict) -> str:
        detail = self._undo_file(self.target(), evidence)
        previous = evidence.get("previous")
        if previous is not None and not os.path.exists(self.target()):
            write_atomic(self.target(), previous.encode(), 0o755, self.p.owner_uid, self.p.owner_gid)
            detail += " (the previous file was restored)"
        self._undo_dirs(evidence)
        return detail


# ── 3. sudoers (Ubuntu only) ───────────────────────────────────────────────────────────────────────


class HelperStore(LauncherHelper):
    """MOS only. Root is RAM there, so the helper at its real path is gone after every boot. This keeps a persistent copy
    under the data pool (the same bytes, root-owned 0755, the whole chain checked exactly as for the live copy) which the
    boot script installs into the real path at every boot, only if its sha256 is the one setup recorded."""
    name = "helper-store"

    def applies(self) -> bool:
        return self.p.flavour == "mos" and bool(self.p.helper_store)

    def target(self) -> str:
        return self.p.helper_store


class LauncherSudoers(Step):
    name = "launcher-sudoers"
    ubuntu_only = True            # MOS has no sudo; and this step is never skipped because a legacy sudoers file exists

    @property
    def target(self) -> str:
        return self.p.sudoers

    def wanted(self) -> bytes:
        helper, user = self.p.helper, self.p.core_user
        return (f"# Managed by Planet Express (session launcher). Do not edit.\n"
                f"Defaults!{helper} env_reset, !setenv, !env_editor\n"
                f"{user} ALL=(root) NOPASSWD: {helper} \"\"\n").encode()

    def _visudo_ok(self, path: str) -> bool:
        try:
            result = self.p.run([self.p.visudo, "-c", "-f", path], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=30, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def unsafe(self) -> str | None:
        """Both values become text in a sudoers rule: a user name and an absolute path of plain characters, nothing else
        (no newline, no space, no glob, no `,` or `:` that sudoers gives a meaning)."""
        if not _UNIX_NAME.fullmatch(self.p.core_user):
            return f"{self.p.core_user!r} is not a plain unix user name"
        if not _SHELL_SAFE.fullmatch(self.p.helper):
            return f"the helper path {self.p.helper!r} has characters that are not allowed in a sudoers rule"
        return None

    def owned_file(self):
        return None if self.unsafe() else (self.target, self.wanted(), 0o440)

    def probe(self) -> Probe:
        problem = self.unsafe()
        if problem:
            return Probe(False, problem, blocked=True)
        try:
            current = read_regular(self.target)
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        if current is None:
            return Probe(False, f"{self.target} is absent")
        if current != self.wanted():
            return Probe(False, f"{self.target} differs from the launcher rule")
        info = os.lstat(self.target)
        if info.st_uid != self.p.owner_uid or stat.S_IMODE(info.st_mode) != 0o440:
            return Probe(False, f"{self.target} must be owned by root with mode 0440")
        if not self._visudo_ok(self.target):
            return Probe(False, "visudo -c rejects the sudoers fragment", blocked=True)
        return Probe(True, self.target)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        wanted, previous = self.wanted(), read_regular(self.target)
        staging = os.path.join(os.path.dirname(self.target), f".{os.path.basename(self.target)}.check")
        write_atomic(staging, wanted, 0o440, self.p.owner_uid, self.p.owner_gid)
        try:
            if not self._visudo_ok(staging):
                raise StepError("visudo -c rejects the new sudoers fragment; nothing was installed")
        finally:
            try:
                os.unlink(staging)          # the staging name is dot-prefixed, which sudoers ignores, but never leave it
            except FileNotFoundError:
                pass
        journal.append("evidence", step=self.name, data={"previous_sha256": None if previous is None else sha256(previous),
                                                         "previous": _text(previous),
                                                         "sha256": sha256(wanted), "created_dirs": []})
        write_atomic(self.target, wanted, 0o440, self.p.owner_uid, self.p.owner_gid)
        if not self.probe().ok:
            raise StepError("the sudoers fragment was installed but does not read back as intended")
        return {"sha256": sha256(wanted), "created_dirs": [],
                "previous": None if previous is None else previous.decode("utf-8", errors="replace")}

    def undo(self, journal, evidence: dict) -> str:
        detail = self._undo_file(self.target, evidence)
        previous = evidence.get("previous")
        if previous is not None and not os.path.exists(self.target):
            write_atomic(self.target, previous.encode(), 0o440, self.p.owner_uid, self.p.owner_gid)
        return detail


SESSION_SUDOERS_RULE = b"%pe-session ALL=(ALL:ALL) NOPASSWD: ALL\n"
SESSION_HOME = "/nonexistent"
SESSION_SHELL = "/usr/sbin/nologin"


def account_name(uid: int) -> str:
    """Setup's account (and private group) for session uid N. pe_launch.ACCOUNT_PREFIX is the same prefix."""
    return f"{pe_launch.ACCOUNT_PREFIX}{uid}"


def _pe_entry(kind: str, fields: list[str]) -> bool:
    """Is this /etc/passwd ("user") or /etc/group ("group") line exactly one `session-access` makes? A user `pe-s<N>` with
    uid N and primary gid N; a group `pe-s<N>` with gid N and no members. The helper accepts exactly these (and nothing else)."""
    if len(fields) < 4 or not fields[2].isdigit():
        return False
    number = int(fields[2])
    if fields[0] != account_name(number):
        return False
    if kind == "user":
        return fields[3] == fields[2]
    return not fields[3].strip()


def _db(path: str) -> list[list[str]]:
    """The colon-separated lines of /etc/passwd or /etc/group (missing file: none). A symlink or other kind raises StepError."""
    data = read_regular(path)
    return [line.split(":") for line in (data or b"").decode("utf-8", errors="replace").splitlines() if line and not line.startswith("#")]


class SessionAccess(Step):
    """Trust mode (docs/designs/v4-trust-mode.md): every session gets real access, and sudo is one of the ways in. sudo refuses
    a uid with no account, so every session uid N in the configured range gets a system account `pe-s<N>` (primary group
    `pe-s<N>`, gid N, no members; home /nonexistent; shell /usr/sbin/nologin: nobody logs in as it) that is a member of the
    system group `pe-session`, whose gid is outside the range. `/etc/sudoers.d/planetexpress-sessions` holds exactly
    `%pe-session ALL=(ALL:ALL) NOPASSWD: ALL` (root 0440, `visudo -cf` on a staging copy before it is moved into place). The
    launcher gives every session the `pe-session` group explicitly (launch.conf's session groups), so the rule matches
    whichever way sudo reads a session's groups. Same discipline as the others: the probe reads only (the passwd and group
    files, the sudoers file); repair journals each account or group BEFORE it is made, so a crash leaves nothing unrecorded;
    undo removes exactly what it made, and refuses if any of it has changed since. An account or group in the range that is not
    shaped as PE's own is never adopted or changed: the step is blocked and names it. Removal (uninstall) takes away the
    sudoers rule only; the accounts stay (no login, no home, no sudo once the rule is gone) and setup says so."""
    name = "session-access"
    ubuntu_only = True            # MOS has no sudo, and Core runs sessions there as root with no account needed

    @property
    def target(self) -> str:
        return self.p.session_sudoers

    def wanted(self) -> bytes:
        return SESSION_SUDOERS_RULE          # exactly the one rule

    def owned_file(self):
        return self.target, self.wanted(), 0o440

    def _run(self, argv: list[str]) -> None:
        result = self.p.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=_RUN_ENV, check=False)
        if result.returncode != 0:
            raise StepError(f"{os.path.basename(argv[0])} failed: "
                            f"{(result.stderr or b'').decode(errors='replace').strip()[:200] or f'exit {result.returncode}'}")

    def _visudo_ok(self, path: str) -> bool:
        try:
            result = self.p.run([self.p.visudo, "-c", "-f", path], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=30, env=_RUN_ENV, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _state(self) -> tuple[list[str], dict]:
        """(why it is blocked, what is missing): reads the passwd and group files only. Blocked: a group or account that is
        not shaped as PE's own where PE's must go, a `pe-session` group with a gid it must not have, a sudoers file that is
        not a plain file. Missing: the group, accounts, private groups or memberships setup makes, and the rule."""
        p = self.p
        lo, hi = p.uid_min, p.uid_max
        users, groups = _db(p.passwd), _db(p.group)
        blocked: list[str] = []
        session = next((g for g in groups if g and g[0] == p.session_group), None)
        members: set[str] = set()
        if session is not None:
            gid = int(session[2]) if len(session) > 2 and session[2].isdigit() else -1
            if gid <= 0 or lo <= gid <= hi or gid == p.core_gid:
                blocked.append(f"group {p.session_group} has gid {session[2] if len(session) > 2 else '?'}, which is root's, "
                               f"Core's or inside the session range {lo}-{hi}")
            members = {m for m in (session[3] if len(session) > 3 else "").split(",") if m}
        by_uid = {int(u[2]): u for u in users if len(u) > 3 and u[2].isdigit() and lo <= int(u[2]) <= hi}
        by_gid = {int(g[2]): g for g in groups if len(g) > 3 and g[2].isdigit() and lo <= int(g[2]) <= hi}
        names_user = {u[0]: u for u in users}
        names_group = {g[0]: g for g in groups}
        missing_users, missing_groups, missing_members = [], [], []
        for n in range(lo, hi + 1):
            name = account_name(n)
            user, group = by_uid.get(n), by_gid.get(n)
            if group is None and name in names_group:
                blocked.append(f"group {name} exists with another gid")
            elif group is not None and not _pe_entry("group", group):
                blocked.append(f"group {group[0]} ({n}) is not PE's own")
            elif group is None:
                missing_groups.append(n)
            if user is None and name in names_user:
                blocked.append(f"user {name} exists with another uid")
            elif user is not None and not (_pe_entry("user", user) and user[5:7] == [SESSION_HOME, SESSION_SHELL]):
                blocked.append(f"user {user[0]} ({n}) is not PE's own")
            elif user is None:
                missing_users.append(n)
            elif name not in members:
                missing_members.append(n)
        blocked += [f"user {u[0]} has primary gid {u[3]} in the session range" for u in users
                    if len(u) > 3 and u[3].isdigit() and lo <= int(u[3]) <= hi and int(u[3]) not in by_uid]
        try:
            current = read_regular(self.target)
        except StepError as exc:
            blocked.append(str(exc))
            current = None
        sudoers = "ok"
        if current is None:
            sudoers = "absent"
        elif current != self.wanted():
            sudoers = "differs"
        else:
            info = os.lstat(self.target)
            if info.st_uid != p.owner_uid or info.st_gid != p.owner_gid or stat.S_IMODE(info.st_mode) != 0o440:
                sudoers = "mode"
        return blocked, {"group": session is None, "groups": missing_groups, "users": missing_users,
                         "members": missing_members, "sudoers": sudoers}

    @staticmethod
    def _describe(missing: dict) -> str:
        parts = []
        if missing["group"]:
            parts.append("group pe-session")
        if missing["users"] or missing["groups"]:
            parts.append(f"{len(set(missing['users']) | set(missing['groups']))} session account(s)")
        if missing["members"]:
            parts.append(f"{len(missing['members'])} pe-session membership(s)")
        if missing["sudoers"] != "ok":
            parts.append({"absent": "the session sudoers rule", "differs": "the session sudoers rule (it differs)",
                          "mode": "the session sudoers rule's owner/mode (root 0440)"}[missing["sudoers"]])
        return ", ".join(parts)

    def probe(self) -> Probe:
        if self.p.session_group != SESSION_GROUP:
            return Probe(False, f"the session group must be {SESSION_GROUP}: the sudoers rule names it", blocked=True)
        try:
            blocked, missing = self._state()
        except (StepError, OSError) as exc:
            return Probe(False, f"cannot read the account databases: {exc}", blocked=True)
        if blocked:
            return Probe(False, "; ".join(blocked[:3]) + ". Setup will not change accounts or groups it did not make", blocked=True)
        todo = self._describe(missing)
        if todo:
            return Probe(False, f"missing: {todo}")
        if not self._visudo_ok(self.target):
            return Probe(False, "visudo -c rejects the session sudoers rule", blocked=True)
        return Probe(True, f"pe-s{self.p.uid_min}..pe-s{self.p.uid_max} in {SESSION_GROUP}; {self.target}")

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.ok:
            return {}
        if state.blocked:
            raise StepError(state.detail)
        p = self.p
        _blocked, missing = self._state()
        # Each account, group or membership is journaled as made BEFORE the command runs, so a crash in between can never
        # leave one that undo does not know about (undo skips what does not exist, and checks the shape of what does).
        # A retry after a failed attempt starts from what the earlier attempts made, so every evidence record (and so whatever
        # merge reads them back: the last record wins) still names everything setup made and undo removes all of it.
        made = self._seed(evidence_entries(journal, self.name))

        def record() -> None:
            journal.append("evidence", step=self.name, data={k: (list(v) if isinstance(v, list) else v) for k, v in made.items()})
        if missing["group"]:
            made["created_group"] = True
            record()
            self._run([p.groupadd, "--system", p.session_group])
            if self._state()[0]:
                raise StepError(f"groupadd made {p.session_group} with a gid the sessions cannot use; undo removes it")
        for n in missing["groups"]:
            if n not in made["created_groups"]:          # journaled before an earlier attempt's command failed
                made["created_groups"].append(n)
            record()
            self._run([p.groupadd, "--gid", str(n), account_name(n)])
        for n in missing["users"]:
            if n not in made["created_users"]:          # journaled before an earlier attempt's command failed
                made["created_users"].append(n)
            record()
            self._run([p.useradd, "--system", "--uid", str(n), "--gid", str(n), "--groups", p.session_group,
                       "--home-dir", SESSION_HOME, "--no-create-home", "--shell", SESSION_SHELL, account_name(n)])
        for n in missing["members"]:
            if n not in made["added_members"]:          # journaled before an earlier attempt's command failed
                made["added_members"].append(n)
            record()
            self._run([p.usermod, "--append", "--groups", p.session_group, account_name(n)])
        if missing["sudoers"] != "ok":
            wanted, previous = self.wanted(), read_regular(self.target)
            staging = os.path.join(os.path.dirname(self.target), f".{os.path.basename(self.target)}.check")
            write_atomic(staging, wanted, 0o440, p.owner_uid, p.owner_gid)
            try:
                if not self._visudo_ok(staging):
                    raise StepError("visudo -c rejects the session sudoers rule; it was not installed")
            finally:
                try:
                    os.unlink(staging)       # dot-prefixed, which sudo ignores, but never left behind
                except FileNotFoundError:
                    pass
            if made["sha256"] is None:       # an earlier attempt that wrote it already holds what was there before setup
                made["previous"] = _text(previous)
            made["sha256"] = sha256(wanted)
            record()
            write_atomic(self.target, wanted, 0o440, p.owner_uid, p.owner_gid)
        record()
        if not self.probe().ok:
            raise StepError("the session accounts and sudoers rule were set up but do not read back as intended")
        return dict(made)

    @staticmethod
    def _seed(entries: list[dict]) -> dict:
        """Everything earlier attempts of this step journaled: lists unioned in order, booleans OR'd, the sudoers sha256 (and the
        file it replaced) kept from whichever attempt wrote it."""
        made: dict = {"created_group": False, "created_groups": [], "created_users": [], "added_members": [],
                      "sha256": None, "previous": None, "created_dirs": []}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            made["created_group"] = made["created_group"] or bool(entry.get("created_group"))
            for key in ("created_groups", "created_users", "added_members", "created_dirs"):
                for item in entry.get(key) or []:
                    if item not in made[key]:
                        made[key].append(item)
            if entry.get("sha256") and made["sha256"] is None:
                made["sha256"], made["previous"] = entry["sha256"], entry.get("previous")
        return made

    def _still_ours(self, evidence: dict) -> list[str]:
        """What setup made that is no longer exactly what it made (undo then changes nothing)."""
        users, groups = _db(self.p.passwd), _db(self.p.group)
        user_by = {u[0]: u for u in users}
        group_by = {g[0]: g for g in groups}
        moved = []
        for n in evidence.get("created_users") or []:
            u = user_by.get(account_name(n))
            if u is not None and not (_pe_entry("user", u) and int(u[2]) == n):
                moved.append(f"user {account_name(n)}")
        for n in evidence.get("created_groups") or []:
            g = group_by.get(account_name(n))
            if g is not None and not (len(g) > 2 and g[2] == str(n)):
                moved.append(f"group {account_name(n)}")
        if evidence.get("created_group"):
            g = group_by.get(self.p.session_group)
            ours = {account_name(n) for n in [*(evidence.get("created_users") or []), *(evidence.get("added_members") or [])]}
            if g is not None and {m for m in (g[3] if len(g) > 3 else "").split(",") if m} - ours:
                moved.append(f"group {self.p.session_group} (it has members setup did not add)")
        return moved

    def undo(self, journal, evidence: dict) -> str:
        p = self.p
        moved = self._still_ours(evidence)
        wrote = evidence.get("sha256")
        current = read_regular(self.target)
        if wrote and current is not None and sha256(current) != wrote:
            moved.append(self.target)
        if moved:
            raise StepRefused(f"{', '.join(moved[:3])} changed after setup made it; nothing was reverted")
        done = []
        if wrote and current is not None:
            os.unlink(self.target)
            done.append(f"{self.target}: removed")
            if evidence.get("previous") is not None:
                write_atomic(self.target, evidence["previous"].encode(), 0o440, p.owner_uid, p.owner_gid)
                done.append("(the previous file was restored)")
        names_user = {u[0] for u in _db(p.passwd)}
        for n in evidence.get("added_members") or []:
            if account_name(n) in names_user:
                self._run([p.gpasswd, "--delete", account_name(n), p.session_group])
        removed_users = [n for n in evidence.get("created_users") or [] if account_name(n) in names_user]
        for n in removed_users:
            self._run([p.userdel, account_name(n)])
        names_group = {g[0] for g in _db(p.group)}     # read again: userdel may have taken a same-named primary group with it
        removed_groups = [n for n in evidence.get("created_groups") or [] if account_name(n) in names_group]
        for n in removed_groups:
            self._run([p.groupdel, account_name(n)])
        if evidence.get("created_group") and p.session_group in names_group:
            self._run([p.groupdel, p.session_group])
            done.append(f"group {p.session_group} removed")
        if removed_users or removed_groups:
            done.append(f"{len(removed_users)} account(s) and {len(removed_groups)} private group(s) removed")
        return "; ".join(done) or "nothing to undo"

    def removal(self) -> tuple[str | None, str]:
        path, why = super().removal()
        return path, why + ("" if path else "; the pe-s<N> accounts are left in place (no login, no home)")


# ── 4. the cgroup tree ─────────────────────────────────────────────────────────────────────────────

class CgroupTree(Step):
    name = "cgroup-tree"

    def wanted(self) -> tuple[str, bytes, int]:
        if self.p.flavour == "systemd":
            body = ("# Managed by Planet Express (session launcher). Core owns a delegated cgroup subtree: it makes\n"
                    "# `core` and `sessions` inside it, moves itself into `core`, and one child per session goes below\n"
                    "# `sessions` (contract 22.4).\n[Service]\nDelegate=yes\n")
            return self.p.unit_dropin, body.encode(), 0o644
        sessions = self.p.cgroup_root
        parent = os.path.dirname(sessions)
        # A configured path becomes text in a script root runs at every boot: it is checked against a strict character
        # set (see `_unsafe_for_shell`, which blocks the step) AND quoted, so it can never be anything but one word.
        q_sessions, q_parent = shlex.quote(sessions), shlex.quote(parent)
        q_control = shlex.quote(f"{parent}/cgroup.subtree_control")
        body = ("#!/bin/sh\n# Managed by Planet Express (session launcher): recreate the sessions cgroup at every boot\n"
                "# (cgroupfs lives in RAM on MOS). Core runs as root here, so root owns everything.\n"
                f"set -e\nmkdir -p {q_parent} {q_sessions}\n"
                "echo +pids > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true\n"
                f"echo +pids > {q_control} 2>/dev/null || true\n")
        if self.p.helper_store:
            body += self._helper_install()
        return self.p.boot_script, body.encode(), 0o700

    def _helper_install(self) -> str:
        """Put the helper back at its real path (MOS root is RAM). From the persistent copy only, only if every directory
        above it and the file are root-owned, not group/world writable and not a symlink, and only if its sha256 is the
        one recorded here when setup ran. Anything else is refused and logged, and the script exits non-zero."""
        want = sha256(LauncherHelper(self.p).wanted())
        store, live = shlex.quote(self.p.helper_store), shlex.quote(self.p.helper)
        live_dir = shlex.quote(os.path.dirname(self.p.helper))
        return (
            "rc=0\n"
            "trusted() {\n"
            "    t=$1\n"
            "    while [ -n \"$t\" ]; do\n"
            "        [ -L \"$t\" ] && return 1\n"
            "        s=$(stat -c '%u %a' \"$t\" 2>/dev/null) || return 1\n"
            "        [ \"${s% *}\" = 0 ] || return 1\n"
            "        [ $(( 0${s#* } & 022 )) -eq 0 ] || return 1\n"
            "        [ \"$t\" = / ] && break\n"
            "        t=$(dirname \"$t\")\n"
            "    done\n"
            "    return 0\n"
            "}\n"
            "digest() { sha256sum \"$1\" 2>/dev/null | cut -d' ' -f1; }\n"
            f"store={store}\nlive={live}\nwant={want}\n"
            "if [ -f \"$store\" ] && trusted \"$store\" && [ \"$(digest \"$store\")\" = \"$want\" ] \\\n"
            f"        && install -d -m 0755 -o root -g root {live_dir} \\\n"
            "        && install -m 0755 -o root -g root \"$store\" \"$live.new\" && mv -f \"$live.new\" \"$live\" \\\n"
            "        && trusted \"$live\" && [ \"$(digest \"$live\")\" = \"$want\" ]; then\n"
            "    echo \"launcher-boot: session helper installed at $live\" >&2\n"
            "else\n"
            "    rm -f \"$live.new\" \"$live\"\n"
            "    echo \"launcher-boot: refusing to install the session helper ($store is missing, not root-owned, "
            "writable, a symlink, or not the recorded version)\" >&2\n"
            "    rc=1\n"
            "fi\n"
            "exit $rc\n")

    def preflight(self) -> str | None:
        """Checked before ANY launcher step writes: sessions need a delegated cgroup2 subtree strictly below a cgroup2 mount."""
        if not self.p.check_delegation:
            return None
        from planet_express.missions.containment_cgroup import cgroup2_mountpoint
        try:
            mount = cgroup2_mountpoint(self.p.cgroup_root, Path(self.p.mountinfo).read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return f"cannot read the mount table to check missions.cgroup_root ({exc.strerror})"
        if mount is None or os.path.normpath(mount) == os.path.normpath(self.p.cgroup_root):
            return f"missions.cgroup_root {self.p.cgroup_root} is not a directory below a cgroup v2 mount"
        return None

    def _unsafe_for_shell(self) -> str | None:
        for path in (self.p.helper, self.p.helper_store):
            if self.p.flavour != "systemd" and path and not _SHELL_SAFE.fullmatch(path):
                return f"{path!r} has characters that are not allowed in the MOS boot script"
        if self.p.flavour != "systemd" and not _SHELL_SAFE.fullmatch(self.p.cgroup_root):
            return (f"missions.cgroup_root {self.p.cgroup_root!r} has characters that are not allowed in the MOS boot "
                    "script (letters, digits and . _ @ + / - only)")
        return None

    def _file_state(self) -> Probe:
        problem = self._unsafe_for_shell()
        if problem:
            return Probe(False, problem, blocked=True)
        path, data, mode = self.wanted()
        try:
            current = read_regular(path)
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        if current is None:
            return Probe(False, f"{path} is absent")
        if current != data:
            return Probe(False, f"{path} differs from the launcher's cgroup settings")
        info = os.lstat(path)
        if info.st_uid != self.p.owner_uid or stat.S_IMODE(info.st_mode) != mode:
            return Probe(False, f"{path} must be owned by root with mode {mode:04o}")
        return Probe(True, path)

    def active(self) -> Probe:
        """Writing the settings is not enough: systemd reads a drop-in at daemon-reload and delegates only when the
        service (re)starts; MOS recreates its tree when the boot script runs. Until Core's cgroup is really delegated,
        that is, present and owned by the Core user, the step is NOT satisfied."""
        if not self.p.check_delegation:
            return Probe(True)
        delegated = os.path.dirname(self.p.cgroup_root)
        how = ("restart the casa-planetexpress service (setup already ran daemon-reload)" if self.p.flavour == "systemd"
               else f"run {self.p.boot_script} (the boot hook does this at every boot)")
        try:
            info = os.lstat(delegated)
        except FileNotFoundError:
            return Probe(False, f"{delegated} does not exist yet: {how}")
        if info.st_uid != self.p.core_uid:
            return Probe(False, f"{delegated} is owned by uid {info.st_uid}, not the Core user: {how}")
        return Probe(True, delegated)

    def probe(self) -> Probe:
        state = self._file_state()
        return self.active() if state.ok else state

    def owned_file(self):
        if self._unsafe_for_shell():
            return None
        path, data, mode = self.wanted()
        return path, data, mode

    def _reload_pending(self) -> bool:
        """systemd knows the drop-in changed but has not reloaded: a write whose `daemon-reload` failed or never ran."""
        unit = os.path.basename(os.path.dirname(self.p.unit_dropin)).removesuffix(".d")
        try:
            result = self.p.run([self.p.systemctl, "show", "--property=NeedDaemonReload", "--value", unit],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == b"yes"

    def written(self) -> Probe:
        """The file, and on systemd that systemd has read it: a drop-in written but not reloaded is not done."""
        state = self._file_state()
        if state.ok and self.p.flavour == "systemd" and self.p.check_delegation and self._reload_pending():
            return Probe(False, f"{state.detail} is written but systemd has not reloaded it (daemon-reload is pending)")
        return state

    def _after_change(self) -> None:
        if self.p.flavour == "systemd":
            try:
                self._daemon_reload()
            except StepError:
                pass                       # the file is what matters; the next daemon-reload picks it up

    def repair(self, journal) -> dict:
        state = self._file_state()
        if state.blocked:
            raise StepError(state.detail)
        path, data, mode = self.wanted()
        previous = read_regular(path)
        created: list[str] = []
        if not state.ok:
            created = self._make_parents(journal, os.path.dirname(path))
            journal.append("evidence", step=self.name, data={
                "previous_sha256": None if previous is None else sha256(previous), "previous": _text(previous),
                "sha256": sha256(data), "created_dirs": created, "path": path})
            write_atomic(path, data, mode, self.p.owner_uid, self.p.owner_gid)
            if not self._file_state().ok:
                raise StepError("the cgroup settings were written but do not read back as intended")
        if self.p.flavour == "systemd":
            self._daemon_reload()
        return {"sha256": sha256(data), "created_dirs": created, "path": path}

    def _daemon_reload(self) -> None:
        try:
            result = self.p.run([self.p.systemctl, "daemon-reload"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=60, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepError(f"the drop-in was written but `systemctl daemon-reload` could not run ({type(exc).__name__})") from None
        if result.returncode != 0:
            raise StepError("the drop-in was written but `systemctl daemon-reload` failed")

    def undo(self, journal, evidence: dict) -> str:
        path, _, mode = self.wanted()
        detail = self._undo_file(path, evidence)
        previous = evidence.get("previous")
        if previous is not None and not os.path.exists(path):
            write_atomic(path, previous.encode(), mode, self.p.owner_uid, self.p.owner_gid)
            detail += " (the previous file was restored)"
        self._undo_dirs(evidence)
        self._after_change()
        return detail


# ── 5 and 6. directories ───────────────────────────────────────────────────────────────────────────

class _CoreDir(Step):
    attr = ""
    mode = 0o711
    why = ""

    @property
    def path(self) -> str:
        return getattr(self.p, self.attr)

    def owned_dir(self):
        return self.path, self.mode, self.p.core_uid, self.p.core_gid

    def probe(self) -> Probe:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return Probe(False, f"{self.path} is absent")
        if not stat.S_ISDIR(info.st_mode):
            return Probe(False, f"{self.path} is not a directory (a symlink or file is never replaced)", blocked=True)
        if info.st_uid != self.p.core_uid or stat.S_IMODE(info.st_mode) != self.mode:
            # It is there and it is not what setup makes: it may hold somebody's data, so setup never chowns or chmods it.
            return Probe(False, f"{self.path} exists, owned by uid {info.st_uid} with mode {stat.S_IMODE(info.st_mode):04o}, "
                                f"not by the Core user with mode {self.mode:04o}; setup does not change a directory it did "
                                "not make (fix it by hand or choose another path)", blocked=True)
        return Probe(True, self.path)

    def dir_identity(self) -> tuple[int, int]:
        return self.p.core_uid, self.mode

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        created: list[str] = []
        self._journal_intent(journal, self.path)
        ensure_dir(self.path, self.mode, self.p.core_uid, self.p.core_gid, created)
        journal.append("evidence", step=self.name, data={"created_dirs": created})
        if not self.probe().ok:
            raise StepError(f"{self.path} was created but does not read back as intended")
        return {"created_dirs": created}

    def undo(self, journal, evidence: dict) -> str:
        removed = self._undo_dirs(evidence)
        return f"removed {', '.join(removed)}" if removed else f"{self.path}: left in place (not created by setup, or not empty)"


class WorkspaceRoot(_CoreDir):
    name = "workspace-root"
    attr = "workspace_root"
    mode = 0o711          # sessions traverse to their own directory (made by Core, handed to them by the launcher)


class SocketRoot(_CoreDir):
    name = "socket-root"
    attr = "sock_root"
    mode = 0o711          # Core makes each session's directory below it; the launcher hands that one to the session


MAX_INCLUDE_DEPTH = 16
MAX_SUDOERS_FILES = 512


def _sudoers_logical_lines(text: str) -> list[str]:
    """The non-comment logical lines of a sudoers file (continuations joined). `#include` lines are kept; `#1001 ALL=...` is a
    rule for uid 1001, not a comment."""
    out, buffer = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        stripped = buffer.strip()
        if stripped and not (stripped.startswith("#") and not re.match(r"^#(\d|include)", stripped)):
            out.append(stripped)
        buffer = ""
    return out


def sudoers_files(main: str, directory: str) -> list[str]:
    """Every file sudo would read: the main file, `sudoers.d`, and anything they `@include` / `@includedir`, recursively (a nested
    include is read, a cycle is cut, a target that cannot be resolved or read raises StepError)."""
    files: list[str] = []
    queue: list[tuple[str, int]] = [(main, 0)]
    dirs: list[tuple[str, int, bool]] = [(directory, 0, False)]       # (dir, depth, named by an include?)
    seen_dirs: set[str] = set()
    while queue or dirs:
        if len(files) > MAX_SUDOERS_FILES:
            raise StepError("sudoers includes too many files to check")
        if dirs:
            here, depth, explicit = dirs.pop(0)
            if here in seen_dirs:
                continue
            if not os.path.isdir(here):
                if explicit:
                    raise StepError(f"@includedir {here[:80]} is not a readable directory")
                continue
            seen_dirs.add(here)
            for name in sorted(os.listdir(here)):
                if "." not in name and not name.endswith("~"):          # sudo itself ignores the others
                    queue.append((os.path.join(here, name), depth))
            continue
        path, depth = queue.pop(0)
        if path in files:
            continue
        if depth > MAX_INCLUDE_DEPTH:
            raise StepError(f"sudoers includes nest deeper than {MAX_INCLUDE_DEPTH}: {path}")
        data = read_regular(path)
        if data is None:
            raise StepError(f"{path} is absent")
        files.append(path)
        for line in _sudoers_logical_lines(data.decode("utf-8", errors="replace")):
            match = re.match(r"^[@#]include(dir)?\s+(.+)$", line)
            if not match:
                continue
            target = match.group(2).strip().strip('"')
            if "%" in target:
                raise StepError(f"{path}: an include with a substitution ({target[:60]}) cannot be checked")
            if not target.startswith("/"):
                target = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if match.group(1):
                dirs.append((target, depth + 1, True))
            else:
                queue.append((target, depth + 1))
    return files


class SudoersReadable(Step):
    """RETIRED in trust mode (rc14): it let a non-root Core read every sudoers file for the Repair side-door gate, and that gate is
    gone. New plans no longer include it (`launcher_steps` leaves it out); it stays so a host that applied it can still undo it.

    A read-only POSIX ACL entry for the Core user on `/etc/sudoers` and each file in `/etc/sudoers.d`, traverse on the directory,
    and a default entry so a file added later inherits it. Mode bits stay 0440; `visudo -c` is run after every change."""
    name = "sudoers-readable"
    ubuntu_only = True
    retired = True

    def _targets(self) -> list[tuple[str, str]]:
        """Every file sudo reads (the include walk: `@include`/`@includedir`, recursively) and the directories that hold them.
        Run as root, so the walk itself can read what it follows."""
        p = self.p
        files = sudoers_files(p.sudoers_main, p.sudoers_dir)
        dirs = dict.fromkeys([p.sudoers_dir] if os.path.isdir(p.sudoers_dir) else [])
        for path in files:
            dirs[os.path.dirname(path)] = None
        return [*((d, "rx") for d in dirs), *((f, "r") for f in dict.fromkeys([p.sudoers_main, *files]))]

    def _untraversable(self, targets: list[tuple[str, str]]) -> list[str]:
        """Ancestors ABOVE the sudoers directories that Core cannot pass through. Setup never widens those (they are not
        sudo's): the operator moves the include or opens the directory, then runs this step again."""
        own = {path for path, need in targets if need == "rx"}
        blocked: dict[str, None] = {}
        for path, _need in targets:
            current, top = os.path.dirname(path), self.p.below.rstrip("/")
            while current not in ("/", "", top):
                if current not in own and not self._can(current, "x"):
                    blocked[current] = None
                current = os.path.dirname(current)
        return list(blocked)

    def _acl(self, path: str, default: bool = False) -> dict[str, str]:
        argv = [self.p.getfacl, "-cpn", *(["-d"] if default else []), "--", path]
        result = self.p.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20, env=_RUN_ENV, check=False)
        if result.returncode != 0:
            raise StepError(f"getfacl {path} failed: {(result.stderr or b'').decode(errors='replace').strip()[:160]}")
        entries = {}
        for line in (result.stdout or b"").decode(errors="replace").splitlines():
            kind, _, rest = line.removeprefix("default:").partition(":")
            qualifier, _, perms = rest.partition(":")
            entries[f"{kind}:{qualifier}"] = perms.split()[0] if perms else ""
        return entries

    def _can(self, path: str, need: str) -> bool:
        info = os.lstat(path)
        bits = {"r": stat.S_IROTH, "x": stat.S_IXOTH}
        if info.st_uid == self.p.core_uid:
            return all(info.st_mode & (bits[c] << 6) for c in need)
        if all(info.st_mode & bits[c] for c in need):
            return True
        entries = self._acl(path)
        mine, mask = entries.get(f"user:{self.p.core_uid}", ""), entries.get("mask:", "rwx")
        return all(c in mine and c in mask for c in need)

    def probe(self) -> Probe:
        if self.p.core_uid == 0:
            return Probe(True, "Core runs as root")
        try:
            targets = self._targets()
            stuck = self._untraversable(targets)
            if stuck:
                return Probe(False, f"Core cannot pass through {', '.join(stuck[:3])} to reach an included sudoers file; setup "
                                    "does not change directories that are not sudo's (move the include, or open the directory)",
                             blocked=True)
            missing = [path for path, need in targets if not self._can(path, need)]
            foreign = [path for path in missing if self._foreign(self._acl(path))]
            if os.path.isdir(self.p.sudoers_dir) and self._foreign(self._acl(self.p.sudoers_dir, default=True)):
                foreign.append(f"{self.p.sudoers_dir} (default ACL)")
            if foreign:
                return Probe(False, f"{', '.join(foreign[:3])} already carr{'ies' if len(foreign) == 1 else 'y'} ACL entries setup did "
                                    "not make; setup will not change them (adding Core would recalculate their mask). Give Core "
                                    "read access by hand, or remove those entries, then run this step again", blocked=True)
            default = self._default_ok()
        except (OSError, StepError) as exc:
            return Probe(False, f"cannot inspect the sudoers files: {exc}", blocked=True)
        if missing:
            return Probe(False, f"Core cannot read {', '.join(missing[:4])}{' ...' if len(missing) > 4 else ''}")
        if not default:
            return Probe(False, f"a file added to {self.p.sudoers_dir} later would not be readable by Core")
        return Probe(True, "Core can read every sudoers file (read-only ACL)")

    def _foreign(self, entries: Mapping[str, str]) -> list[str]:
        """ACL entries setup did not make: a named user other than Core, a named group, or a mask without Core's entry.
        Adding Core's entry recalculates the mask, which could widen what those others get, so setup leaves such a file alone."""
        mine = f"user:{self.p.core_uid}"
        out = [k for k in entries if (k.startswith("user:") and k not in ("user:", mine)) or (k.startswith("group:") and k != "group:")]
        if "mask:" in entries and mine not in entries:
            out.append("mask:")
        return out

    def _default_ok(self) -> bool:
        """A file added to sudoers.d later inherits an entry Core can read through (the default mask counts too)."""
        if not os.path.isdir(self.p.sudoers_dir):
            return True
        entries = self._acl(self.p.sudoers_dir, default=True)
        return "r" in entries.get(f"user:{self.p.core_uid}", "") and "r" in entries.get("mask:", "r")

    def _setfacl(self, *argv: str) -> None:
        result = self.p.run([self.p.setfacl, *argv], stdin=subprocess.DEVNULL, capture_output=True, timeout=20, env=_RUN_ENV,
                            check=False)
        if result.returncode != 0:
            raise StepError(f"setfacl {' '.join(argv)[:160]} failed: {(result.stderr or b'').decode(errors='replace').strip()[:160]}")

    def _sudo_still_valid(self) -> bool:
        result = self.p.run([self.p.visudo, "-c"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=_RUN_ENV,
                            check=False)
        return result.returncode == 0

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.ok:
            return {}
        if state.blocked:
            raise StepError(state.detail)
        who = str(self.p.core_uid)
        # path -> Core's entry before this step ("" when it had none), so undo puts back exactly what was there.
        prior: dict[str, str] = {}
        evidence = {"core_uid": self.p.core_uid, "files": list(prior), "prior": prior, "default_dir": None, "default_prior": None}
        try:
            for path, need in self._targets():
                if not self._can(path, need):
                    had = self._acl(path).get(f"user:{who}", "")
                    keep = "".join(c for c in "rwx" if c in had or c in need)
                    journal.append("evidence", step=self.name, data=dict(evidence, intent=path, intent_prior=had))
                    self._setfacl("-m", f"u:{who}:{keep}", "--", path)
                    prior[path] = had
                    evidence["files"] = list(prior)
            if not self._default_ok():
                current = self._acl(self.p.sudoers_dir, default=True)
                had, mask = current.get(f"user:{who}", ""), current.get("mask:")
                journal.append("evidence", step=self.name, data=dict(evidence, intent="default", intent_prior=had))
                self._setfacl("-d", "-m", f"u:{who}:r", "--", self.p.sudoers_dir)     # recalculates the default mask
                evidence["default_dir"], evidence["default_prior"] = self.p.sudoers_dir, {"user": had, "mask": mask}
            journal.append("evidence", step=self.name, data=dict(evidence))
            if not self._sudo_still_valid():
                raise StepError("visudo -c fails after the read ACLs were added")
            if not self.probe().ok:
                raise StepError("the read ACLs were set but Core still cannot read every sudoers file")
        except (StepError, OSError):
            self._remove(evidence)                     # nothing half-done is left behind
            raise
        return dict(evidence)

    def _remove(self, evidence: dict) -> list[str]:
        who, removed = str(evidence.get("core_uid", self.p.core_uid)), []
        prior = evidence.get("prior") or {path: "" for path in evidence.get("files") or []}
        for path, had in prior.items():
            if os.path.lexists(path):
                if had:
                    self._setfacl("-m", f"u:{who}:{had}", "--", path)                # what Core had before, exactly
                else:
                    self._setfacl("-x", f"u:{who}", "--", path)
                removed.append(path)
        directory = evidence.get("default_dir")
        if directory and os.path.isdir(directory):
            before = evidence.get("default_prior") or {}
            if before.get("user"):
                self._setfacl("-d", "-m", f"u:{who}:{before['user']}", "--", directory)
            else:
                self._setfacl("-d", "-x", f"u:{who}", "--", directory)
            if before.get("mask") is not None:
                self._setfacl("-d", "-m", f"m::{before['mask']}", "--", directory)
            removed.append(f"default ACL on {directory}")
        return removed

    def undo(self, journal, evidence: dict) -> str:
        removed = self._remove(evidence)
        return f"removed Core's read ACL from {', '.join(removed)}" if removed else "nothing to remove"


def launcher_steps(paths: Paths, *, include_retired: bool = False) -> list[Step]:
    """The steps in dependency order, minus those that do not apply on this host's flavour. A retired step (`sudoers-readable`) is
    left out of new plans; `include_retired` finds it again, so a step an older plan applied can still be checked and undone."""
    steps = [SessionUidRange(paths), LauncherHelper(paths), HelperStore(paths), LauncherSudoers(paths), SessionAccess(paths),
             SudoersReadable(paths), CgroupTree(paths), WorkspaceRoot(paths), SocketRoot(paths)]
    return [s for s in steps if s.applies() and (include_retired or not getattr(s, "retired", False))]


# ── driving them ───────────────────────────────────────────────────────────────────────────────────

class MemoryJournal:
    """A journal with the planet_express.setup.journal.Journal surface that keeps events in a list."""

    def __init__(self):
        self.events: list[dict] = []

    def append(self, type_: str, **data) -> dict:
        event = {"seq": len(self.events) + 1, "type": type_, **data}
        self.events.append(event)
        return event

    def evidence(self, step: str) -> dict:
        merged: dict = {}
        for event in self.events:
            if event["type"] == "evidence" and event.get("step") == step:
                merged.update(event["data"])
        return merged


def evidence_entries(journal, step: str) -> list[dict]:
    """Every evidence record a step journaled, oldest first (`MemoryJournal`, the plan's journal adapter, or the setup `Journal`).
    A journal that cannot say gives none: the step then starts from nothing, as before."""
    if hasattr(journal, "evidence_entries"):
        return list(journal.evidence_entries(step))
    if isinstance(journal, MemoryJournal):
        return [e["data"] for e in journal.events if e["type"] == "evidence" and e.get("step") == step]
    if hasattr(journal, "steps"):
        record = journal.steps().get(step)
        return list(record.evidence) if record else []
    return []


def evidence_for(journal, step: str) -> dict:
    """Everything a step journaled as evidence, merged (works with `MemoryJournal` and the setup `Journal`)."""
    if hasattr(journal, "evidence"):
        return journal.evidence(step)
    record = journal.steps().get(step)
    merged: dict = {}
    for item in (record.evidence if record else []):
        merged.update(item)
    return merged


def probe_all(steps: list[Step]) -> dict[str, Probe]:
    return {s.name: s.probe() for s in steps}


def repair_all(steps: list[Step], journal) -> dict[str, str]:
    """Probe each step and repair what is not satisfied, in order, stopping at the first one that cannot be."""
    done: dict[str, str] = {}
    for step in steps:
        state = step.probe()
        if state.ok:
            done[step.name] = "already in place"
            continue
        journal.append("step_started", step=step.name)
        try:
            step.repair(journal)
        except StepError as exc:
            journal.append("step_failed", step=step.name, reason=str(exc))
            done[step.name] = f"FAILED: {exc}"
            break
        journal.append("step_ok", step=step.name)
        after = step.probe()
        # A step can be written correctly and still be inactive (a drop-in needs a restart): say so, do not claim done.
        done[step.name] = "repaired" if after.ok else f"repaired, activation pending: {after.detail}"
    return done


def undo_all(steps: list[Step], journal) -> dict[str, str]:
    """Newest first. A step whose evidence no longer matches the host stops the undo (nothing is reverted over
    changes that are not setup's)."""
    out: dict[str, str] = {}
    for step in reversed(steps):
        evidence = evidence_for(journal, step.name)
        if not evidence:
            out[step.name] = "nothing to undo"
            continue
        try:
            detail = step.undo(journal, evidence)
        except StepRefused as exc:
            journal.append("undo_refused", step=step.name, reason=str(exc))
            out[step.name] = f"REFUSED: {exc}"
            break
        journal.append("step_undone", step=step.name, detail=detail)
        out[step.name] = detail
    return out


def cli_paths(args, core_uid: int, core_gid: int) -> Paths:
    """The CLI's Paths. On MOS the conf, helper store and boot script come from PE_HOME through the one function the
    setup plan uses; Ubuntu keeps the fixed defaults."""
    extra = mos_launcher_paths(args.pe_home) if args.flavour == "mos" else {}
    return Paths(core_uid=core_uid, core_gid=core_gid, core_user=args.core_user, flavour=args.flavour,
                 workspace_root=args.workspace_root, cgroup_root=args.cgroup_root, sock_root=args.sock_root,
                 uid_min=args.uid_min, uid_max=args.uid_max,
                 log_groups=tuple(getattr(args, "session_group", None) or _DEFAULTS.session_groups), **extra)


def main(argv: list[str] | None = None) -> int:               # pragma: no cover -- a thin CLI over the above
    import argparse
    import pwd

    from planet_express.setup.journal import Journal
    parser = argparse.ArgumentParser(description="Probe, repair or undo the session launcher's host setup (root).")
    parser.add_argument("action", choices=("probe", "repair", "undo"))
    parser.add_argument("--core-user", required=True)
    parser.add_argument("--flavour", choices=("systemd", "mos"), default="systemd")
    parser.add_argument("--pe-home", default="/mnt/data/pe", help="MOS only: PE_HOME, where launch.conf, the helper copy and "
                        "the boot script live (the setup plan derives them the same way)")
    parser.add_argument("--journal-dir", default="/var/lib/planetexpress/setup-journal")
    parser.add_argument("--workspace-root", required=True, help="missions.workspace_root (or <state dir>/mission-workspaces)")
    parser.add_argument("--cgroup-root", default=_DEFAULTS.cgroup_root, help="missions.cgroup_root")
    parser.add_argument("--sock-root", default=_DEFAULTS.sock_root, help="missions.sock_root")
    parser.add_argument("--uid-min", type=int, default=_DEFAULTS.session_uid_min)
    parser.add_argument("--uid-max", type=int, default=_DEFAULTS.session_uid_max)
    parser.add_argument("--session-group", "--log-group", dest="session_group", action="append",
                        help="missions.session_groups: a supplementary group every session gets (repeat; default "
                        f"{', '.join(_DEFAULTS.session_groups)}). Must match config.yaml")
    args = parser.parse_args(argv)
    account = pwd.getpwnam(args.core_user)
    paths = cli_paths(args, account.pw_uid, account.pw_gid)
    steps = launcher_steps(paths, include_retired=args.action == "undo")
    if args.action == "probe":
        for name, state in probe_all(steps).items():
            print(f"{'ok  ' if state.ok else 'TODO'} {name}: {state.detail}")
        return 0
    journal = Journal(args.journal_dir, "launcher")
    result = repair_all(steps, journal) if args.action == "repair" else undo_all(steps, journal)
    for name, detail in result.items():
        print(f"{name}: {detail}")
    return 0 if not any(d.startswith(("FAILED", "REFUSED")) for d in result.values()) else 1


if __name__ == "__main__":             # pragma: no cover
    raise SystemExit(main())
