"""Deterministic incident observations derived from Leela monitor snapshots."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from planet_express.core.redact import redact

FINGERPRINT_VERSION = 1
MAX_IDENTITY_CHARS = 512
MAX_TEXT_CHARS = 1024
CRITICAL_IMAGES = ("postgres", "redis", "elasticsearch", "mariadb", "mysql", "valkey", "mongo")


@dataclass(frozen=True)
class Observation:
    fingerprint: str
    fingerprint_version: int
    kind: str
    resource: str
    condition: str
    severity: str | None
    summary: str
    details: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _text(value) -> str:
    return redact(str(value)[:4096])[:MAX_TEXT_CHARS]


def fingerprint(kind: str, resource: str) -> str:
    if not kind or not resource or len(kind) > 64 or len(resource) > MAX_IDENTITY_CHARS:
        raise ValueError("invalid incident identity")
    raw = json.dumps(
        {"version": FINGERPRINT_VERSION, "kind": kind, "resource": resource},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def scan_id(snapshot: dict) -> str:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _observation(kind, resource, condition, severity, summary, details=None):
    resource = str(resource or "").strip()
    return Observation(
        fingerprint(kind, resource), FINGERPRINT_VERSION, kind, resource, condition, severity,
        _text(summary), {key: _text(value) if isinstance(value, str) else value
                         for key, value in (details or {}).items()},
    )


def observations_from_snapshot(snapshot: dict) -> list[Observation]:
    observations = []

    for item in snapshot.get("containers", []):
        name = item.get("name")
        if not name:
            continue
        issue = item.get("issue")
        if issue or item.get("crash_looping"):
            condition = "failing"
            image = str(item.get("image", "")).lower()
            exit_match = re.match(r"^Exited \((-?\d+)\)", str(item.get("status", "")))
            nonzero_exit = exit_match is not None and int(exit_match[1]) != 0
            critical_failure = any(x in image for x in CRITICAL_IMAGES) and (
                item.get("health") == "unhealthy" or nonzero_exit
            )
            severity = "HIGH" if item.get("crash_looping") or critical_failure else "MEDIUM"
            summary = issue or "container is crash-looping"
        elif item.get("health") == "starting":
            condition, severity, summary = "unknown", None, "healthcheck is starting"
        else:
            condition, severity, summary = "healthy", None, "container is healthy"
        observations.append(_observation("container_health", name, condition, severity, summary, {
            "status": item.get("status", ""), "health": item.get("health", ""),
            "image": item.get("image", ""), "restart_count": item.get("restart_count", 0),
        }))

    for item in snapshot.get("stack_completeness", []):
        stack = item.get("stack")
        if not stack:
            continue
        services = item.get("services", {})
        has_unknown = item.get("status") == "unknown" or any(
            isinstance(value, dict) and value.get("status") == "unknown"
            for value in services.values()
        )
        if has_unknown:
            condition, severity = "unknown", item.get("alert") or "MEDIUM"
        elif item.get("alert"):
            condition, severity = "failing", item["alert"]
        else:
            condition, severity = "healthy", None
        summary = item.get("error") or (
            f"missing services: {', '.join(item.get('missing_services', []))}"
            if item.get("missing_services") else f"stack is {condition}"
        )
        observations.append(_observation("stack_completeness", stack, condition, severity, summary, {
            "missing_services": [_text(v) for v in item.get("missing_services", [])[:100]],
            "expected_count": item.get("expected_count"), "present_count": item.get("present_count"),
        }))

    for item in snapshot.get("disk", []):
        mount = item.get("mount")
        if not mount:
            continue
        severity = item.get("alert")
        observations.append(_observation("disk_usage", mount, "failing" if severity else "healthy",
                                         severity, f"disk usage is {item.get('used_pct', '?')}%", {
                                             "source": item.get("source", ""),
                                             "used_pct": item.get("used_pct"),
                                         }))

    mounts = snapshot.get("mounts")
    if isinstance(mounts, dict) and isinstance(mounts.get("missing"), list):
        missing = mounts["missing"]
        observations.append(_observation("configured_mounts", "host-mounts",
                                         "failing" if missing else "healthy",
                                         "HIGH" if missing else None,
                                         f"missing mounts: {', '.join(map(str, missing))}"
                                         if missing else "mounts reachable",
                                         {"missing": [_text(v) for v in missing[:100]]}))

    unraid = snapshot.get("unraid_exports")
    if isinstance(unraid, dict) and "reachable" in unraid:
        if unraid.get("reachable") is False:
            condition, severity, summary = "unknown", "MEDIUM", unraid.get("error", "Unraid unreachable")
        elif unraid.get("duplicate_fsids") or unraid.get("alert"):
            condition, severity, summary = "failing", "HIGH", "duplicate Unraid NFS fsids"
        else:
            condition, severity, summary = "healthy", None, "Unraid exports are healthy"
        observations.append(_observation("unraid_exports", "unraid", condition, severity, summary, {
            "duplicate_fsids": [_text(v) for v in unraid.get("duplicate_fsids", [])[:100]],
        }))

    for item in snapshot.get("nfs_mount_health", []):
        status = item.get("status")
        container, path = item.get("container"), item.get("path")
        if status == "unknown" and not container and not path:
            observations.append(_observation("nfs_discovery", "mount-discovery", "unknown",
                                             item.get("alert") or "MEDIUM",
                                             item.get("error", "NFS discovery failed")))
            continue
        if not container or not path or status == "unavailable":
            continue
        resource = f"{container}:{path}"
        condition = "healthy" if status == "ok" else "failing"
        observations.append(_observation("nfs_mount", resource, condition, item.get("alert"),
                                         item.get("error", f"NFS mount is {status}"), {
                                             "status": status, "mount": item.get("mount", ""),
                                             "nfs": item.get("nfs", ""),
                                         }))

    vpn = snapshot.get("vpn_port_forwarding")
    if isinstance(vpn, dict) and "reachable" in vpn:
        severity = "HIGH" if vpn.get("alert") else None
        observations.append(_observation("vpn_port_forwarding", "gluetun-qbittorrent",
                                         "failing" if severity else "healthy", severity,
                                         vpn.get("issue", "VPN port forwarding is healthy"), {
                                             "reachable": vpn.get("reachable"),
                                             "gluetun_port": vpn.get("gluetun_port"),
                                             "qbit_port": vpn.get("qbit_port"),
                                         }))

    for name, item in snapshot.get("backups", {}).items():
        if not isinstance(item, dict) or not name:
            continue
        healthy = item.get("result") == "success" and item.get("last_run") not in (None, "", "n/a")
        observations.append(_observation("backup_job", name, "healthy" if healthy else "failing",
                                         None if healthy else "HIGH",
                                         "backup completed successfully" if healthy else "backup result or last run is unhealthy", {
                                             "result": item.get("result", "unknown"),
                                             "last_run": item.get("last_run", ""),
                                         }))

    for unit, status in snapshot.get("services", {}).items():
        if not unit:
            continue
        healthy = status == "active"
        observations.append(_observation("systemd_service", unit, "healthy" if healthy else "failing",
                                         None if healthy else "MEDIUM", f"service is {status}", {"status": status}))

    cert_severity = {"expired": "CRITICAL", "expiring": "HIGH", "renew_soon": "MEDIUM"}
    for item in snapshot.get("certs", []):
        resolver = item.get("resolver")
        if not resolver:
            continue
        status = item.get("status") or item.get("tier")
        severity = "HIGH" if item.get("error") else cert_severity.get(status)
        observations.append(_observation("tls_certificate", resolver,
                                         "failing" if severity else "healthy", severity,
                                         item.get("error", f"certificate is {status or 'valid'}"), {
                                             "domain": item.get("domain", ""), "status": status or "valid",
                                             "days_remaining": item.get("days_remaining"),
                                         }))

    return sorted(observations, key=lambda item: (item.kind, item.resource))
