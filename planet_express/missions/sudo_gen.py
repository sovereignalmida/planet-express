"""The generated sudoers file for the worker tier (contract 15, brief 6.8). Pure text, no I/O.

What a Repair session may do as root is exactly a list of `systemctl <start|stop|restart> <unit>` argv: the unit and the action
come from validated rules, the program path is fixed, and sudoers matches the arguments EXACTLY (a rule naming arguments allows
no others). There is no wildcard, no command alias that expands to `ALL`, no environment passing, and nothing in the output is
copied from input that has not passed a character whitelist, so no rule input can make the text mean more than it says.

The principals are the session uids themselves (`#uid` tokens in small aliases, the reserved range minus its top uid, which
only preflight uses): a session can use the rules and nothing else can. The file is a host-wide CEILING, written at setup; a grant's own
`sudo.rules` must sit inside it (`outside_ceiling`). `sudo.mode = broad` is never generated: it is a per-mission elevated approval.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SYSTEMCTL = "/usr/bin/systemctl"
ACTIONS = ("start", "stop", "restart")
SUDOERS_NAME = "planetexpress-worker"
MAX_RULES = 256
MAX_UIDS = 4096
UIDS_PER_ALIAS = 100
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.(service|socket|timer|mount|path|target)$")
_PROGRAM = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
HEADER = "# Managed by Planet Express (worker sudo scope). Do not edit: setup rewrites it and the side-door check hashes it.\n"


class SudoGenError(ValueError):
    """A rule or range the generator will not turn into sudoers text."""


@dataclass(frozen=True)
class Rule:
    unit: str
    actions: tuple[str, ...]


def normalise(rules: Iterable) -> list[Rule]:
    """Validated, merged by unit, sorted: the same set of rules always renders the same bytes. Accepts (unit, actions) pairs,
    dicts with `unit` and `actions`, or objects with those attributes."""
    merged: dict[str, set[str]] = {}
    for rule in rules:
        if isinstance(rule, dict):
            unit, actions = rule.get("unit"), rule.get("actions")
        elif isinstance(rule, (tuple, list)):
            unit, actions = rule
        else:
            unit, actions = getattr(rule, "unit", None), getattr(rule, "actions", None)
        if not isinstance(unit, str) or not _UNIT.fullmatch(unit) or re.search(r"\bALL\b", unit):
            raise SudoGenError(f"not a plain systemd unit name: {str(unit)[:60]!r}")
        if not isinstance(actions, (list, tuple, set, frozenset)) or not actions:
            raise SudoGenError(f"{unit}: a rule needs at least one action")
        for action in actions:
            if action not in ACTIONS:
                raise SudoGenError(f"{unit}: action {str(action)[:20]!r} is not one of {', '.join(ACTIONS)}")
        merged.setdefault(unit, set()).update(actions)
    if len(merged) > MAX_RULES:
        raise SudoGenError(f"more than {MAX_RULES} units")
    return [Rule(unit, tuple(a for a in ACTIONS if a in merged[unit])) for unit in sorted(merged)]


def commands(rules: Sequence[Rule], systemctl: str = SYSTEMCTL) -> list[str]:
    if not _PROGRAM.fullmatch(systemctl) or ".." in systemctl.split("/"):
        raise SudoGenError("the systemctl path is not a plain absolute path")
    return [f"{systemctl} {action} {rule.unit}" for rule in rules for action in rule.actions]


def render(rules: Iterable, *, uid_min: int, uid_max: int, systemctl: str = SYSTEMCTL) -> bytes | None:
    """The sudoers text for these rules, or None when there are none (no file should exist). Sessions hold uids
    `uid_min .. uid_max - 1`; the top one is reserved for preflight and is never named."""
    clean = normalise(rules)
    if not clean:
        return None
    if not (isinstance(uid_min, int) and isinstance(uid_max, int) and 0 < uid_min < uid_max < 2**31):
        raise SudoGenError("the session uid range is malformed")
    count = uid_max - uid_min
    if count > MAX_UIDS:
        raise SudoGenError(f"the session range holds more than {MAX_UIDS} uids")
    uids = [f"#{uid}" for uid in range(uid_min, uid_max)]
    cmds = commands(clean, systemctl)
    lines = [HEADER.rstrip("\n")]
    # sudo's own parser refuses a single list of around a thousand items ("too many items in list"), so the uids are
    # chunked into small aliases and the rule names the aliases.
    chunks = [uids[i:i + UIDS_PER_ALIAS] for i in range(0, len(uids), UIDS_PER_ALIAS)]
    for number, chunk in enumerate(chunks):
        rows = [", ".join(chunk[i:i + 16]) for i in range(0, len(chunk), 16)]
        lines.append(f"User_Alias PE_SESSION_UIDS_{number} = " + ", \\\n    ".join(rows))
    lines.append("User_Alias PE_SESSIONS = " + ", ".join(f"PE_SESSION_UIDS_{n}" for n in range(len(chunks))))
    lines.append("Cmnd_Alias PE_WORKER_CMDS = " + ", \\\n    ".join(cmds))
    lines.append("Defaults:PE_SESSIONS env_reset, !setenv, !env_editor")
    lines.append(f"Defaults!{systemctl} env_reset, !setenv, !env_editor")
    lines.append("PE_SESSIONS ALL = (root) NOPASSWD: PE_WORKER_CMDS")
    return ("\n".join(lines) + "\n").encode("ascii")


def digest(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def outside_ceiling(ceiling: Iterable, grant_rules: Iterable) -> list[str]:
    """Every (unit, action) a grant asks for that the host-wide file does not grant, as readable problems."""
    try:
        allowed = {r.unit: set(r.actions) for r in normalise(ceiling)}
    except SudoGenError:
        allowed = {}
    out = []
    for rule in grant_rules:
        for action in getattr(rule, "actions", ()):
            if action not in allowed.get(getattr(rule, "unit", None), set()):
                out.append(f"sudo rule {action} {str(getattr(rule, 'unit', ''))[:80]} is not in the host's worker sudoers")
    return out


__all__ = [
    "ACTIONS",
    "HEADER",
    "MAX_UIDS",
    "SUDOERS_NAME",
    "SYSTEMCTL",
    "Rule",
    "SudoGenError",
    "commands",
    "digest",
    "normalise",
    "outside_ceiling",
    "render",
]
