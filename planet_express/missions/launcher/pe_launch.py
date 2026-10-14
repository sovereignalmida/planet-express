#!/usr/bin/python3 -IS
"""pe-launch: the one root-privileged step that starts a mission session (contract section 22, slice 3d).

READ THIS FILE ALONE. It is single-threaded, stdlib-only and imports nothing from planet_express, so it can
be audited without the rest of the project. It is installed as a root-owned file under a root-owned,
non-symlink directory chain and run either by `sudo -n` (Ubuntu, unprivileged Core) or directly (MOS, Core is
root). It takes NO arguments (the sudoers rule forbids any) and reads ONE JSON spec on stdin:

    {session_id, worker_uid, cwd, argv, env, token, sock_dir?}

Everything that decides authority comes from the root-owned launch.conf, never from the spec: the uid range,
the gid (always equal to the uid), the cgroup root, the workspace root, the socket root, the Core identity.
Every check fails closed with a fixed error code; the spec holds a token, so nothing from it is ever echoed.

What it does, in order (any failure before the cgroup join removes what it made):
  1. interpret nothing from the environment: clear it, rebuild a minimal one, ignore PYTHON* (the shebang is
     `-I -S`), refuse to run as non-root, refuse an argv, check its own path and the config's ancestors;
  2. validate the spec (shape, bounds) and then against the config (uid range, not Core, not root, cwd under the
     workspace root and not a symlink, owned by Core or by that uid, argv[0] an absolute executable file);
  3. create `<cgroup_root>/<session_id>` (must not exist) on a cgroup2 mount, give Core the control files
     (cgroup.procs, cgroup.freeze, cgroup.kill), set pids.max; hand the workspace to the session uid;
     optionally take over the per-session socket directory Core already made and filled (chown to the session);
  4. token pipe -> fd 3; fd 0 /dev/null; fds 1 and 2 stay the merged output pipe Core holds; nothing else;
  5. join the cgroup, setgroups([]), setgid, setuid, verify root cannot be regained, no_new_privs, setsid,
     reset rlimits and signals, fchdir to the already-verified cwd;
  6. write ONE framed status line (`PE-LAUNCH-STATUS {json}`) as the first line of the merged output, then execve
     exactly argv with exactly the spec env. The exec'd pid is not tracked; the cgroup is.
"""

import ctypes
import grp
import json
import os
import pwd
import re
import resource
import signal
import stat
import sys

CONF_PATH = "/etc/planetexpress/launch.conf"
STATUS_PREFIX = "PE-LAUNCH-STATUS "
CGROUP2_SUPER_MAGIC = 0x63677270
TIMEOUT_S = 10          # shorter than Core's wait (strategies.LAUNCH_TIMEOUT_S): a helper Core gave up on is already dead
MAX_SPEC_BYTES = 60_000
MAX_WORKSPACE_ENTRIES = 64
_PR_SET_NO_NEW_PRIVS = 38

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_ARGV_ITEMS, MAX_ARG_CHARS, MAX_ARGV_CHARS = 64, 4096, 32_768
MAX_ENV_ITEMS, MAX_ENV_VALUE_CHARS, MAX_ENV_CHARS = 64, 4096, 32_768
MAX_CWD_CHARS = 1024
TOKEN_CHARS = (16, 256)
SPEC_KEYS = frozenset({"session_id", "worker_uid", "cwd", "argv", "env", "token", "sock_dir"})
SPEC_REQUIRED = SPEC_KEYS - {"sock_dir"}
CONF_KEYS = frozenset({"version", "uid_min", "uid_max", "core_uid", "core_gid", "cgroup_root",
                       "workspace_root", "sock_root", "pids_max"})


class Refused(Exception):
    """A check failed. `code` is a fixed phrase; it never carries spec content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ── shape checks (a deliberate copy of planet_express.missions.launcher.spec.check_wire; a test pins parity) ──

def _clean(text):
    return isinstance(text, str) and "\0" not in text


def check_wire(obj):
    if not isinstance(obj, dict) or not SPEC_REQUIRED <= set(obj) <= SPEC_KEYS:
        raise Refused("spec keys")
    sid, uid, cwd, argv, env, token = (obj[k] for k in ("session_id", "worker_uid", "cwd", "argv", "env", "token"))
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise Refused("session_id")
    if type(uid) is not int or uid <= 0:
        raise Refused("worker_uid")
    if not _clean(cwd) or not cwd.startswith("/") or len(cwd) > MAX_CWD_CHARS:
        raise Refused("cwd")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV_ITEMS
            or not all(_clean(a) and len(a) <= MAX_ARG_CHARS for a in argv)
            or sum(len(a) for a in argv) > MAX_ARGV_CHARS):
        raise Refused("argv")
    if not argv[0].startswith("/"):
        raise Refused("argv[0] must be absolute")
    if not isinstance(env, dict) or len(env) > MAX_ENV_ITEMS:
        raise Refused("env")
    total = 0
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or name.startswith("LD_"):
            raise Refused("env name")
        if not _clean(value) or len(value) > MAX_ENV_VALUE_CHARS:
            raise Refused("env value")
        total += len(name) + len(value)
    if total > MAX_ENV_CHARS:
        raise Refused("env size")
    if (not isinstance(token, str) or not TOKEN_CHARS[0] <= len(token) <= TOKEN_CHARS[1]
            or not all(0x21 <= ord(c) <= 0x7E for c in token)):
        raise Refused("token")
    if "sock_dir" in obj and not isinstance(obj["sock_dir"], bool):
        raise Refused("sock_dir")
    return obj


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise Refused("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise Refused("json constant")


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except Refused:
        raise
    except (ValueError, RecursionError):
        raise Refused("json") from None


# ── trusted paths ─────────────────────────────────────────────────────────────────────────────────

def _norm_abs(path):
    return (isinstance(path, str) and "\0" not in path and path.startswith("/") and path == os.path.normpath(path)
            and len(path) <= MAX_CWD_CHARS)


def check_trusted_path(path, *, trusted_uid=0, file_ok=True, below="/"):
    """Every ancestor of `path` is a real directory (never a symlink) owned by `trusted_uid` and not group or
    world writable; the final component (if `file_ok`) is a regular file under the same rules. `below` exists only
    so a test can anchor the walk inside a throwaway tree (/tmp is sticky and world-writable); main() never sets it."""
    if not _norm_abs(path):
        raise Refused("path")
    parts = [p for p in path.split("/") if p]
    prefix = ""
    for index in range(len(parts) + 1):
        current = prefix or "/"
        info = os.lstat(current)
        last = index == len(parts)
        if below != "/" and (below == current or below.startswith(current.rstrip("/") + "/")):
            if not last:
                prefix = prefix + "/" + parts[index]
            continue
        if stat.S_ISLNK(info.st_mode):
            raise Refused("symlink in a trusted path")
        if last and file_ok:
            if not stat.S_ISREG(info.st_mode):
                raise Refused("not a regular file")
        elif not stat.S_ISDIR(info.st_mode):
            raise Refused("not a directory")
        if info.st_uid != trusted_uid or info.st_mode & 0o022:
            raise Refused("untrusted ownership or mode")
        if not last:
            prefix = prefix + "/" + parts[index]


def load_conf(path=CONF_PATH, *, trusted_uid=0, below="/"):
    try:
        check_trusted_path(path, trusted_uid=trusted_uid, below=below)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused("conf") from None
    try:
        info = os.fstat(fd)
        if info.st_uid != trusted_uid or info.st_mode & 0o077 or info.st_size > 16_384:
            raise Refused("conf mode")
        raw = os.read(fd, 16_385)
    finally:
        os.close(fd)
    conf = parse_json(raw)
    if not isinstance(conf, dict) or set(conf) != CONF_KEYS or conf["version"] != 1:
        raise Refused("conf keys")
    ints = ("uid_min", "uid_max", "core_uid", "core_gid", "pids_max")
    if not all(type(conf[k]) is int and conf[k] >= 0 for k in ints):
        raise Refused("conf ints")
    if not 0 < conf["uid_min"] <= conf["uid_max"] < 2**31 or conf["uid_min"] <= conf["core_uid"] <= conf["uid_max"]:
        raise Refused("conf range")
    paths = [conf[k] for k in ("cgroup_root", "workspace_root", "sock_root")]
    if not all(_norm_abs(p) and p != "/" for p in paths) or len(set(paths)) != 3:
        raise Refused("conf paths")
    return conf


# ── opening without following links ───────────────────────────────────────────────────────────────

def open_dir_nofollow(path):
    """An O_RDONLY fd for the directory at `path`, refusing a symlink at ANY component."""
    if not _norm_abs(path):
        raise Refused("path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in [p for p in path.split("/") if p]:
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError:
                raise Refused("path component") from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _statfs_type(fd):
    """The filesystem magic of the open directory `fd` (stdlib has no statfs: call libc's fstatfs)."""
    buf = ctypes.create_string_buffer(512)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(fd, buf) != 0:
        raise Refused("statfs")
    return ctypes.c_long.from_buffer(buf).value


# ── validation against the config ─────────────────────────────────────────────────────────────────

def validate(spec, conf):
    """Everything the config decides. Returns (uid, gid)."""
    uid = spec["worker_uid"]
    if not conf["uid_min"] <= uid <= conf["uid_max"] or uid in (0, conf["core_uid"]):
        raise Refused("uid")
    gid = uid                          # the private group has the same number: the spec cannot choose another
    if gid in (0, conf["core_gid"]):
        raise Refused("gid")
    return uid, gid


def check_identity_unused(uid, gid):
    """The reserved range must belong to no account at all: no passwd entry for the uid, no account (any uid) whose
    primary group is this gid, and a group of that number with no members. A same-numbered private group is not an
    excuse: an account in the range would share the session's identity, files and socket."""
    try:
        pwd.getpwuid(uid)
    except KeyError:
        pass
    else:
        raise Refused("uid belongs to an account")
    if any(entry.pw_gid == gid for entry in pwd.getpwall()):
        raise Refused("gid is an account's primary group")
    try:
        if grp.getgrgid(gid).gr_mem:
            raise Refused("group has members")
    except KeyError:
        pass


def open_cwd(spec, conf, uid):
    """Open the cwd below the workspace root with no symlink anywhere; it must be a directory owned by Core or by
    the session uid. Returns the fd (kept open: the later fchdir uses it, so nothing can be swapped in between)."""
    cwd, root = spec["cwd"], conf["workspace_root"]
    if not _norm_abs(cwd) or not cwd.startswith(root + "/") or cwd == root:
        raise Refused("cwd outside the workspace root")
    fd = open_dir_nofollow(cwd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (uid, conf["core_uid"]):
            raise Refused("cwd owner")
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_argv0(argv0):
    try:
        info = os.stat(argv0)
    except OSError:
        raise Refused("argv[0]") from None
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise Refused("argv[0] is not an executable file")


# ── effects ───────────────────────────────────────────────────────────────────────────────────────

def hand_over_workspace(dir_fd, uid, conf):
    """The session owns its workspace; Core's group keeps read/write/cleanup access through setgid inheritance, so
    an unprivileged Core can read result.json and remove the directory later. Only regular, unlinked-elsewhere
    files directly inside are handed over; anything else is refused."""
    names = os.listdir(dir_fd)
    if len(names) > MAX_WORKSPACE_ENTRIES:
        raise Refused("workspace has too many entries")
    for name in names:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused("workspace entry")
        os.chown(name, uid, conf["core_gid"], dir_fd=dir_fd, follow_symlinks=False)
    os.fchown(dir_fd, uid, conf["core_gid"])
    os.fchmod(dir_fd, 0o2770)


def make_cgroup(conf, session_id, uid):
    """Create the session cgroup under the (cgroup2) root and give Core the control files. Returns (path, dir_fd)."""
    root_fd = open_dir_nofollow(conf["cgroup_root"])
    try:
        if _statfs_type(root_fd) != CGROUP2_SUPER_MAGIC:
            raise Refused("cgroup root is not cgroup2")
        try:
            os.mkdir(session_id, 0o755, dir_fd=root_fd)
        except OSError:
            raise Refused("cgroup create") from None
        made = True
        try:
            dir_fd = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
            try:
                os.fchown(dir_fd, conf["core_uid"], conf["core_gid"])
                os.fchmod(dir_fd, 0o755)           # whatever the caller's umask was: the session must read cgroup.events
                for name in ("cgroup.procs", "cgroup.freeze", "cgroup.kill"):
                    os.chown(name, conf["core_uid"], conf["core_gid"], dir_fd=dir_fd, follow_symlinks=False)
                    os.chmod(name, 0o644, dir_fd=dir_fd)
                if conf["pids_max"]:
                    fd = os.open("pids.max", os.O_WRONLY | os.O_CLOEXEC, dir_fd=dir_fd)
                    try:
                        os.write(fd, str(conf["pids_max"]).encode())
                    finally:
                        os.close(fd)
            except BaseException:
                os.close(dir_fd)
                raise
        except BaseException:
            if made:
                try:
                    os.rmdir(session_id, dir_fd=root_fd)
                except OSError:
                    pass
            raise
        return conf["cgroup_root"] + "/" + session_id, session_id, dir_fd
    finally:
        os.close(root_fd)


def remove_cgroup(conf, session_id):
    try:
        root_fd = open_dir_nofollow(conf["cgroup_root"])
    except (Refused, OSError):
        return
    try:
        os.rmdir(session_id, dir_fd=root_fd)
    except OSError:
        pass
    finally:
        os.close(root_fd)


SOCKET_NAMES = ("worker.sock", "proxy.sock")


def adopt_sock_dir(conf, session_id, uid, gid):
    """Core made `<sock_root>/<session_id>` and bound the session's sockets in it BEFORE asking for the launch (so
    nothing can race the agent's first connect). Here the helper checks exactly that (a real directory owned by Core,
    holding only `worker.sock` and optionally `proxy.sock`, each a socket owned by Core with one link), then hands
    the sockets to the session (uid:gid, mode 0600) and the directory to Core with the session's private group, mode 0750, so
    only that session can reach them. Nothing is created or removed here (Core's cleanup removes the directory)."""
    root_fd = open_dir_nofollow(conf["sock_root"])
    try:
        try:
            fd = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        except OSError:
            raise Refused("socket dir") from None
    finally:
        os.close(root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != conf["core_uid"]:
            raise Refused("socket dir owner")
        names = sorted(os.listdir(fd))
        if "worker.sock" not in names or not set(names) <= set(SOCKET_NAMES):
            raise Refused("socket dir contents")
        pinned = []
        try:
            for name in names:                 # pin each inode first: what is checked is exactly what is changed
                held = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                pinned.append(held)
                entry = os.fstat(held)
                if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != conf["core_uid"] or entry.st_nlink != 1:
                    raise Refused("socket entry")
            for held in pinned:                # /proc/self/fd/N follows to the pinned inode, never to a path
                os.chown(f"/proc/self/fd/{held}", uid, gid)
                os.chmod(f"/proc/self/fd/{held}", 0o600)
        finally:
            for held in pinned:
                os.close(held)
        os.fchown(fd, conf["core_uid"], gid)
        os.fchmod(fd, 0o750)
    finally:
        os.close(fd)
    return conf["sock_root"] + "/" + session_id


def token_pipe(token):
    """A pipe holding the token (it is far smaller than the pipe buffer); returns the read end."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, token.encode("ascii"))
    finally:
        os.close(write_end)
    return read_end


def arrange_fds(token_fd, out_fd=1):
    """Exactly 0 /dev/null, 1 and 2 the merged output, 3 the token read end. Nothing above 3 survives."""
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)
    if devnull != 0:
        os.close(devnull)
    if token_fd != 3:
        os.dup2(token_fd, 3, inheritable=True)
        os.close(token_fd)
    else:
        os.set_inheritable(3, True)
    os.dup2(1, 2)                              # stderr is the same merged pipe as stdout
    limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    os.closerange(4, limit if limit != resource.RLIM_INFINITY else 1 << 20)


def drop_privileges(uid, gid):
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if (os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid) or os.getgroups()):
        raise Refused("identity not dropped")
    try:
        os.setuid(0)
    except PermissionError:
        pass
    else:
        raise Refused("root regained")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise Refused("no_new_privs")


def new_session():
    """Lead a session of our own. setsid() is refused for a process-group leader (which sudo or Core's Popen may have
    made us); then a forked child, which cannot be one, does it and the parent leaves. Everything the child needs
    (fds, cgroup membership, identity) is inherited, and the parent exits before it has written anything."""
    try:
        os.setsid()
        return
    except PermissionError:
        pass
    if os.fork() != 0:
        os._exit(0)
    os.setsid()


def reset_process_state():
    new_session()
    os.umask(0o007)                            # files in the setgid workspace stay group-accessible to Core only
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, hard) if hard != resource.RLIM_INFINITY else 1024, hard))
    for number in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


def say(obj):
    os.write(1, (STATUS_PREFIX + json.dumps(obj, separators=(",", ":")) + "\n").encode())


# ── the whole thing ───────────────────────────────────────────────────────────────────────────────

def launch(raw, conf):
    """Validate and start the session. Returns only by raising; on success it does not return (execve)."""
    spec = check_wire(parse_json(raw))
    uid, gid = validate(spec, conf)
    check_identity_unused(uid, gid)
    check_argv0(spec["argv"][0])
    sid = spec["session_id"]
    cwd_fd = open_cwd(spec, conf, uid)
    cgroup_made = False
    try:
        sock_path = adopt_sock_dir(conf, sid, uid, gid) if spec.get("sock_dir") else None
        cgroup_path, _, cg_fd = make_cgroup(conf, sid, uid)
        cgroup_made = True
        hand_over_workspace(cwd_fd, uid, conf)
        procs_fd = os.open("cgroup.procs", os.O_WRONLY | os.O_CLOEXEC, dir_fd=cg_fd)
        token_fd = token_pipe(spec["token"])
    except BaseException:
        if cgroup_made:
            remove_cgroup(conf, sid)
        raise
    # Past this point the helper is inside the cgroup; a failure ends the process and Core removes the empty cgroup.
    os.write(procs_fd, b"0")                   # "0" means the writing process itself
    os.close(procs_fd)
    os.close(cg_fd)
    drop_privileges(uid, gid)
    reset_process_state()
    os.fchdir(cwd_fd)
    status = {"ok": True, "session_id": sid, "uid": uid, "gid": gid, "cgroup": cgroup_path}
    if sock_path:
        status["sock_dir"] = sock_path
    arrange_fds(token_fd)
    signal.alarm(0)
    say(status)
    os.execve(spec["argv"][0], spec["argv"], spec["env"])


def main():
    try:
        signal.alarm(TIMEOUT_S)
        os.umask(0o022)                            # the caller's umask must not change the modes of what is made below
        sudo_uid = os.environ.get("SUDO_UID")
        os.environ.clear()
        os.environ["PATH"] = "/usr/bin:/bin"
        if len(sys.argv) != 1:
            raise Refused("no arguments are accepted")
        if os.geteuid() != 0 or os.getuid() != 0:
            raise Refused("not root")
        check_trusted_path(os.path.abspath(__file__))
        raw = sys.stdin.buffer.read(MAX_SPEC_BYTES + 1)
        if len(raw) > MAX_SPEC_BYTES:
            raise Refused("spec too large")
        conf = load_conf()
        if sudo_uid is not None and sudo_uid != str(conf["core_uid"]):
            raise Refused("invoker is not Core")
        launch(raw, conf)
    except Refused as exc:
        say({"ok": False, "error": exc.code})
    except Exception:  # noqa: BLE001 -- fail closed with a fixed code; nothing from the spec may be echoed
        say({"ok": False, "error": "internal"})
    sys.exit(1)


if __name__ == "__main__":
    main()
