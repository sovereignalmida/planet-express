"""The launch spec: what Core hands the exec helper on stdin, and the bounds both sides enforce.

`pe_launch.py` is audited alone and imports nothing from this package, so `check_wire` below is a deliberate
copy of its `check_wire`; `tests/test_launcher_spec.py` runs both over the same corpus and requires identical
verdicts. The spec NEVER carries a gid, a cgroup root, a uid range or a workspace root: those come from the
root-owned launch.conf the helper loads itself (section 22.2).
"""

import json
import re
from dataclasses import dataclass, field

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_SPEC_BYTES = 60_000                  # under the 64 KiB pipe buffer: writing it can never block
MAX_ARGV_ITEMS, MAX_ARG_CHARS, MAX_ARGV_CHARS = 64, 4096, 32_768
MAX_ENV_ITEMS, MAX_ENV_VALUE_CHARS, MAX_ENV_CHARS = 64, 4096, 32_768
MAX_CWD_CHARS = 1024
TOKEN_CHARS = (16, 256)
STATE_DIR = re.compile(r"C-[A-Za-z0-9_-]{1,62}")
KEYS = frozenset({"session_id", "worker_uid", "cwd", "argv", "env", "token", "sock_dir", "state_dir"})
REQUIRED = KEYS - {"sock_dir", "state_dir"}
STATUS_PREFIX = "PE-LAUNCH-STATUS "


class SpecError(ValueError):
    """The spec is not acceptable. The message is a fixed phrase and never echoes spec content (it holds a token)."""


def _clean(text) -> bool:
    return isinstance(text, str) and "\0" not in text


def check_wire(obj) -> dict:
    """Shape and bounds of a decoded spec. Returns it unchanged or raises SpecError."""
    if not isinstance(obj, dict) or not REQUIRED <= set(obj) <= KEYS:
        raise SpecError("spec keys")
    sid, uid, cwd, argv, env, token = (obj[k] for k in ("session_id", "worker_uid", "cwd", "argv", "env", "token"))
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise SpecError("session_id")
    if type(uid) is not int or uid <= 0:
        raise SpecError("worker_uid")
    if not _clean(cwd) or not cwd.startswith("/") or len(cwd) > MAX_CWD_CHARS:
        raise SpecError("cwd")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV_ITEMS
            or not all(_clean(a) and len(a) <= MAX_ARG_CHARS for a in argv)
            or sum(len(a) for a in argv) > MAX_ARGV_CHARS):
        raise SpecError("argv")
    if not argv[0].startswith("/"):
        raise SpecError("argv[0] must be absolute")
    if not isinstance(env, dict) or len(env) > MAX_ENV_ITEMS:
        raise SpecError("env")
    total = 0
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or name.startswith("LD_"):
            raise SpecError("env name")
        if not _clean(value) or len(value) > MAX_ENV_VALUE_CHARS:
            raise SpecError("env value")
        total += len(name) + len(value)
    if total > MAX_ENV_CHARS:
        raise SpecError("env size")
    if (not isinstance(token, str) or not TOKEN_CHARS[0] <= len(token) <= TOKEN_CHARS[1]
            or not all(0x21 <= ord(c) <= 0x7E for c in token)):
        raise SpecError("token")
    if "sock_dir" in obj and not isinstance(obj["sock_dir"], bool):
        raise SpecError("sock_dir")
    if "state_dir" in obj and (not isinstance(obj["state_dir"], str) or not STATE_DIR.fullmatch(obj["state_dir"])):
        raise SpecError("state_dir")
    return obj


@dataclass(frozen=True)
class LaunchSpec:
    """One session's launch. `uid` is allocated by `start_session`; the launcher only validates it."""
    uid: int
    cwd: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    token: str = field(default="", repr=False)          # never in a repr, a log or an exception
    sock_dir: bool = False
    state_dir: str | None = None          # `C-<id>`: a conversation's state directory beside the workspaces (see pe_launch.adopt_state_dir)

    def to_wire(self, session_id: str) -> bytes:
        obj = {"session_id": session_id, "worker_uid": self.uid, "cwd": self.cwd, "argv": list(self.argv),
               "env": dict(self.env), "token": self.token, "sock_dir": self.sock_dir}
        if self.state_dir is not None:             # sent only when used: a helper that does not know the key refuses it, fail closed
            obj["state_dir"] = self.state_dir
        check_wire(obj)
        data = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > MAX_SPEC_BYTES:
            raise SpecError("spec too large")
        return data


__all__ = ["KEYS", "MAX_SPEC_BYTES", "SESSION_ID", "STATUS_PREFIX", "LaunchSpec", "SpecError", "check_wire"]
