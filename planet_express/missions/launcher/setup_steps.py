"""Setup steps for the session launcher (contract 22.5): each named, each with a probe, a repair and an undo.

    session-uid-range   launch.conf: the reserved uid range (and Core's identity), checked free of accounts
    launcher-helper     the root-owned pe-launch under a root-owned, non-symlink chain
    launcher-sudoers    Ubuntu only: `Core ALL=(root) NOPASSWD: pe-launch ""`, checked with visudo
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

Nothing here executes a shell. `visudo -c` is the one external program, run as argv with a minimal environment.
"""

import hashlib
import os
import re
import shlex
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from config_schema import MissionsConfig
from planet_express.missions.launcher import pe_launch
from planet_express.missions.launcher.config import (
    CONF_PATH,
    DEFAULT_PIDS_MAX,
    HELPER_PATH,
    SUDOERS_PATH,
    ConfError,
    LaunchConf,
)

SOURCE = Path(pe_launch.__file__)
_SHELL_SAFE = re.compile(r"/[A-Za-z0-9._@+/-]*")
_UNIX_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
UNIT_DROPIN = "/etc/systemd/system/casa-planetexpress.service.d/launcher.conf"


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
    cgroup_root: str = _DEFAULTS.cgroup_root
    sock_root: str = _DEFAULTS.sock_root
    helper: str = HELPER_PATH
    conf: str = CONF_PATH
    sudoers: str = SUDOERS_PATH
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
    mountinfo: str = "/proc/self/mountinfo"
    check_delegation: bool = True                 # require Core's cgroup to exist and be Core's (tests may turn it off)
    run: Callable = field(default=subprocess.run, repr=False)

    @classmethod
    def from_missions(cls, missions, *, state_dir: str, **kw) -> "Paths":
        """Build from the `missions` section of Core's config so the two sides share one set of values."""
        return cls(uid_min=missions.session_uid_min, uid_max=missions.session_uid_max, cgroup_root=missions.cgroup_root,
                   sock_root=missions.sock_root,
                   workspace_root=missions.workspace_root or os.path.join(state_dir, "mission-workspaces"), **kw)

    def launch_conf(self) -> LaunchConf:
        return LaunchConf(uid_min=self.uid_min, uid_max=self.uid_max, core_uid=self.core_uid, core_gid=self.core_gid,
                          cgroup_root=self.cgroup_root, workspace_root=self.workspace_root, sock_root=self.sock_root,
                          pids_max=self.pids_max)


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


def trusted_chain(path: str, paths: Paths) -> str | None:
    """None if every ancestor and the file are root-owned, unwritable by others, and no symlink; else why not."""
    try:
        pe_launch.check_trusted_path(path, trusted_uid=paths.owner_uid, below=paths.below)
    except pe_launch.Refused as exc:
        return f"{path}: {exc.code}"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    return None


def trusted_dir_chain(path: str, paths: Paths) -> str | None:
    try:
        pe_launch.check_trusted_path(path, trusted_uid=paths.owner_uid, file_ok=False, below=paths.below)
    except pe_launch.Refused as exc:
        return f"{path}: {exc.code}"
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
        """Accounts and groups that already use a number in the range: a passwd entry by uid OR by primary gid, a group
        by gid (the helper and the preflight refuse each of these, so setup must not report success over one)."""
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
                if hit:
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
            return self.p.helper, self.wanted(), 0o755
        except StepError:
            return None

    def probe(self) -> Probe:
        try:
            current = read_regular(self.p.helper)
            wanted = self.wanted()
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        problem = trusted_dir_chain(os.path.dirname(self.p.helper), self.p) if os.path.isdir(os.path.dirname(self.p.helper)) else None
        if problem:
            return Probe(False, problem, blocked=True)
        if current is None:
            return Probe(False, f"{self.p.helper} is absent")
        if current != wanted:
            return Probe(False, f"{self.p.helper} differs from this version's helper")
        info = os.lstat(self.p.helper)
        if info.st_uid != self.p.owner_uid or stat.S_IMODE(info.st_mode) != 0o755:
            return Probe(False, f"{self.p.helper} must be owned by root with mode 0755")
        problem = trusted_chain(self.p.helper, self.p)
        return Probe(problem is None, problem or self.p.helper, blocked=problem is not None)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        wanted, previous = self.wanted(), read_regular(self.p.helper)
        created = self._make_parents(journal, os.path.dirname(self.p.helper))
        problem = trusted_dir_chain(os.path.dirname(self.p.helper), self.p)
        if problem:
            raise StepError(f"refusing to install the helper: {problem}; fix the directory chain first")
        journal.append("evidence", step=self.name, data={"previous_sha256": None if previous is None else sha256(previous),
                                                         "previous": _text(previous),
                                                         "sha256": sha256(wanted), "created_dirs": created})
        write_atomic(self.p.helper, wanted, 0o755, self.p.owner_uid, self.p.owner_gid)
        if not self.probe().ok:
            raise StepError("the helper was installed but does not pass its own checks")
        return {"sha256": sha256(wanted), "created_dirs": created,
                "previous": None if previous is None else previous.decode("utf-8", errors="replace")}

    def undo(self, journal, evidence: dict) -> str:
        detail = self._undo_file(self.p.helper, evidence)
        previous = evidence.get("previous")
        if previous is not None and not os.path.exists(self.p.helper):
            write_atomic(self.p.helper, previous.encode(), 0o755, self.p.owner_uid, self.p.owner_gid)
            detail += " (the previous file was restored)"
        self._undo_dirs(evidence)
        return detail


# ── 3. sudoers (Ubuntu only) ───────────────────────────────────────────────────────────────────────

class LauncherSudoers(Step):
    name = "launcher-sudoers"
    ubuntu_only = True            # MOS has no sudo; and this step is never skipped because a legacy sudoers file exists

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
        return None if self.unsafe() else (self.p.sudoers, self.wanted(), 0o440)

    def probe(self) -> Probe:
        problem = self.unsafe()
        if problem:
            return Probe(False, problem, blocked=True)
        try:
            current = read_regular(self.p.sudoers)
        except StepError as exc:
            return Probe(False, str(exc), blocked=True)
        if current is None:
            return Probe(False, f"{self.p.sudoers} is absent")
        if current != self.wanted():
            return Probe(False, f"{self.p.sudoers} differs from the launcher rule")
        info = os.lstat(self.p.sudoers)
        if info.st_uid != self.p.owner_uid or stat.S_IMODE(info.st_mode) != 0o440:
            return Probe(False, f"{self.p.sudoers} must be owned by root with mode 0440")
        if not self._visudo_ok(self.p.sudoers):
            return Probe(False, "visudo -c rejects the sudoers fragment", blocked=True)
        return Probe(True, self.p.sudoers)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.blocked:
            raise StepError(state.detail)
        wanted, previous = self.wanted(), read_regular(self.p.sudoers)
        staging = os.path.join(os.path.dirname(self.p.sudoers), f".{os.path.basename(self.p.sudoers)}.check")
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
        write_atomic(self.p.sudoers, wanted, 0o440, self.p.owner_uid, self.p.owner_gid)
        if not self.probe().ok:
            raise StepError("the sudoers fragment was installed but does not read back as intended")
        return {"sha256": sha256(wanted), "created_dirs": [],
                "previous": None if previous is None else previous.decode("utf-8", errors="replace")}

    def undo(self, journal, evidence: dict) -> str:
        detail = self._undo_file(self.p.sudoers, evidence)
        previous = evidence.get("previous")
        if previous is not None and not os.path.exists(self.p.sudoers):
            write_atomic(self.p.sudoers, previous.encode(), 0o440, self.p.owner_uid, self.p.owner_gid)
        return detail


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
        return self.p.boot_script, body.encode(), 0o700

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


def launcher_steps(paths: Paths) -> list[Step]:
    """The steps in dependency order, minus those that do not apply on this host's flavour."""
    steps = [SessionUidRange(paths), LauncherHelper(paths), LauncherSudoers(paths), CgroupTree(paths),
             WorkspaceRoot(paths), SocketRoot(paths)]
    return [s for s in steps if s.applies()]


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


def main(argv: list[str] | None = None) -> int:               # pragma: no cover -- a thin CLI over the above
    import argparse
    import pwd

    from planet_express.setup.journal import Journal
    parser = argparse.ArgumentParser(description="Probe, repair or undo the session launcher's host setup (root).")
    parser.add_argument("action", choices=("probe", "repair", "undo"))
    parser.add_argument("--core-user", required=True)
    parser.add_argument("--flavour", choices=("systemd", "mos"), default="systemd")
    parser.add_argument("--journal-dir", default="/var/lib/planetexpress/setup-journal")
    parser.add_argument("--workspace-root", required=True, help="missions.workspace_root (or <state dir>/mission-workspaces)")
    parser.add_argument("--cgroup-root", default=_DEFAULTS.cgroup_root, help="missions.cgroup_root")
    parser.add_argument("--sock-root", default=_DEFAULTS.sock_root, help="missions.sock_root")
    parser.add_argument("--uid-min", type=int, default=_DEFAULTS.session_uid_min)
    parser.add_argument("--uid-max", type=int, default=_DEFAULTS.session_uid_max)
    args = parser.parse_args(argv)
    account = pwd.getpwnam(args.core_user)
    paths = Paths(core_uid=account.pw_uid, core_gid=account.pw_gid, core_user=args.core_user, flavour=args.flavour,
                  workspace_root=args.workspace_root, cgroup_root=args.cgroup_root, sock_root=args.sock_root,
                  uid_min=args.uid_min, uid_max=args.uid_max)
    steps = launcher_steps(paths)
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
