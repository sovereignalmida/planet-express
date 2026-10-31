"""Discovery of the protected set's host-specific inputs (G4-F1).

`build_protected` only knows what it is fed. This module feeds it from config and the live host, and says so when it
cannot: a REQUIRED domain that resolves empty, or any discovery step that failed, becomes a *failure*. The runner then
withholds the Repair controls and records why (observe/propose keep working). Anything found is still protected; a
failure never shrinks the set.

What "required" means. A domain is required when the host shows evidence that the thing exists; then it must resolve to
at least one concrete path or the host is not trusted with Repair:
  data_volumes  `docker volume ls` lists a volume (each must resolve to a host path), or the mount table is unreadable
  ingress       a traefik/caddy/haproxy/nginx-proxy container exists, or a stack directory is named for one
  dns           an adguard/pihole/unbound/dnsmasq/coredns container exists, or a stack directory is named for one
  backup        backup tooling config exists on disk, a backup container exists, or `checkpoint.backup_sources` is set
  credentials   the stacks tree can be scanned (finding no env or secrets files is fine)
A discovery command that fails (docker down, refused, output truncated) fails every domain that depends on it. A host
genuinely without one of these needs nothing; a host where the evidence cannot be read acknowledges the domain in
`missions.protected_absent`, which skips the requirement (reported) and never removes a built-in default.
"""

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

Runner = Callable[[list[str], int], tuple[int, str, str]]
BoundedRunner = Callable[[list[str], int, int], tuple[int, str, str, bool]]

ACKNOWLEDGEABLE = ("ingress", "dns", "credentials", "backup", "data_volumes")
INGRESS_TOKENS = ("traefik", "caddy", "haproxy", "nginx-proxy", "nginx-proxy-manager", "nginxproxymanager")
DNS_TOKENS = ("adguard", "pihole", "unbound", "dnsmasq", "coredns")
BACKUP_TOKENS = ("borg", "restic", "duplicati", "kopia")
BACKUP_CONFIG_PATHS = ("/etc/borgmatic", "/etc/borgmatic.d", "/root/.config/borg", "/root/.config/borgmatic",
                       "/etc/restic", "/root/.config/restic")
# Every persistent mount is data unless it is a pseudo/virtual filesystem or boot/system plumbing. A mount that holds the
# stacks root is carved out later by `build_protected`; anything else (including an unlisted fs type) is protected.
PSEUDO_FS = frozenset({"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "ramfs", "overlay", "squashfs", "securityfs",
                       "debugfs", "tracefs", "configfs", "fusectl", "pstore", "bpf", "autofs", "mqueue", "hugetlbfs",
                       "binfmt_misc", "efivarfs", "nsfs", "rpc_pipefs", "selinuxfs", "fuse.portal", "fuse.gvfsd-fuse",
                       "fuse.lxcfs", "aufs"})
SYSTEM_MOUNT_PREFIXES = ("/boot", "/efi", "/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker/",
                         "/var/lib/containerd", "/var/lib/kubelet")
SECRET_DIRS = frozenset({"secrets", ".secrets"})
VENDORED_DIRS = frozenset({"node_modules", "bower_components", "site-packages", "dist-packages", ".git"})   # third-party trees, VCS internals
MAX_OUTPUT = 1024 * 1024
TIMEOUT = 20


@dataclass(frozen=True)
class DiscoveredInputs:
    storage_mounts: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    ingress: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    backup: tuple[str, ...] = ()
    failures: dict[str, str] = field(default_factory=dict)       # domain -> why it could not be established
    acknowledged: tuple[str, ...] = ()                           # domains the operator named in protected_absent
    blind: tuple[str, ...] = ()                                  # directories Core could not read: protected whole (see `default_scan`)

    def kwargs(self) -> dict[str, tuple[str, ...]]:
        return {"storage_mounts": self.storage_mounts, "volumes": self.volumes, "ingress": self.ingress,
                "dns": self.dns, "credentials": self.credentials, "backup": self.backup}


MAX_SCAN_DEPTH = 32                 # a stack's vendored source tree (node_modules) runs past 10; exceeding MAX_SCAN_ENTRIES still fails
MAX_SCAN_ENTRIES = 50_000


def default_scan(root: str, depth: int = MAX_SCAN_DEPTH, limit: int = MAX_SCAN_ENTRIES,
                 blind: list[str] | None = None) -> list[tuple[str, bool]]:
    """(path, is_dir) for every entry under `root` (symlinks not followed). Raises OSError when `root` is unreadable, or when the
    tree is deeper or larger than the bounds: an incomplete scan is a failure, not a result.

    A directory BELOW `root` that Core may not read (a container's data directory, owned by another account) is the usual case on
    a real host, and it need not sink the scan: with `blind` given, its path is appended there and the walk goes on. The caller
    protects every blind directory whole, because what Core cannot read it cannot prove free of secrets. Without `blind` it
    raises, as before."""
    out: list[tuple[str, bool]] = []

    def walk(directory: str, level: int) -> None:
        try:
            it = os.scandir(directory)
        except PermissionError:
            if blind is None or level == 1:
                raise
            blind.append(directory)
            return
        with it:
            for entry in sorted(it, key=lambda e: e.name):
                is_dir = entry.is_dir(follow_symlinks=False)
                out.append((entry.path, is_dir))
                if len(out) > limit:
                    raise OSError(f"{root} holds more than {limit} entries")
                if is_dir:
                    if level >= depth:
                        raise OSError(f"{entry.path} is deeper than {depth} levels")
                    walk(entry.path, level + 1)

    walk(root, 1)
    return out


def _default_read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read(MAX_OUTPUT + 1)
    if len(text) > MAX_OUTPUT:
        raise OSError(f"{path} exceeds {MAX_OUTPUT} bytes")          # a prefix of the mount table is not the table
    return text


def _default_exists(path: str) -> bool:
    """False only when the path is genuinely absent. A permission failure (an untraversable parent) is not absence."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _unescape(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """(mount point, fs type) per line of /proc/self/mountinfo. A malformed line raises ValueError."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        left, sep, right = line.partition(" - ")
        parts = left.split(" ")
        fstype = right.split(" ")[0]
        if not sep or len(parts) < 6 or not fstype:
            raise ValueError(f"unparseable mountinfo line: {line[:80]!r}")
        out.append((_unescape(parts[4]), fstype))
    return out


def _data_mounts(table: Sequence[tuple[str, str]]) -> list[str]:
    found = []
    for point, fstype in table:
        if point == "/" or not point.startswith("/") or fstype in PSEUDO_FS or fstype.startswith("cgroup"):
            continue
        if any(point == p.rstrip("/") or point.startswith(p.rstrip("/") + "/") for p in SYSTEM_MOUNT_PREFIXES):
            continue
        found.append(point)
    return sorted(set(found))


def _vendored(path: str, stacks_root: str) -> bool:
    """Is `path` inside a third-party package tree (or a .git directory) below the stacks root?"""
    return any(part.lower() in VENDORED_DIRS for part in os.path.relpath(path, stacks_root).split(os.sep)[:-1])


def _matches(tokens: Sequence[str], name: str, image: str) -> bool:
    base = image.lower().rsplit("/", 1)[-1].split(":", 1)[0]
    return any(t in base or t in name.lower() for t in tokens)


def discover_protected_inputs(
    cfg, *, stacks_root: str, runner: Runner | None = None, bounded: BoundedRunner | None = None,
    read_text: Callable[[str], str] | None = None, scan: Callable[[str], list[tuple[str, bool]]] | None = None,
    exists: Callable[[str], bool] | None = None, mountinfo: str = "/proc/self/mountinfo",
) -> DiscoveredInputs:
    """A step that blows up is recorded as a failure of the domains that depend on it; nothing here raises on a host fault."""
    if bounded is None:
        from planet_express.missions.checkpoint.provider import default_bounded_runner
        bounded = default_bounded_runner
    read_text = read_text or _default_read
    blind: list[str] = []
    scan = scan or (lambda root: default_scan(root, blind=blind))
    exists = exists or _default_exists
    failures: dict[str, str] = {}
    mounts: list[str] = []
    volumes: list[str] = []
    paths: dict[str, list[str]] = {"ingress": [], "dns": [], "credentials": [], "backup": []}
    evidence = {"ingress": False, "dns": False, "backup": False}

    def fail(domain: str, why: str) -> None:
        failures.setdefault(domain, why)

    def docker(argv: list[str]) -> str:
        try:
            rc, out, _err, truncated = bounded(["docker", *argv], TIMEOUT, MAX_OUTPUT)
        except Exception as exc:
            raise RuntimeError(f"docker {argv[0]}: {type(exc).__name__}") from None
        if rc != 0:
            raise RuntimeError(f"docker {argv[0]} exited {rc}")
        if truncated:
            raise RuntimeError(f"docker {argv[0]} output exceeded {MAX_OUTPUT} bytes")
        return out

    try:
        mounts = _data_mounts(parse_mountinfo(read_text(mountinfo)))
    except (OSError, ValueError) as exc:
        fail("data_volumes", f"mount table unreadable: {type(exc).__name__}")

    try:
        names = docker(["volume", "ls", "-q"]).split()
        if names:
            raw = docker(["volume", "inspect", "--format", "{{.Name}}\t{{.Mountpoint}}", *names])
            resolved = {}
            for line in raw.splitlines():
                name, _, point = line.partition("\t")
                if name and point.startswith("/"):
                    resolved[name] = point
            volumes = sorted(set(resolved.values()))
            missing = [n for n in names if n not in resolved]
            if missing:
                fail("data_volumes", f"{len(missing)} docker volume(s) did not resolve to a host path")
    except RuntimeError as exc:
        fail("data_volumes", str(exc))

    families = (("ingress", INGRESS_TOKENS), ("dns", DNS_TOKENS), ("backup", BACKUP_TOKENS))
    try:
        for line in docker(["ps", "-a", "--format", "{{.Names}}\t{{.Image}}"]).splitlines():
            name, _, image = line.partition("\t")
            for domain, tokens in families:
                if not name or not _matches(tokens, name, image):
                    continue
                evidence[domain] = True
                try:
                    listed = json.loads(docker(["inspect", "--format", "{{json .Mounts}}", name]) or "[]")
                except (RuntimeError, ValueError) as exc:
                    fail(domain, f"container {name}: {exc}")
                    continue
                for m in listed or []:
                    src = m.get("Source", "") if isinstance(m, dict) else ""
                    if m.get("Type") == "bind" and src.startswith("/") and not src.endswith("docker.sock"):
                        paths[domain].append(src)
    except RuntimeError as exc:
        for domain in ("ingress", "dns", "backup"):
            fail(domain, str(exc))

    try:
        entries = scan(stacks_root)
    except OSError as exc:
        bound = exc.errno is None                       # raised by a scan bound, not by the OS
        fail("credentials", f"stacks tree not fully scanned: {str(exc)[:200]}" if bound
             else f"stacks tree unreadable: {type(exc).__name__}")
        entries = []
    paths["credentials"].extend(blind)            # unreadable below the root: protected whole, never searched around
    for path, is_dir in entries:
        base = os.path.basename(path).lower()
        if is_dir:
            for domain, tokens in families:
                if any(t in base for t in tokens) and not _vendored(path, stacks_root):   # a package named `cyborg` is not a backup
                    evidence[domain] = True
                    paths[domain].append(path)
            if base in SECRET_DIRS:
                paths["credentials"].append(path)
        elif base == ".env" or base.endswith(".env") or base.startswith(".env."):
            paths["credentials"].append(path)

    for candidate in BACKUP_CONFIG_PATHS:
        try:
            if exists(candidate):
                evidence["backup"] = True
                paths["backup"].append(candidate)
        except OSError:
            fail("backup", f"cannot test {candidate}")
    for source in getattr(getattr(cfg, "checkpoint", None), "backup_sources", ()) or ():
        evidence["backup"] = True
        paths["backup"].append(source.marker)          # the freshness marker: a mission must not be able to touch it

    result = {d: tuple(sorted(set(v))) for d, v in paths.items()}
    for domain in ("ingress", "dns", "backup"):
        if evidence[domain] and not result[domain]:
            fail(domain, f"a {domain} component exists but no path of it was discovered")
    acknowledged = tuple(d for d in getattr(cfg, "protected_absent", ()) if d in ACKNOWLEDGEABLE)
    for domain in acknowledged:
        failures.pop(domain, None)
    return DiscoveredInputs(
        storage_mounts=tuple(mounts), volumes=tuple(volumes), ingress=result["ingress"], dns=result["dns"],
        credentials=result["credentials"], backup=result["backup"], failures=failures, acknowledged=acknowledged,
        blind=tuple(sorted(set(blind))))


__all__ = ["ACKNOWLEDGEABLE", "DiscoveredInputs", "discover_protected_inputs", "parse_mountinfo"]
