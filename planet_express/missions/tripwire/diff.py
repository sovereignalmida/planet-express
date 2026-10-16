"""The tripwire diff (contract 9): baseline snapshot vs current snapshot, one verdict per change.

Verdicts:
    allowed              a change to something inside the grant's scope, of a kind a repair makes
    unexpected_in_scope  inside the scope but reaching outside it or of a kind no repair needs (counted against the budgets)
    protected            touches a protected resource: an immediate pause
    out_of_scope         touches something outside the grant that is not protected either: an immediate pause too
                         (mission-control 6.2: "a protected-domain change or out-of-scope change pauses")

Matching uses `resources.classify` (protected wins, containment per contract 2.3). Text fields come from the host
(container names, paths) and are DATA: they are sanitised and bounded here and never interpreted.
"""

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from planet_express.core.redact import redact
from planet_express.missions.resources import BadResource, canon, classify

Verdict = Literal["allowed", "unexpected_in_scope", "protected", "out_of_scope"]
VOLUME_SHRINK_SIZE = 0.10
VOLUME_SHRINK_ENTRIES = 0.05
MAX_DETAIL = 200
_PSEUDO_FS = frozenset({
    "overlay", "proc", "sysfs", "devpts", "tmpfs", "cgroup", "cgroup2", "mqueue", "nsfs", "shm", "devtmpfs",
    "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "pstore", "bpf", "autofs", "hugetlbfs",
    "binfmt_misc", "ramfs", "rpc_pipefs", "efivarfs", "squashfs"})
_DOCKER_PREFIXES = ("/var/lib/docker/", "/run/docker/", "/var/lib/containerd/", "/run/containerd/",
                    "/var/lib/kubelet/", "/run/user/", "/snap/")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
SEVERITY = {"allowed": 0, "unexpected_in_scope": 1, "out_of_scope": 2, "protected": 3}


def clean(text: Any, limit: int = MAX_DETAIL) -> str:
    """Control characters out, secrets redacted, length bounded: every host-derived string goes through here."""
    return _CTRL.sub("?", redact(str(text)))[:limit]


@dataclass(frozen=True)
class Change:
    signal: str
    resource: str
    kind: str
    verdict: Verdict
    detail: str = ""

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.signal}\0{self.resource}\0{self.kind}\0{self.detail}".encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, str]:
        return {"signal": self.signal, "resource": clean(self.resource), "kind": self.kind,
                "verdict": self.verdict, "detail": clean(self.detail), "change_id": self.key}


@dataclass(frozen=True)
class DiffContext:
    scope: tuple[str, ...]
    protected: tuple[str, ...]
    stacks_root: str | None = None
    host: str = "local"

    def cls(self, resource: str) -> str:
        return classify(resource, self.scope, self.protected, stacks_root=self.stacks_root)

    def verdict(self, resource: str, inside: Verdict = "allowed") -> Verdict:
        got = self.cls(resource)
        return "protected" if got == "protected" else "out_of_scope" if got == "out_of_scope" else inside

    def rid(self, text: str) -> str | None:
        try:
            return canon(text, self.host)
        except BadResource:
            return None


def _is_public(addr: str) -> bool:
    host = addr.rsplit(":", 1)[0].strip("[]").split("%", 1)[0]
    if host in ("*", ""):
        return True
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True                       # unreadable: treat as public, which is the cautious reading


def _mount_target(line: str) -> tuple[str, str] | None:
    parts = line.split()
    if len(parts) < 3:
        return None
    target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
    return target, parts[2]


def _storage_mounts(lines: Iterable[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        parsed = _mount_target(line)
        if not parsed:
            continue
        target, fstype = parsed
        if fstype in _PSEUDO_FS or target == "/" or target.startswith(_DOCKER_PREFIXES):
            continue
        out[target] = line
    return out


_PROPAGATION = frozenset({"shared", "slave", "private", "unbindable", "rshared", "rslave", "rprivate"})


def _mount_state(line: str) -> tuple[str, str, str, tuple[str, ...]]:
    """(device, fstype, ro|rw, propagation words): what a remount can change that matters."""
    parts = line.split()
    opts = parts[3].split(",") if len(parts) > 3 else []
    mode = "ro" if "ro" in opts else "rw"
    words = tuple(sorted(w for w in (*opts, *(x.split(":")[0] for x in parts[4:])) if w in _PROPAGATION))
    return parts[0], parts[2] if len(parts) > 2 else "", mode, words


def _containers(base: dict, cur: dict, ctx: DiffContext) -> list[Change]:
    out: list[Change] = []
    routes_res = ctx.rid("detector:ingress_routes")
    for name in sorted(set(base) | set(cur)):
        b, c = base.get(name), cur.get(name)
        res = (c or b)["resource"]
        label = clean(name, 80)
        added_container = b is None and c is not None
        if b is None or c is None:
            out.append(Change("containers", res, "container_added" if b is None else "container_removed",
                              ctx.verdict(res), label))
            if b is None:
                b = {"image": c["image"], "mounts": [], "networks": [], "labels_hash": "", "routes": []}
            else:
                continue
        inside = ctx.cls(res) == "in_scope"
        if b["image"] != c["image"]:
            out.append(Change("containers", res, "image_changed", ctx.verdict(res), label))
        added_routes = sorted(set(c["routes"]) - set(b["routes"]))
        if added_routes and routes_res:
            out.append(Change("containers", routes_res, "ingress_route_added", "protected",
                              f"{label}: {clean(added_routes[0], 120)}"))
        elif b["labels_hash"] != c["labels_hash"] and b["labels_hash"]:
            out.append(Change("containers", res, "labels_changed", ctx.verdict(res), label))
        for net in sorted(set(c["networks"]) - set(b["networks"])):
            nres = ctx.rid(f"network:{net}")
            v: Verdict = ctx.verdict(nres) if nres and ctx.cls(nres) == "protected" else ctx.verdict(res)
            if added_container and v not in ("protected", "unexpected_in_scope"):
                continue
            out.append(Change("containers", nres if v == "protected" and nres else res, "network_added", v,
                              f"{label}: {clean(net, 80)}"))
        old = {tuple(m[:4]) for m in b["mounts"]}
        for m in sorted(c["mounts"]):
            if tuple(m[:4]) in old:
                continue
            src = ctx.rid(f"path:{m[1]}") if m[1].startswith("/") else None
            where = ctx.cls(src) if src else "out_of_scope"
            if where == "protected":
                v = "protected"
            elif where == "in_scope":
                v = ctx.verdict(res)
            else:
                v = "unexpected_in_scope" if inside else ctx.verdict(res)
            if added_container and v not in ("protected", "unexpected_in_scope"):
                continue
            out.append(Change("containers", src or res, "mount_added", v, f"{label}: {clean(m[1], 120)}"))
        for m in sorted(b["mounts"]):
            if tuple(m[:4]) not in {tuple(x[:4]) for x in c["mounts"]}:
                out.append(Change("containers", res, "mount_removed", ctx.verdict(res), f"{label}: {clean(m[1], 120)}"))
    return out


def _ports(base: Sequence[str], cur: Sequence[str], ctx: DiffContext) -> list[Change]:
    res = ctx.rid("detector:listeners")
    out = []
    for line in sorted(set(cur) - set(base)):
        addr = line.split(" ", 1)[-1]
        if _is_public(addr):
            out.append(Change("ports", res or "", "public_listener_added", "protected", clean(line, 80)))
        else:
            out.append(Change("ports", res or "", "listener_added", "unexpected_in_scope", clean(line, 80)))
    for line in sorted(set(base) - set(cur)):
        out.append(Change("ports", res or "", "listener_removed", "allowed", clean(line, 80)))
    return out


def _mounts(base: Sequence[str], cur: Sequence[str], ctx: DiffContext) -> list[Change]:
    b, c = _storage_mounts(base), _storage_mounts(cur)
    out = []
    for target in sorted(set(c) - set(b)):
        res = ctx.rid(f"mount:{target}")
        if res:
            out.append(Change("mounts", res, "mount_added", ctx.verdict(res), clean(target, 120)))
    for target in sorted(set(b) - set(c)):
        res = ctx.rid(f"mount:{target}")
        if res:
            out.append(Change("mounts", res, "mount_removed", ctx.verdict(res), clean(target, 120)))
    for target in sorted(set(b) & set(c)):
        old, new = _mount_state(b[target]), _mount_state(c[target])
        if old != new:
            res = ctx.rid(f"mount:{target}")
            if res:
                what = f"{old[2]}->{new[2]}" if old[2] != new[2] else "device, type or propagation changed"
                out.append(Change("mounts", res, "mount_options_changed", ctx.verdict(res, "unexpected_in_scope"),
                                  f"{clean(target, 100)}: {what}"))
    return out


def _files(base: dict, cur: dict, ctx: DiffContext) -> list[Change]:
    out = []
    for res in sorted(set(base) | set(cur)):
        b, c = base.get(res), cur.get(res)
        if b == c:
            continue
        kind = "created" if b == "absent" else "deleted" if c == "absent" else "changed"
        out.append(Change("files", res, f"file_{kind}", ctx.verdict(res, "unexpected_in_scope")))
    return out


def _shrunk(b: dict, c: dict) -> str | None:
    if c.get("absent") and not b.get("absent"):
        return "volume is gone"
    if b.get("absent") or c.get("absent") or b.get("blind") or c.get("blind"):
        return None
    if not b["capped"] and not c["capped"]:
        if b["size"] > 0 and (b["size"] - c["size"]) / b["size"] > VOLUME_SHRINK_SIZE:
            return f"size {b['size']} -> {c['size']}"
        if b["entries"] > 0 and (b["entries"] - c["entries"]) / b["entries"] > VOLUME_SHRINK_ENTRIES:
            return f"entries {b['entries']} -> {c['entries']}"
        return None
    if b["used"] > 0 and (b["used"] - c["used"]) / b["used"] > VOLUME_SHRINK_SIZE:
        return f"used {b['used']} -> {c['used']}"
    if b["top"] > 0 and (b["top"] - c["top"]) / b["top"] > VOLUME_SHRINK_ENTRIES:
        return f"top-level entries {b['top']} -> {c['top']}"
    return None


def _volumes(base: dict, cur: dict, ctx: DiffContext) -> list[Change]:
    out = []
    for res in sorted(set(base) & set(cur)):
        why = _shrunk(base[res], cur[res])
        if why:
            out.append(Change("volumes", res, "data_volume_shrunk", ctx.verdict(res, "unexpected_in_scope"), why))
    return out


def _vcs(base: dict, cur: dict, ctx: DiffContext) -> list[Change]:
    out = []
    destructive, remotes = ctx.rid("detector:vcs"), ctx.rid("detector:git_remotes")
    for tree in sorted(set(base) | set(cur)):
        b, c = base.get(tree), cur.get(tree)
        label = clean(tree, 120)
        if b is None or c is None or b.get("git") != c.get("git"):
            out.append(Change("vcs", destructive or "", "tree_changed", "protected", label))
            continue
        if c.get("git"):
            for field in ("head", "refs", "reflog"):
                if b[field] != c[field]:
                    out.append(Change("vcs", destructive or "", f"git_{field}_changed", "protected", label))
            for field in ("remotes", "tracking"):
                if b[field] != c[field]:
                    kind = "git_remote_config_changed" if field == "remotes" else "git_push_or_fetch_seen"
                    out.append(Change("vcs", remotes or "", kind, "protected", label))
        if b["worktree"] != c["worktree"]:
            res = ctx.rid(f"path:{tree}")
            if res:
                out.append(Change("vcs", res, "worktree_changed", ctx.verdict(res), label))
    return out


def diff_snapshots(base: dict[str, Any], cur: dict[str, Any], ctx: DiffContext) -> list[Change]:
    out: list[Change] = []
    out += _containers(base.get("containers", {}), cur.get("containers", {}), ctx)
    out += _ports(base.get("ports", []), cur.get("ports", []), ctx)
    out += _mounts(base.get("mounts", []), cur.get("mounts", []), ctx)
    out += _files(base.get("files", {}), cur.get("files", {}), ctx)
    if base.get("firewall") != cur.get("firewall"):
        out.append(Change("firewall", ctx.rid("detector:firewall") or "", "firewall_changed", "protected"))
    bb, cb = base.get("boot") or {}, cur.get("boot") or {}
    boot = ctx.rid("detector:boot") or ""
    if bb.get("boot_id") != cb.get("boot_id"):
        out.append(Change("boot", boot, "host_rebooted", "protected", "boot id changed"))
    elif cb.get("uptime", 0) < bb.get("uptime", 0):
        out.append(Change("boot", boot, "uptime_went_backwards", "protected", "uptime decreased"))
    bp, cp = base.get("packages", {}), cur.get("packages", {})
    pkg = ctx.rid("detector:packages") or ""
    for path in sorted(set(bp) | set(cp)):
        if bp.get(path) != cp.get(path):
            out.append(Change("packages", pkg, "package_state_changed", "protected", clean(path, 120)))
    out += _volumes(base.get("volumes", {}), cur.get("volumes", {}), ctx)
    out += _vcs(base.get("vcs", {}), cur.get("vcs", {}), ctx)
    return out


def worst(changes: Iterable[Change]) -> Verdict:
    return max((c.verdict for c in changes), key=lambda v: SEVERITY[v], default="allowed")


__all__ = ["Change", "DiffContext", "Verdict", "clean", "diff_snapshots", "worst"]
