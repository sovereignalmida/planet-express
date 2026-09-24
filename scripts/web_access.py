"""
scripts/web_access.py — give the dashboard's own user (planetexpress-web) read-only access to
exactly what it needs (landing 1c, T10).

Split into a pure plan_web_access() (unit-tested with injected lookups, no host access) and a
thin main() that applies the plan with sudo, same pattern as setup_wizard.py.

Least privilege by ALLOWLIST, not blocklist. Once the web user can traverse into the clone,
anything world-readable there becomes readable to it, and a real host keeps untracked files
beside the code that the dashboard must never see (the live host has `.mcp.json` and
`scpdump/`; `logs/` holds Bender step output). So allowlisted top-level entries get a recursive
read grant and every other top-level entry gets an explicit `u:<web_user>:---` ACL entry, which
POSIX ACL evaluation applies INSTEAD of the "other" permission bits.

Re-run it after upgrades that add top-level entries (deploy.sh does). A new top-level file that
isn't covered yet falls back to its normal permission bits until then.
"""

import grp
import os
import pwd
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

# What casa_scruffy.py imports or serves: top-level modules (*.py), the package, Flask's
# templates/static, and the venv. __pycache__ only avoids needless bytecode-read failures.
READABLE_DIRS = frozenset({"planet_express", "templates", "static", "venv", "__pycache__"})
STATE_DIR = "state"


def _readable(entry: str) -> bool:
    return entry in READABLE_DIRS or entry.endswith(".py")


def plan_web_access(install_dir, run_user, web_user="planetexpress-web",
                    rpc_group="planetexpress-rpc", *, user_exists, group_exists,
                    in_group, other_can_traverse, entries, config_file=None) -> list[list[str]]:
    """Return an ordered list of argv commands, each safe to re-run.

    `entries` are the install dir's top-level names (listed by the caller). `in_group` takes
    (user, group). `config_file` optionally covers a topology config outside the clone, or a
    config.yaml directly at the clone's top level."""
    install_dir = Path(install_dir)
    if not install_dir.is_absolute():
        raise ValueError("install_dir must be absolute")
    entries = sorted(entries)
    if any(Path(entry).name != entry or entry in {".", ".."} for entry in entries):
        raise ValueError("entries must be top-level names")
    if config_file is not None:
        config_file = Path(config_file)
        if not config_file.is_absolute():
            raise ValueError("config_file must be absolute")
        if config_file.is_relative_to(install_dir) and config_file.parent != install_dir:
            # A nested config would sit under a denied (or state/data) directory.
            raise ValueError("a config_file inside the install dir must be at its top level")

    commands = []
    if not group_exists(rpc_group):
        commands.append(["groupadd", "--system", rpc_group])
    if not user_exists(web_user):
        commands.append(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent",
                         "--shell", "/usr/sbin/nologin", "--user-group", web_user])
    for user in (run_user, web_user):
        if not in_group(user, rpc_group):
            commands.append(["usermod", "-aG", rpc_group, user])

    traversed = set()

    def traverse(path):
        for parent in reversed(path.parents):
            if parent == Path("/") or parent in traversed:
                continue
            traversed.add(parent)
            if not other_can_traverse(parent):
                commands.append(["setfacl", "-m", f"u:{web_user}:x", str(parent)])

    traverse(install_dir)
    commands.append(["setfacl", "-m", f"u:{web_user}:rx", str(install_dir)])
    for entry in entries:
        if entry == STATE_DIR:
            continue
        path = str(install_dir / entry)
        if _readable(entry):
            commands.append(["setfacl", "-R", "-m", f"u:{web_user}:rX", path])
        else:
            # data/ (core-only SQLite), logs/, .git and anything untracked: explicit deny,
            # which takes precedence over world-readable "other" bits.
            commands.append(["setfacl", "-m", f"u:{web_user}:---", path])

    state = str(install_dir / STATE_DIR)
    commands.extend([
        ["setfacl", "-R", "-m", f"u:{web_user}:rX", state],
        ["setfacl", "-d", "-m", f"u:{web_user}:rX", state],
        # Existing snapshots are world-readable and rewritten in place, so the core unit's
        # UMask alone would never remove that; the store holds approved compose contents.
        ["chmod", "-R", "o-rwx", state],
    ])

    if config_file is not None:
        if not config_file.is_relative_to(install_dir):
            traverse(config_file)
        # Last on purpose: a top-level config.yaml in the clone was just denied above, and this
        # later -m replaces that entry with read access.
        commands.append(["setfacl", "-m", f"u:{web_user}:r", str(config_file)])
    return commands


def _user_exists(name):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _group_exists(name):
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _in_group(user, group):
    try:
        account = pwd.getpwnam(user)
        return grp.getgrnam(group).gr_gid in os.getgrouplist(user, account.pw_gid)
    except KeyError:
        return False


def main() -> None:
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("Don't run this as root (or via sudo); run as the unprivileged install user.")
    if shutil.which("setfacl") is None:
        # Fail before any groupadd/useradd, not partway through the plan.
        raise SystemExit("setfacl not found -- install the acl package (e.g. sudo apt install acl) and re-run.")
    install_dir = Path(__file__).resolve().parent.parent
    run_user = pwd.getpwuid(os.getuid()).pw_name
    commands = plan_web_access(
        install_dir, run_user, entries=[entry.name for entry in install_dir.iterdir()],
        config_file=Path(os.environ.get("CASA_CONFIG", "/etc/planetexpress/config.yaml")).resolve(),
        user_exists=_user_exists, group_exists=_group_exists, in_group=_in_group,
        other_can_traverse=lambda path: bool(path.stat().st_mode & stat.S_IXOTH),
    )
    for command in commands:
        subprocess.run(["sudo", *command], check=True)
        print(shlex.join(command))


if __name__ == "__main__":
    main()
