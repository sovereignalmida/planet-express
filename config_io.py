"""Validate and atomically persist configuration without loading runtime config."""

import errno
import hashlib
import os
import stat
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from config_schema import PlanetExpressConfig


class ConfigError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("\n".join(f"{e['loc']}: {e['msg']}" for e in errors))


def validate_config_text(text: str) -> tuple[PlanetExpressConfig | None, list[dict]]:
    try:
        raw = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as exc:
        return None, [{"loc": "", "msg": str(exc)}]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, [{"loc": "", "msg": "Config must be a mapping"}]
    try:
        return PlanetExpressConfig.model_validate(raw), []
    except ValidationError as exc:
        return None, [
            {"loc": ".".join(str(part) for part in e["loc"]), "msg": e["msg"]}
            for e in exc.errors()
        ]


def load_config_file(path: Path) -> PlanetExpressConfig:
    return load_config_file_with_sha256(path)[0]


def load_config_file_with_sha256(path: Path) -> tuple[PlanetExpressConfig, str]:
    """Load and validate, returning the SHA-256 of the exact bytes loaded, so a running process
    can say which config it is actually using (the file on disk may be newer — T35)."""
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except FileNotFoundError as exc:
        raise ConfigError([{"loc": "", "msg": f"Config file not found: {path}"}]) from exc
    except UnicodeError as exc:
        raise ConfigError([{"loc": "", "msg": str(exc)}]) from exc
    model, errors = validate_config_text(text)
    if errors:
        raise ConfigError(errors)
    return model, hashlib.sha256(data).hexdigest()


_ACL_XATTR = "system.posix_acl_access"


def _copy_access_acl(source: Path, target: Path) -> None:
    """Carry the POSIX access ACL onto the replacement file, AFTER chmod (the ACL's mask
    entry sets the group bits, so chmod first would clobber it).

    scripts/web_access.py grants the dashboard's separate user read access with a named-user
    ACL entry. os.replace swaps in a fresh inode that has no ACL, so without this a config
    applied through core would silently lock the dashboard out of its own config (Codex
    review, T26). No ACL on the source, or a filesystem without ACL support, is a no-op.
    Failing to SET an ACL that exists raises: an apply that strips access must not succeed.
    """
    try:
        acl = os.getxattr(source, _ACL_XATTR)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    os.setxattr(target, _ACL_XATTR, acl)


class ConfigConflict(Exception):
    """The file changed on disk after the caller last read it."""


def write_config_text(
    text: str, path: Path, *, expected_sha256: str | None = None
) -> PlanetExpressConfig:
    """Validate, then atomically replace `path` with `text`.

    `expected_sha256`, when given, is re-checked against the file immediately before the rename, so
    an edit made on the host while the draft was being validated and written is not overwritten
    (Codex review round 3, T35). Without an OS-level lock the editor could still write in the
    microseconds between that read and the rename; accepted: host editors take no lock to honour."""
    model, errors = validate_config_text(text)
    if errors:
        raise ConfigError(errors)
    try:
        existing = path.stat()
    except FileNotFoundError:
        existing = None
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            if existing is not None:
                try:
                    os.chown(temp, existing.st_uid, existing.st_gid)
                except PermissionError:
                    pass
            os.chmod(temp, stat.S_IMODE(existing.st_mode) if existing else 0o640)
            if existing is not None:
                _copy_access_acl(path, temp)
        if expected_sha256 is not None:
            try:
                current = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                current = None
            if current != expected_sha256:
                raise ConfigConflict("config changed on disk since it was loaded")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return model
