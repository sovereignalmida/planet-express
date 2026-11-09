"""Grant validation (contract 4) and the grant-to-environment builder (brief 6.3: default deny).

`validate_grant` is Core's check that a grant is sound BEFORE an operator is asked to approve it. It returns every
reason it refuses, never raises for a bad grant, and refuses on anything it cannot evaluate. In trust mode a grant says
what a Repair works on (for leases and verification), its budgets and its verification plan; it carries no access rules.

`build_session_env` returns exactly what a session's environment holds. Nothing is inherited: a variable the builder did not
put there is absent, a credential the grant does not name (as `tool:<name>`) has no route into the session, and a value that
equals a known secret (the model broker's key, a brokered tool's key) is dropped whatever its name. The session can read the
host through sudo anyway; this keeps PE's own secrets out of what it is handed. The grant hash is `Grant.hash()` (RFC 8785
canonical JSON, `canonical.py`); nothing here ever edits a grant.
"""

import os
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from .models import Grant, VerificationPlan, scope_file_problem
from .resources import BadResource, canon
from .store import grant_resources
from .verify.plan import binding_problems

_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASS|PASSWD|PASSWORD|CRED|AUTH|COOKIE|SESSION|PRIVATE|CERT)", re.IGNORECASE)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_ENV_VALUE = 4096
REPAIR_UP = ("repair", "change", "critical")
SCAN_MAX_FILES = 5000
SCAN_MAX_BYTES = 1 << 20


class EnvLeak(RuntimeError):
    """The session environment cannot be built safely. Fail closed: no session starts."""


@dataclass(frozen=True)
class Inventory:
    """What discovery found on this host: a grant's scope must be a subset of it."""
    hosts: Sequence[str] = ("local",)
    stacks: Sequence[str] = ()
    services: Sequence[str] = ()          # "<stack>/<service>"


@dataclass(frozen=True)
class SiteLimits:
    wall_s: int
    tool_calls: int
    output_bytes: int
    retries: int

    @classmethod
    def from_config(cls, budgets) -> "SiteLimits":
        return cls(budgets.wall_seconds, budgets.tool_calls, budgets.output_bytes, budgets.retries)


def grant_hash(grant: Grant) -> str:
    return grant.hash()


def validate_grant(grant: Grant, *, inventory: Inventory, known_checks: Collection[str], known_tools: Collection[str],
                   limits: SiteLimits, verification_plan: VerificationPlan | None = None) -> list[str]:
    """Every reason this grant must not be offered for approval; empty means valid."""
    out: list[str] = []
    scope = grant.scope
    for host in scope.hosts:
        if host not in inventory.hosts:
            out.append(f"scope host {host!r} is not in the discovered inventory")
    for stack in scope.stacks:
        if stack not in inventory.stacks:
            out.append(f"scope stack {stack!r} is not in the discovered inventory")
    for service in scope.services:
        if service not in inventory.services:
            out.append(f"scope service {service!r} is not in the discovered inventory")
    for n, path in enumerate(scope.files, 1):
        why = scope_file_problem(path)
        if why:
            out.append(f"scope file {n} {why}")
    try:
        scope_ids = [canon(r) for r in grant_resources(grant)]
    except BadResource as exc:
        return [*out, f"scope is malformed: {exc}"]
    if grant.tier in REPAIR_UP:
        if not scope_ids:
            out.append("a mutating grant needs a non-empty scope")
        if not grant.verification.checks:
            out.append("a mutating grant needs at least one verification check")
    for check in grant.verification.checks:
        if check not in known_checks:
            out.append(f"verification check {check[:64]!r} does not exist")
    if grant.tier in REPAIR_UP:
        # Contract 10: the plan exists, is valid, and is the one the grant's hash names (so it cannot change after approval).
        out += binding_problems(grant, verification_plan)
    for cred in grant.credentials:
        name = cred[5:] if cred.startswith("tool:") else None
        if name is None or name not in known_tools:
            out.append(f"credential {cred[:64]!r} is not a registered brokered tool (tool:<name>)")
    b = grant.budgets
    for label, value, cap in (("wall_s", b.wall_s, limits.wall_s), ("tool_calls", b.tool_calls, limits.tool_calls),
                              ("output_bytes", b.output_bytes, limits.output_bytes), ("retries", b.retries, limits.retries)):
        if value > cap:
            out.append(f"budget {label} {value} exceeds the site maximum {cap}")
    return out


# ── grant -> environment ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionEnv:
    env: Mapping[str, str]
    tools: tuple[str, ...]
    readable: tuple[str, ...]
    denied: tuple[str, ...]
    cwd: str
    dropped: tuple[str, ...] = field(default=())      # base variables that were NOT passed on (names only)


def _tool_names(grant: Grant, known: Collection[str]) -> tuple[str, ...]:
    names = []
    for cred in grant.credentials:
        if not cred.startswith("tool:") or cred[5:] not in known:
            raise EnvLeak(f"credential {cred[:64]!r} is not a registered brokered tool")
        names.append(cred[5:])
    return tuple(sorted(set(names)))


def _scope_paths(grant: Grant, stacks_root: str | None) -> list[str]:
    if not stacks_root:
        return []
    stacks = dict.fromkeys([*grant.scope.stacks, *(s.split("/", 1)[0] for s in grant.scope.services)])
    return [os.path.join(stacks_root, s) for s in stacks]


def build_session_env(grant: Grant, *, mission_id: str, workspace: str, worker_socket: str, stacks_root: str | None,
                      base_env: Mapping[str, str], known_tools: Collection[str], secret_values: Collection[str] = (),
                      secret_paths: Collection[str] = ()) -> SessionEnv:
    """The only description of what a session's environment holds. `base_env` is the operator's allow-list (already filtered
    by the runtime config); anything secret-looking in it, by name or by value, is dropped here as well. `readable` and `denied`
    are what `scan_session_env` looks through for a leaked secret (the stack directories and the workspace, minus PE's secrets)."""
    tools = _tool_names(grant, known_tools)
    secrets = [s for s in secret_values if s]
    env: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in base_env.items():
        bad = (not _ENV_NAME.match(key) or key.startswith("PE_") or key == "HOME" or key == "DOCKER_HOST"
               or _SECRETISH.search(key) or not isinstance(value, str) or len(value) > MAX_ENV_VALUE or "\0" in value
               or any(s in value for s in secrets))
        if bad:
            dropped.append(key)
        else:
            env[key] = value
    env.update({"HOME": workspace, "PE_MISSION_ID": mission_id, "PE_WORKER_SOCKET": worker_socket,
                "PE_TOOLS": ",".join(tools)})
    for key, value in env.items():
        if any(s in value for s in secrets):
            raise EnvLeak(f"a secret reached {key}")
    denied = sorted({os.path.normpath(p) for p in secret_paths})
    readable = [p for p in (*_scope_paths(grant, stacks_root), os.path.normpath(workspace))
                if not any(p == d or p.startswith(d.rstrip("/") + "/") for d in denied)]
    return SessionEnv(env=env, tools=tools, readable=tuple(dict.fromkeys(readable)), denied=tuple(denied),
                      cwd=workspace, dropped=tuple(sorted(dropped)))


def make_env_policy(*, stacks_root: str | None, known_tools: Collection[str],
                    secret_values: Callable[[], Collection[str]] = lambda: (),
                    secret_paths: Collection[str] = ()) -> Callable[[Grant, str, str, Mapping[str, str]], dict[str, str]]:
    """The hook `CommandRuntime` calls: the allow-listed environment goes through `build_session_env`, so a variable that
    is secret by name or by value, or a credential the grant does not name, never reaches the session. `secret_values`
    is read at call time (Core's own tool secrets)."""
    def policy(grant: Grant, mission_id: str, workspace: str, env: Mapping[str, str]) -> dict[str, str]:
        built = build_session_env(
            grant, mission_id=mission_id, workspace=workspace, worker_socket=env.get("PE_WORKER_SOCKET", ""),
            stacks_root=stacks_root, base_env=env, known_tools=known_tools,
            secret_values=list(secret_values()), secret_paths=secret_paths)
        return dict(built.env)
    return policy


def scan_session_env(se: SessionEnv, secrets: Collection[str], *, max_files: int = SCAN_MAX_FILES,
                     max_bytes: int = SCAN_MAX_BYTES) -> list[str]:
    """Where, if anywhere, a known secret is visible to the session: its env, cwd tree and readable files. A hit names
    the place, never the value. Bounded in files and bytes read; unreadable entries are skipped."""
    wanted = [s.encode() for s in secrets if s]
    hits: list[str] = []
    for key, value in se.env.items():
        if any(s.decode() in value for s in wanted) or any(s.decode() in key for s in wanted):
            hits.append(f"env:{key}")
    denied = [d.rstrip("/") for d in se.denied]
    seen = 0
    for root in dict.fromkeys([se.cwd, *se.readable]):
        stack = [root]
        while stack and seen < max_files:
            current = stack.pop()
            if any(current == d or current.startswith(d + "/") for d in denied):
                continue
            try:
                if os.path.islink(current):
                    continue
                if os.path.isdir(current):
                    stack.extend(os.path.join(current, n) for n in sorted(os.listdir(current)))
                    continue
                seen += 1
                with open(current, "rb") as handle:
                    data = handle.read(max_bytes)
            except OSError:
                continue
            if any(s in data for s in wanted) or any(s in current.encode() for s in wanted):
                hits.append(f"file:{current}")
    return hits


__all__ = ["EnvLeak", "Inventory", "SessionEnv", "SiteLimits", "build_session_env", "grant_hash", "make_env_policy",
           "scan_session_env", "validate_grant"]
