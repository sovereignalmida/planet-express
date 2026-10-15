"""Protected domains (brief 5.3, contract 4/9): concrete resources a mission must never touch.

The built-in default set covers EVERY 5.3 domain. The site config adds to it (`missions.protected`); setup proposes
more from discovery. It cannot be emptied silently: removing a default needs a `ProtectedOverride` that names the
resource, a reason and an operator, and each one is recorded. Matching is `resources.classify` (protected wins).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .resources import BadResource, canon, contains

REQUIRED_DOMAINS = ("ingress", "dns", "credentials", "firewall", "backup", "sudoers", "docker_daemon",
                    "data_volumes", "packages", "reboot", "publishing", "vcs", "cross_host")

# Concrete defaults, bare ids (the host is added at build time). `data_volumes` and `cross_host` are filled from
# discovery and the operator's named systems; they have no host-independent default.
DEFAULT_PROTECTED: dict[str, tuple[str, ...]] = {
    "ingress": ("path:/etc/traefik", "path:/etc/nginx", "path:/etc/caddy", "detector:ingress_routes"),
    "dns": ("path:/etc/hosts", "path:/etc/resolv.conf", "path:/etc/dnsmasq.d", "path:/etc/dnsmasq.conf"),
    "credentials": ("path:/etc/shadow", "path:/etc/gshadow", "path:/etc/passwd", "path:/etc/group",
                    "path:/etc/ssh/sshd_config", "path:/root/.ssh", "path:/root/.docker/config.json"),
    "firewall": ("detector:firewall", "path:/etc/nftables.conf", "path:/etc/iptables", "path:/etc/ufw"),
    "backup": ("path:/etc/cron.d", "path:/etc/crontab", "path:/var/spool/cron"),
    "sudoers": ("path:/etc/sudoers", "path:/etc/sudoers.d"),
    "docker_daemon": ("path:/etc/docker/daemon.json", "path:/var/run/docker.sock", "path:/run/docker.sock"),
    "packages": ("detector:packages", "path:/var/lib/dpkg", "path:/usr/lib/node_modules",
                 "path:/usr/local/lib/node_modules", "path:/usr/lib/python3/dist-packages"),
    "reboot": ("detector:boot", "unit:reboot.target", "unit:poweroff.target", "unit:halt.target",
               "unit:kexec.target", "unit:shutdown.target"),
    "publishing": ("detector:listeners", "detector:git_remotes"),
    "vcs": ("detector:vcs",),
}


class OverrideRefused(ValueError):
    """An override that would silently weaken the protected set."""


@dataclass(frozen=True)
class ProtectedOverride:
    resource: str
    reason: str
    operator: str
    at: int


@dataclass(frozen=True)
class Discovery:
    """What setup found on this host. Everything is a concrete path or name."""
    stacks_root: str
    host: str = "local"
    storage_mounts: Sequence[str] = ()           # persistent data / storage mount points (not the stacks)
    volumes: Sequence[str] = ()                  # named-volume host paths
    ingress: Sequence[str] = ()
    dns: Sequence[str] = ()
    credentials: Sequence[str] = ()
    firewall: Sequence[str] = ()
    backup: Sequence[str] = ()
    package_paths: Sequence[str] = ()
    live_trees: Sequence[str] = ()               # working trees watched for destructive VCS and pushes
    cross_host: Sequence[tuple[str, str | None]] = ()   # (system name, a readable config path or None)


@dataclass(frozen=True)
class ProtectedSet:
    host: str
    resources: tuple[str, ...]
    domains: Mapping[str, tuple[str, ...]]
    overrides: tuple[ProtectedOverride, ...] = ()
    carved_out: tuple[str, ...] = ()             # discovered resources left out because they hold the stacks root
    live_trees: tuple[str, ...] = ()
    cross_host_paths: Mapping[str, str] = field(default_factory=dict)

    def covers(self, resource: str) -> bool:
        return any(contains(p, resource) for p in self.resources)


def build_protected(*, site: Sequence[str], discovery: Discovery, overrides: Sequence[ProtectedOverride] = (),
                    record: Callable[[dict], None] | None = None) -> ProtectedSet:
    host = discovery.host
    domains: dict[str, list[str]] = {d: [] for d in REQUIRED_DOMAINS}

    def add(domain: str, rid: str) -> None:
        full = canon(rid, host)
        if full not in domains[domain]:
            domains[domain].append(full)

    for domain, ids in DEFAULT_PROTECTED.items():
        for rid in ids:
            add(domain, rid)
    for kind, domain in (("ingress", "ingress"), ("dns", "dns"), ("credentials", "credentials"),
                         ("firewall", "firewall"), ("backup", "backup"), ("package_paths", "packages")):
        for path in getattr(discovery, kind):
            add(domain, f"path:{path}")
    carved: list[str] = []
    stacks = canon(f"path:{discovery.stacks_root}", host)
    for mount in discovery.storage_mounts:
        rid = canon(f"mount:{mount}", host)
        if contains(rid, stacks):
            carved.append(rid)           # protecting it would forbid the stacks themselves
        else:
            add("data_volumes", f"mount:{mount}")
    for volume in discovery.volumes:
        rid = canon(f"path:{volume}", host)
        if contains(rid, stacks):
            carved.append(rid)
        else:
            add("data_volumes", f"path:{volume}")
    cross: dict[str, str] = {}
    for name, path in discovery.cross_host:
        add("cross_host", f"system:{name}")
        if path:
            canon(f"path:{path}", host)
            cross[name] = path
    trees = tuple(discovery.live_trees)
    for tree in trees:
        canon(f"path:{tree}", host)
    extra: list[str] = []
    for item in site:
        try:
            extra.append(canon(item, host))
        except BadResource as exc:
            raise ValueError(f"missions.protected: {exc}") from None

    resources = {r for rs in domains.values() for r in rs} | set(extra)
    kept = tuple(overrides)
    for ov in kept:
        try:
            target = canon(ov.resource, host)
        except BadResource as exc:
            raise OverrideRefused(str(exc)) from None
        if not ov.reason.strip() or not ov.operator.startswith("operator:") or len(ov.operator) <= 9:
            raise OverrideRefused("an override needs a reason and an operator:<name>")
        if target not in resources:
            raise OverrideRefused(f"{target} is not in the protected set")
        resources.discard(target)
        for rs in domains.values():
            if target in rs:
                rs.remove(target)
        if record is not None:
            record({"override": target, "reason": ov.reason[:512], "operator": ov.operator, "at": ov.at})
    return ProtectedSet(host=host, resources=tuple(sorted(resources)),
                        domains={d: tuple(rs) for d, rs in domains.items()}, overrides=kept,
                        carved_out=tuple(carved), live_trees=trees, cross_host_paths=cross)


__all__ = ["DEFAULT_PROTECTED", "REQUIRED_DOMAINS", "Discovery", "OverrideRefused", "ProtectedOverride",
           "ProtectedSet", "build_protected"]
