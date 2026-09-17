"""Snapshot and restore core state without loading or validating the config."""

import argparse
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Standard library ONLY, and nothing from this repository: rollback runs this script while the
# checkout may be an older tag, or from a copy kept outside the clone (Codex review, T23).
_ACL_XATTR = "system.posix_acl_access"
# The only states in which the core provably holds no database connection.
_STOPPED_STATES = {"inactive", "failed"}


def copy_access_acl(source, target):
    """Carry the POSIX access ACL (e.g. web_access.py's named-user read grant for the dashboard)
    onto the replacement file. No ACL or no ACL support on the source is a no-op; failing to set
    an ACL that exists raises, so a restore never silently strips access."""
    try:
        acl = os.getxattr(source, _ACL_XATTR)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    os.setxattr(target, _ACL_XATTR, acl)


def paths():
    data = Path(os.environ.get("CASA_DATA_DIR", str(REPO / "data")))
    return data, {
        "planetexpress.db": data / "planetexpress.db",
        "config.yaml": Path(os.environ.get("CASA_CONFIG", "/etc/planetexpress/config.yaml")),
    }


def private_directory(path):
    if not path.exists():
        private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create(label=None):
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") if label else ""
    data, sources = paths()
    root = data / "snapshots"
    private_directory(root)
    root.chmod(0o700)
    now = datetime.now(timezone.utc)
    stamp = now
    while True:
        destination = root / (stamp.strftime("%Y%m%dT%H%M%SZ") + (f"-{suffix}" if suffix else ""))
        try:
            destination.mkdir(mode=0o700)
            break
        except FileExistsError:
            stamp += timedelta(seconds=1)
    try:
        manifest = {
            "created_at": now.isoformat(), "label": label,
            "source_paths": {name: str(path.absolute()) for name, path in sources.items()},
            "schema_version": None, "git_describe": None,
            "sha256": {name: None for name in sources},
        }
        for name, source in sources.items():
            if not source.exists():
                continue
            target = destination / name
            if name == "planetexpress.db":
                with (
                    closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)) as src,
                    closing(sqlite3.connect(target)) as dst,
                ):
                    src.backup(dst)
                    manifest["schema_version"] = dst.execute("PRAGMA user_version").fetchone()[0]
            else:
                shutil.copy2(source, target)
            manifest["sha256"][name] = sha256(target)
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=REPO, capture_output=True, text=True, check=True,
            )
            manifest["git_describe"] = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination


def list_snapshots():
    root = paths()[0] / "snapshots"
    entries = []
    if root.exists():
        for directory in root.iterdir():
            if directory.is_dir() and (directory / "manifest.json").exists():
                manifest = json.loads((directory / "manifest.json").read_text())
                entries.append((manifest["created_at"], directory.name, manifest))
    for _, name, manifest in sorted(entries, key=lambda entry: entry[:2], reverse=True):
        print(f"{name}\t{manifest['schema_version']}\t{manifest['git_describe']}")


def restore(snapshot, *, yes=False, no_service_check=False, skip_config=False):
    if not yes:
        raise ValueError("Restore requires --yes; it discards changes made after the snapshot")
    # Require POSITIVE proof the core is stopped. `is-active` exits non-zero for
    # "deactivating" and for a failed bus connection alike, and treating either as "stopped"
    # would replace a database the core may still be writing to (Codex review, T23).
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=ActiveState", "--value", "casa-planetexpress"],
            capture_output=True, text=True, check=False,
        )
        state = result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        state = None
    if state == "active" or state in ("activating", "deactivating", "reloading"):
        raise ValueError(f"Core service is {state}; stop casa-planetexpress first")
    if state not in _STOPPED_STATES and not no_service_check:
        raise ValueError(
            "Could not confirm casa-planetexpress is stopped; stop it, then re-run with "
            "--no-service-check only if you are certain"
        )

    data, targets = paths()
    directory = Path(snapshot)
    if directory.name == snapshot:
        directory = data / "snapshots" / snapshot
    manifest = json.loads((directory / "manifest.json").read_text())
    files = []
    remove = []
    for name, target in targets.items():
        source = directory / name
        expected = manifest["sha256"][name]
        if expected is None and not source.exists():
            # The snapshot recorded this file as ABSENT. Keeping a live file created later
            # would silently retain newer state (e.g. a database with a newer schema) while
            # reporting success (Codex review, T23).
            if name == "config.yaml":
                if target.exists():
                    raise ValueError(
                        "Snapshot has no config.yaml but one exists at "
                        f"{target}; the config probably lived at another path, so refusing "
                        "rather than deleting it"
                    )
            elif target.exists():
                remove.append(target)
            continue
        if not isinstance(expected, str) or not source.is_file() or sha256(source) != expected:
            raise ValueError(f"Snapshot checksum verification failed: {name}")
        files.append((source, target))

    # A default install keeps the config in root-owned /etc/planetexpress, which the
    # unprivileged core user cannot write (Codex review, T23). Refuse BEFORE touching anything
    # and hand over the privileged step instead of failing midway. `cp` onto the existing file
    # rewrites it in place, so its owner, mode and dashboard ACL all survive.
    config_step = None
    for index, (source, target) in enumerate(files):
        if source.name != "config.yaml":
            continue
        if skip_config or not os.access(target.parent, os.W_OK):
            # Quoted, with `--`: an install path may contain shell metacharacters or start with
            # a dash, and this line is meant to be pasted into a shell (Codex review, T23).
            config_step = f"sudo cp --no-preserve=all -- {shlex.quote(str(source))} {shlex.quote(str(target))}"
            if not skip_config:
                raise ValueError(
                    f"Cannot write {target.parent} as this user. Re-run with --skip-config to "
                    f"restore the database, then restore the config with:\n  {config_step}"
                )
            del files[index]
        break

    backup = create("pre-restore")
    print(f"Pre-restore snapshot: {backup}")
    staged = []
    try:
        for source, target in files:
            private_directory(target.parent)
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temporary = Path(name)
            staged.append((temporary, target))
            with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
                shutil.copyfileobj(original, stream)
                os.fchmod(stream.fileno(), source.stat().st_mode & 0o7777)
                stream.flush()
                os.fsync(stream.fileno())
            # Keep the LIVE file's owner and access ACL, not the snapshot copy's: web_access.py
            # grants the dashboard read access to the config through a named-user ACL, and a
            # bare os.replace would silently strip it (same finding as T26's write path).
            if target.exists():
                existing = target.stat()
                try:
                    os.chown(temporary, existing.st_uid, existing.st_gid)
                except PermissionError:
                    pass
                copy_access_acl(target, temporary)
        for target in remove:
            for suffix in ("-wal", "-shm", ""):
                Path(str(target) + suffix).unlink(missing_ok=True)
            print(f"Removed {target} (absent in snapshot)")
        for temporary, target in staged:
            if target == targets["planetexpress.db"]:
                for suffix in ("-wal", "-shm"):
                    Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(temporary, target)
            print(f"Restored {target}")
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    if config_step:
        print(f"Config NOT restored (--skip-config). Restore it with:\n  {config_step}")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create").add_argument("--label")
    commands.add_parser("list")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("snapshot")
    restore_parser.add_argument("--yes", action="store_true")
    restore_parser.add_argument("--no-service-check", action="store_true")
    restore_parser.add_argument("--skip-config", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "create":
            print(create(args.label))
        elif args.command == "list":
            list_snapshots()
        else:
            restore(args.snapshot, yes=args.yes, no_service_check=args.no_service_check,
                    skip_config=args.skip_config)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"State snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
