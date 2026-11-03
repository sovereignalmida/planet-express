"""Grant validation (contract 4) and the grant-to-environment builder (brief 6.3: default deny).

`validate_grant` is Core's check that a grant is sound BEFORE an operator is asked to approve it. It returns every
reason it refuses, never raises for a bad grant, and refuses on anything it cannot evaluate.

`build_session_env` returns exactly what a session may see: the environment variables, the brokered tool names and the
readable paths. Nothing is inherited: a variable the builder did not put there is absent, a credential the grant does
not name (as `tool:<name>`) has no route into the session, and a value that equals a known secret is dropped whatever its
name. The grant hash is `Grant.hash()` (RFC 8785 canonical JSON, `canonical.py`); nothing here ever edits a grant.
"""

import os
import re
import stat
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .models import GRANT_JSON_MAX, MAX_PROTECTED, Grant, VerificationPlan
from .protected import ProtectedSet
from .resources import BadResource, canon, contains, split
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
    mounts: Sequence[str] = ()
    path_roots: Sequence[str] = ()        # a scope path must be at or below one of these


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


def with_site_protected(grant: Grant, protected: ProtectedSet) -> Grant:
    """Copy the site's protected set into the grant (it is hashed, so approval binds to it). A set too large to store
    with the grant is refused here, in words, rather than as a schema error from the store."""
    count = len(protected.resources)
    if count > MAX_PROTECTED:
        raise ValueError(f"the site protects {count} resources, more than the {MAX_PROTECTED} a grant can carry; "
                         "protect a parent path instead of each volume, or prune unused Docker volumes")
    out = Grant.model_validate({**grant.model_dump(mode="json"), "protected": {"resources": list(protected.resources)}})
    size = len(out.canonical().encode("utf-8"))   # bytes: at least what the store counts (characters), and what RPC frames count
    if size > GRANT_JSON_MAX:
        raise ValueError(f"the site's {count} protected resources make the grant {size} bytes, over the {GRANT_JSON_MAX} "
                         "a mission can store; protect a parent path instead of each volume, or prune unused Docker volumes")
    return out


LIFECYCLE_WALK_LIMIT = 50_000      # entries looked at per grant before the answer is "could not be checked"


@dataclass(frozen=True)
class SessionPrincipals:
    """Every identity a session process can hold: a uid in the launcher's range, the private gid of the same number, and the
    configured session groups (missions.session_groups)."""
    uid_min: int
    uid_max: int
    gids: frozenset[int] = frozenset()

    def uid(self, uid: int) -> bool:
        return self.uid_min <= uid <= self.uid_max

    def gid(self, gid: int) -> bool:
        return self.uid_min <= gid <= self.uid_max or gid in self.gids


_ACL_XATTR = "system.posix_acl_access"
_ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK = 0x02, 0x04, 0x08, 0x10


def _acl(path: str) -> list[tuple[int, int, int]] | None:
    """The file's access ACL as (tag, perm, id) entries, or None when it has none beyond its mode bits."""
    import errno
    import struct
    try:
        raw = os.getxattr(path, _ACL_XATTR, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, getattr(errno, "ENOATTR", errno.ENODATA)):
            return None
        raise
    if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack_from("<I", raw)[0] != 2:
        raise OSError(f"unreadable ACL on {path[:120]}")
    return [struct.unpack_from("<HHI", raw, off) for off in range(4, len(raw), 8)]


def _session_may(path: str, st: os.stat_result, who: SessionPrincipals, bit: int) -> bool:
    """Whether ANY session identity has `bit` (2 write, 1 traverse). Sessions are many uids, each falling in a different class
    (one may own the file while the next gets the group or other bits), so this is the union of every class a session could
    land in: the owner bits if a session uid owns it, a named ACL user's entry, the group class when a session gid matches it,
    and the other bits (some session uid is always neither owner nor named). Over-counting only keeps the floor (Codex, rc13)."""
    mode = st.st_mode
    if mode & bit:
        return True
    if who.uid(st.st_uid) and (mode >> 6) & bit:
        return True
    entries = _acl(path)
    if entries is None:
        return bool(who.gid(st.st_gid) and (mode >> 3) & bit)
    mask = next((perm for tag, perm, _id in entries if tag == _ACL_MASK), 7)
    for tag, perm, ident in entries:
        if not perm & mask & bit:
            continue
        if tag == _ACL_USER and who.uid(ident):
            return True
        if (tag == _ACL_GROUP_OBJ and who.gid(st.st_gid)) or (tag == _ACL_GROUP and who.gid(ident)):
            return True
    return False


def lifecycle_write_reach(grant: Grant, stacks_root: str | None, principals: SessionPrincipals | None, *,
                          limit: int = LIFECYCLE_WALK_LIMIT) -> str | None:
    """None when no session identity can write anything in the grant's stack directories. Otherwise why not, in words.

    A `docker: lifecycle` grant skips the backup floor because the proxy and the typed steps can only restart containers; the
    session still has its own shell, so the waiver holds only if that shell cannot write in scope either. Access is worked out
    for the session's actual identities (its uid range, the private gids, the log groups) through owner, group and ACL entries,
    not just the 'other' bits. A directory no session identity can traverse stops the walk; anything one could write, or a tree
    too large or unreadable to check, or an unknown identity, means the floor applies as usual (Codex review, rc13)."""
    if principals is None:
        return "the session identities are not known, so what a session could write cannot be checked"
    if not stacks_root:
        return "the stacks root is not known, so what the session could write cannot be checked"
    stacks = sorted({*grant.scope.stacks, *(s.split("/", 1)[0] for s in grant.scope.services)})
    seen = 0
    for name in stacks:
        top = os.path.join(stacks_root, name)
        try:
            if os.path.islink(top):
                return f"{top[:160]} is a symbolic link, so what it reaches cannot be bounded"
            real_top = os.path.realpath(top)
            reachable = True
            parts = os.path.normpath(top).split(os.sep)
            for i in range(1, len(parts)):
                ancestor = os.sep.join(parts[:i]) or os.sep
                if not os.path.lexists(ancestor):
                    reachable = False                            # nothing exists down there to write
                    break
                if not _session_may(ancestor, os.lstat(ancestor), principals, 1):
                    reachable = False
                    break
            if not reachable or not os.path.lexists(top):
                continue
            pending = [top]
            while pending:
                here = pending.pop()
                st = os.lstat(here)
                if not _session_may(here, st, principals, 1):
                    continue                                     # no session identity can enter: nothing below is in reach
                if _session_may(here, st, principals, 2):
                    return f"{here[:160]} can be written by a session"
                with os.scandir(here) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > limit:
                            return f"the stack directories hold more than {limit} entries, too many to check"
                        est = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(est.st_mode):
                            # A session's shell follows links. One that stays inside this stack directory reaches only what the
                            # walk checks anyway; one that leads anywhere else (or nowhere yet) is not bounded by this walk.
                            target = os.path.realpath(entry.path)
                            if not (target == real_top or target.startswith(real_top + os.sep)) or not os.path.lexists(target):
                                return f"{entry.path[:160]} is a link that leads outside its stack directory"
                            continue
                        if stat.S_ISDIR(est.st_mode):
                            pending.append(entry.path)
                        elif _session_may(entry.path, est, principals, 2):
                            return f"{entry.path[:160]} can be written by a session"
        except OSError as exc:
            return f"{top[:160]} could not be checked ({type(exc).__name__})"
    return None


def _under_root(path: str, roots: Iterable[str]) -> bool:
    return any(path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in roots)


def validate_grant(grant: Grant, *, inventory: Inventory, protected: ProtectedSet, known_checks: Collection[str],
                   known_tools: Collection[str], limits: SiteLimits, stacks_root: str | None = None,
                   verification_plan: VerificationPlan | None = None,
                   session_principals: SessionPrincipals | None = None) -> list[str]:
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
    for mount in scope.mounts:
        if mount not in inventory.mounts:
            out.append(f"scope mount {mount!r} is not in the discovered inventory")
    for path in scope.paths:
        if not path.startswith("/") or ".." in path.split("/") or not _under_root(os.path.normpath(path), inventory.path_roots):
            out.append(f"scope path {path[:80]!r} is not under a root in the discovered inventory")
        elif os.path.normpath(path) != path and path != "/":
            # Leases refuse a path that is not already normalised, so a grant that carried one would be approved and then
            # never start. Say so now.
            out.append(f"scope path {path[:80]!r} is not normalised (no trailing slash, '//' or '.' segments)")
    try:
        scope_ids = [canon(r) for r in grant_resources(grant)]
    except BadResource as exc:
        return [*out, f"scope is malformed: {exc}"]

    site = set(protected.resources)
    if not site <= set(grant.protected.resources):
        out.append("grant.protected does not carry the site's protected resources")
    effective: list[str] = []
    for resource in {*protected.resources, *grant.protected.resources}:
        try:
            effective.append(canon(resource))
        except BadResource:
            out.append(f"grant.protected has a malformed resource {resource[:80]!r}")
    if grant.tier != "critical":
        for sid in scope_ids:
            # An item INSIDE a protected resource is refused. An item that merely CONTAINS one (a stack with an ingress
            # container) is allowed: the protected resource is carved out of scope because protected wins at match time.
            for pid in effective:
                if contains(pid, sid, stacks_root=stacks_root):
                    out.append(f"scope {sid} is protected ({pid}); only a critical mission may include it")
                    break
        for rule in grant.sudo.rules:
            unit = canon(f"unit:{rule.unit}") if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", rule.unit) else None
            if unit is None or any(contains(pid, unit) for pid in effective):
                out.append(f"sudo rule for unit {rule.unit[:80]!r} touches a protected resource")

    if grant.tier in REPAIR_UP:
        if not scope_ids:
            out.append("a mutating grant needs a non-empty scope")
        if not grant.verification.checks:
            out.append("a mutating grant needs at least one verification check")
        # A lifecycle grant can only start, stop and restart containers (the proxy and the typed-step gate enforce it). When the
        # session's own account cannot write in its stacks either, no data it could lose is in reach and the floor does not apply.
        if not (grant.backup.covered or grant.backup.acknowledged_no_backup):
            reach = lifecycle_write_reach(grant, stacks_root, session_principals) if grant.docker == "lifecycle" \
                else "not restart-only"
            if grant.docker == "lifecycle" and reach is not None:
                out.append(f"backup floor: a restart-only repair skips the backup only when nothing in its stacks is writable "
                           f"by the session, and {reach}")
            elif reach is not None:
                out.append("backup floor: the scope is not covered by a recent backup and no-backup is not acknowledged")
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
    paths = [os.path.normpath(p) for p in (*grant.scope.paths, *grant.scope.mounts)]
    if stacks_root:
        paths += [os.path.join(stacks_root, s) for s in grant.scope.stacks]
    return paths


def _path_resource(path: str) -> str:
    return canon(f"path:{path}")


def build_session_env(grant: Grant, *, mission_id: str, workspace: str, worker_socket: str, protected: ProtectedSet,
                      stacks_root: str | None, base_env: Mapping[str, str], known_tools: Collection[str],
                      secret_values: Collection[str] = (), secret_paths: Collection[str] = ()) -> SessionEnv:
    """The only description of what a session may see. `base_env` is the operator's allow-list (already filtered
    by the runtime config); anything secret-looking in it, by name or by value, is dropped here as well."""
    tools = _tool_names(grant, known_tools)
    secrets = [s for s in secret_values if s]
    env: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in base_env.items():
        bad = (not _ENV_NAME.match(key) or key.startswith("PE_") or key == "HOME" or _SECRETISH.search(key)
               or not isinstance(value, str) or len(value) > MAX_ENV_VALUE or "\0" in value
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

    secret_ids = [_path_resource(os.path.normpath(p)) for p in secret_paths]
    prot_paths = [r for r in protected.resources if split(r)[1] in ("path", "mount")]
    denied = sorted({split(r)[2] for r in prot_paths} | {os.path.normpath(p) for p in secret_paths})
    readable: list[str] = []
    for path in [*_scope_paths(grant, stacks_root), os.path.normpath(workspace)]:
        rid = _path_resource(path)
        # a scope path inside a protected or secret path is not readable; one that merely contains them is, minus them
        if any(contains(p, rid) for p in (*prot_paths, *secret_ids)) and os.path.normpath(path) != os.path.normpath(workspace):
            continue
        readable.append(path)
    return SessionEnv(env=env, tools=tools, readable=tuple(dict.fromkeys(readable)), denied=tuple(denied),
                      cwd=workspace, dropped=tuple(sorted(dropped)))


def make_env_policy(*, protected: ProtectedSet, stacks_root: str | None, known_tools: Collection[str],
                    secret_values: Callable[[], Collection[str]] = lambda: (),
                    secret_paths: Collection[str] = ()) -> Callable[[Grant, str, str, Mapping[str, str]], dict[str, str]]:
    """The hook `CommandRuntime` calls: the allow-listed environment goes through `build_session_env`, so a variable that
    is secret by name or by value, or a credential the grant does not name, never reaches the session. `secret_values`
    is read at call time (Core's own tool secrets)."""
    def policy(grant: Grant, mission_id: str, workspace: str, env: Mapping[str, str]) -> dict[str, str]:
        built = build_session_env(
            grant, mission_id=mission_id, workspace=workspace, worker_socket=env.get("PE_WORKER_SOCKET", ""),
            protected=protected, stacks_root=stacks_root, base_env=env, known_tools=known_tools,
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


__all__ = ["SessionPrincipals", "lifecycle_write_reach", "EnvLeak", "Inventory", "SessionEnv", "SiteLimits", "build_session_env", "grant_hash", "make_env_policy", "scan_session_env",
           "validate_grant", "with_site_protected"]
