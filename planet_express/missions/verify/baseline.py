"""The finding normaliser (contract 10): ONE function turns a Leela full-scan snapshot into bounded, redacted findings.

The same function reads the baseline taken when the checkpoint is created and the fresh scan taken at verification,
so the two are comparable by construction. A finding is `{kind, resource_id, key_hash, severity}`:

* `resource_id` is a canonical contract-2.3 id, or the literal `unscoped` when the snapshot row cannot be tied to
  one. An `unscoped` finding counts as in scope for every mission (fail closed): it is never dropped;
* `key_hash` identifies the finding by what is wrong and where (never by volatile numbers or messages), so the same
  fault in two scans compares equal and a new fault of the same kind elsewhere does not;
* nothing free-form (container output, error text, paths beyond the resource id) is kept.

Sections the normaliser does not know are scanned generically: any row carrying an `alert`, `issue` or `error` becomes
one `unscoped` finding for the section. Only the sections named in `INFORMATIONAL` are skipped.
"""

import hashlib
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ..canonical import canonical_json, sha256_hex
from ..resources import DEFAULT_HOST, BadResource, canon, contains

UNSCOPED = "unscoped"
MAX_FINDINGS = 2000
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
# Sections that report facts, not faults (an update list, a disk-usage table, uptime and journal lines).
INFORMATIONAL = frozenset({"timestamp", "agent", "mode", "docker_disk", "image_candidates", "system"})


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    resource_id: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    key_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    severity: Annotated[StrictStr, Field(max_length=16)] = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.resource_id, self.key_hash)


def _key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()


def _sev(row: Mapping[str, Any], default: str = "MEDIUM") -> str:
    value = str(row.get("alert") or default).upper()
    return value if value in SEVERITIES else default


def _resource(text: str, host: str) -> str:
    try:
        return canon(text, host)
    except BadResource:
        return UNSCOPED


def _category(issue: str) -> str:
    for prefix, name in (("crash-looping", "crash_looping"), ("not running", "not_running"),
                         ("healthcheck failing", "unhealthy"), ("healthcheck still initialising", "starting_stuck")):
        if issue.startswith(prefix):
            return name
    return "issue"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []


def normalize(snapshot: Mapping[str, Any], *, host: str = DEFAULT_HOST) -> list[Finding]:
    """The findings of one snapshot, deduplicated and sorted. Deterministic: the same host state gives identical records."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("a scan snapshot is a mapping")
    out: dict[tuple[str, str, str], Finding] = {}

    def add(kind: str, resource: str, identity: str, severity: str) -> None:
        f = Finding(kind=kind, resource_id=resource, key_hash=_key(kind, resource, identity), severity=severity)
        out.setdefault(f.key, f)

    stacks = _rows(snapshot.get("stack_completeness"))
    container_service: dict[str, str] = {}
    for row in stacks:
        stack = str(row.get("stack") or "")
        services = row.get("services") if isinstance(row.get("services"), Mapping) else {}
        for name, info in services.items():
            if isinstance(info, Mapping) and isinstance(info.get("container"), str):
                container_service[info["container"]] = _resource(f"service:{stack}/{name}", host)

    for row in stacks:
        stack = str(row.get("stack") or "")
        if row.get("status") == "unknown":
            add("stack_unreadable", _resource(f"stack:{stack}", host), "unknown", _sev(row))
        for service in row.get("missing_services") or []:
            add("service_missing", _resource(f"service:{stack}/{service}", host), "missing", _sev(row, "HIGH"))

    for row in _rows(snapshot.get("containers")):
        issue = row.get("issue")
        if not issue and not row.get("crash_looping"):
            continue
        name = str(row.get("name") or "")
        category = _category(str(issue or "crash-looping"))
        add("container_issue", container_service.get(name, UNSCOPED), f"{name}\0{category}",
            "HIGH" if row.get("crash_looping") or category == "not_running" else "MEDIUM")

    for row in _rows(snapshot.get("disk")):
        if row.get("alert"):
            add("disk_alert", _resource(f"path:{row.get('mount')}", host), "alert", _sev(row))

    mounts = snapshot.get("mounts")
    for path in (mounts.get("missing") if isinstance(mounts, Mapping) else None) or []:
        add("mount_missing", _resource(f"path:{path}", host) if isinstance(path, str) else UNSCOPED, "missing", "HIGH")

    for row in _rows(snapshot.get("nfs_mount_health")):
        if row.get("alert"):
            path = row.get("path")
            add("nfs_mount", _resource(f"path:{path}", host) if isinstance(path, str) else UNSCOPED,
                f"{row.get('container')}\0{row.get('status')}", _sev(row))

    vpn = snapshot.get("vpn_port_forwarding")
    if isinstance(vpn, Mapping) and vpn.get("alert"):
        add("vpn_port", UNSCOPED, "alert", _sev(vpn))

    exports = snapshot.get("unraid_exports")
    if isinstance(exports, Mapping):
        for fsid in exports.get("duplicate_fsids") or []:
            add("duplicate_fsid", UNSCOPED, str(fsid), "MEDIUM")

    services = snapshot.get("services")
    if isinstance(services, Mapping):
        for unit, state in services.items():
            if state != "active":
                add("unit_not_active", _resource(f"unit:{unit}", host), str(state), "HIGH")

    backups = snapshot.get("backups")
    if isinstance(backups, Mapping):
        for label, info in backups.items():
            if isinstance(info, Mapping) and info.get("result") not in ("success", "unknown", None):
                add("backup_failed", UNSCOPED, str(label), "MEDIUM")

    for row in _rows(snapshot.get("certs")):
        if row.get("kind") == "error":
            add("cert_error", UNSCOPED, str(row.get("domain") or row.get("path") or ""), "MEDIUM")

    known = {"stack_completeness", "containers", "disk", "mounts", "nfs_mount_health", "vpn_port_forwarding",
             "unraid_exports", "services", "backups", "certs"}
    for section, value in snapshot.items():
        if section in known or section in INFORMATIONAL:
            continue
        if _has_fault(value, 0):
            add(f"section:{section}"[:64], UNSCOPED, section, "MEDIUM")

    if len(out) > MAX_FINDINGS:
        kept = sorted(out.values(), key=lambda f: f.key)[:MAX_FINDINGS - 1]
        out = {f.key: f for f in kept}
        add("overflow", UNSCOPED, "more than the finding cap", "MEDIUM")
    return sorted(out.values(), key=lambda f: f.key)


def _has_fault(value: Any, depth: int) -> bool:
    if depth > 4:
        return True                                  # too deep to read: treat as a fault rather than skip it
    if isinstance(value, Mapping):
        if any(value.get(k) for k in ("alert", "issue", "error")):
            return True
        return any(_has_fault(v, depth + 1) for v in value.values() if isinstance(v, Mapping | list))
    if isinstance(value, list):
        return any(_has_fault(v, depth + 1) for v in value[:200])
    return False


def baseline_document(findings: Iterable[Finding]) -> tuple[str, str]:
    """(canonical JSON, sha256) of a baseline: what is persisted next to the checkpoint and recorded in its manifest."""
    records = [f.model_dump(mode="json") for f in sorted(findings, key=lambda f: f.key)]
    doc = {"v": 1, "findings": records}
    return canonical_json(doc), sha256_hex(doc)


def load_baseline(text: str) -> list[Finding]:
    import json
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("findings"), list):
        raise ValueError("not a baseline document")
    return [Finding.model_validate(r) for r in doc["findings"]]


def in_scope(finding: Finding, scope_ids: Iterable[str], *, stacks_root: str | None = None) -> bool:
    """Unscoped findings are in scope for every mission; a resource finding is in scope when a scope id contains it."""
    if finding.resource_id == UNSCOPED:
        return True
    return any(contains(s, finding.resource_id, stacks_root=stacks_root) for s in scope_ids)


def new_findings(baseline: Iterable[Finding], fresh: Iterable[Finding]) -> list[Finding]:
    """Findings in `fresh` that the baseline did not have."""
    seen = {f.key for f in baseline}
    return [f for f in fresh if f.key not in seen]
