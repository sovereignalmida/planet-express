"""Validate and atomically persist configuration without loading runtime config."""

import errno
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
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError([{"loc": "", "msg": f"Config file not found: {path}"}]) from exc
    except UnicodeError as exc:
        raise ConfigError([{"loc": "", "msg": str(exc)}]) from exc
    model, errors = validate_config_text(text)
    if errors:
        raise ConfigError(errors)
    return model


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


def write_config_text(text: str, path: Path) -> PlanetExpressConfig:
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
