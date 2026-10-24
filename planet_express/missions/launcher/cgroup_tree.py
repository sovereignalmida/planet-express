"""Core's own half of the cgroup layout (contract 22.4), done at daemon start.

The delegated cgroup (`<delegated>`, from systemd's `Delegate=yes` or the MOS boot script) may not hold processes
AND have controllers enabled for its children, so Core moves itself into a `<delegated>/core` leaf, and session
cgroups are children of `<delegated>/sessions` (an empty parent, where `pids` can be enabled and `pids.max` set per
session). `missions.cgroup_root` names that `sessions` directory. Everything here is idempotent; any failure is
reported and left for the launcher preflight to turn into proposal-only.
"""

import os


def ensure_tree(sessions_root: str, *, core_leaf: str = "core", pid: int | None = None) -> list[str]:
    """Make `<delegated>/sessions` and `<delegated>/core`, move this process into the core leaf, and only then
    enable `pids` for the delegated children (the kernel refuses to enable a controller in a cgroup that still holds
    processes). Returns what it did (for the log)."""
    done: list[str] = []
    sessions = os.path.normpath(sessions_root)
    delegated = os.path.dirname(sessions)
    leaf = os.path.join(delegated, core_leaf)
    for path in (sessions, leaf):
        if not os.path.isdir(path):
            os.mkdir(path, 0o755)
            done.append(f"created {path}")
    if os.stat(sessions).st_mode & 0o755 != 0o755:
        # mkdir's mode went through Core's umask (the unit's UMask=0027 makes 0750): a session uid must be able to walk through
        # `sessions` to its own cgroup and read cgroup.events. Only `sessions`; the core leaf stays as the umask made it.
        os.chmod(sessions, 0o755)
        done.append(f"made {sessions} traversable by the session uids")
    me = os.getpid() if pid is None else pid
    procs = os.path.join(leaf, "cgroup.procs")
    with open(procs, encoding="ascii") as handle:
        members = handle.read().split()
    if str(me) not in members:
        with open(procs, "w", encoding="ascii") as handle:
            handle.write(str(me))
        done.append(f"moved {me} into {leaf}")
    control = os.path.join(delegated, "cgroup.subtree_control")
    with open(control, encoding="ascii") as handle:
        enabled = handle.read().split()
    if "pids" not in enabled:
        with open(control, "w", encoding="ascii") as handle:
            handle.write("+pids")
        done.append("enabled pids on the delegated cgroup")
    return done


__all__ = ["ensure_tree"]
