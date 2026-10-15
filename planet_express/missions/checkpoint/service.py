"""The checkpoint service (contract §7, brief slice 5).

`create` validates scope, enforces the backup floor and the disk guard, then captures files and host facts into
the store and records the reference on the mission row. `capture_post` takes the post-change manifest.
`restore` is compare-and-swap: any file that is not in a state this checkpoint knows (the post-change hash, or
the original hash when it was already restored) blocks the whole restore and nothing is written, unless an
operator acknowledges the drift, in which case the drifted bytes are kept first. `purge` removes the directory
and leaves the mission's reference. Entry points are for the supervisor and tests; no operator RPC lives here.

File contents never appear in an event, a log line or an error string: only paths, hashes and field names.
"""

import logging
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..events import insert_event
from ..models import Grant, Mission
from ..state import NON_TERMINAL, IllegalTransition, MissionNotFound, transition
from ..store import MissionStore
from . import safefs
from .backup import BackupFreshness
from .provider import (
    RESTORABLE_FIELDS,
    CheckpointProvider,
    ContainerFacts,
    ProviderError,
)
from .store import CheckpointStore, StoreError

log = logging.getLogger("planetexpress.missions.checkpoint")

MANIFEST_V = 1


class CheckpointError(Exception):
    """Base for every typed refusal."""


class CheckpointDisabled(CheckpointError):
    """Missions are off: no checkpoint code path runs."""


class ScopeRefused(CheckpointError):
    """The scope contains something Core cannot capture and restore (a grant cannot cover it)."""


class BackupFloorError(CheckpointError):
    def __init__(self, path: str, state: str):
        super().__init__(f"{path}: backup {state}; refusing without an acknowledged no-backup grant")
        self.path, self.state = path, state


class DiskGuardError(CheckpointError):
    pass


class CheckpointExists(CheckpointError):
    pass


class CheckpointNotFound(CheckpointError):
    pass


class CheckpointCorrupt(CheckpointError):
    pass


class CheckpointInUse(CheckpointError):
    pass


class CheckpointId(str):
    """The checkpoint id, carrying the one flag the caller needs: `no_backup` (the grant was acknowledged without
    a verified backup, so the caller classes the mission Critical; this module never changes a tier)."""
    no_backup: bool = False

    def __new__(cls, value: str, no_backup: bool = False):
        obj = super().__new__(cls, value)
        obj.no_backup = no_backup
        return obj


@dataclass(frozen=True)
class CheckpointSettings:
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_store_bytes: int = 2 * 1024 * 1024 * 1024
    retention_days: int = 90

    @classmethod
    def from_config(cls, cfg: Any) -> "CheckpointSettings":
        return cls(cfg.max_file_bytes, cfg.max_total_bytes, cfg.max_store_bytes, cfg.retention_days)


@dataclass(frozen=True)
class CheckpointScope:
    stacks: tuple[str, ...] = ()
    services: tuple[str, ...] = ()           # "<stack>/<service>"
    files: tuple[str, ...] = ()              # extra absolute file paths to capture
    data_paths: tuple[str, ...] = ()         # mounts and directories: not captured, the backup floor applies
    units: tuple[str, ...] = ()

    @classmethod
    def from_grant_scope(cls, scope: Any, *, units: Sequence[str] = ()) -> "CheckpointScope":
        files = tuple(p for p in scope.paths if not os.path.isdir(p))
        dirs = tuple(p for p in scope.paths if os.path.isdir(p))
        return cls(tuple(scope.stacks), tuple(scope.services), files, (*scope.mounts, *dirs), tuple(units))


@dataclass
class RestoreReport:
    status: str                                      # done | refused
    drifted: list[dict[str, str | None]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    drift_saved: list[str] = field(default_factory=list)
    unrestored: list[dict[str, Any]] = field(default_factory=list)       # restorable facts that still differ
    meta_skipped: list[dict[str, Any]] = field(default_factory=list)     # owner/ACL that could not be set
    observational: dict[str, Any] = field(default_factory=dict)          # reported, never asserted
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done" and not self.unrestored and not self.meta_skipped


@dataclass
class _Planned:
    path: str
    real: str
    symlink: bool
    stack: str | None
    kind: str
    meta: safefs.FileMeta


class _Hooks:
    """Test seams: fault injection between steps. Production leaves every one as a no-op."""

    def after_file_restored(self, count: int) -> None:
        pass

    def before_capture(self, planned: "list[_Planned]") -> None:
        pass


class CheckpointService:
    def __init__(self, *, mission_store: MissionStore, root: Path | str, stacks_root: Path | str,
                 provider: CheckpointProvider, backup: BackupFreshness,
                 settings: CheckpointSettings | None = None, enabled: Callable[[], bool] | bool = True,
                 trace: Callable[[str, str], None] | None = None, hooks: _Hooks | None = None):
        self.missions = mission_store
        self.store = CheckpointStore(root, trace=trace)
        self.stacks_root = safefs.real_root(str(stacks_root))
        self.provider = provider
        self.backup = backup
        self.settings = settings or CheckpointSettings()
        self._enabled = enabled if callable(enabled) else (lambda: enabled)
        self.hooks = hooks or _Hooks()

    # ── guards ──────────────────────────────────────────────────────────────
    def _require_enabled(self) -> None:
        if not self._enabled():
            raise CheckpointDisabled("missions are disabled")

    def _now(self) -> int:
        return self.missions._now()

    def _event(self, mission_id: str, name: str, actor: str, body: dict[str, Any]) -> None:
        self.missions.append_event(mission_id, "note", actor, {"event": name, **body})

    # ── startup ─────────────────────────────────────────────────────────────
    def startup_scan(self) -> dict[str, list[str]]:
        """Quarantine every directory without the COMMITTED marker; report committed ones no mission references."""
        self._require_enabled()
        quarantined = self.store.quarantine_uncommitted()
        referenced = {m.checkpoint_id for m in self.missions.list_missions() if m.checkpoint_id}
        unreferenced = [c for c in self.store.list_ids() if c not in referenced]
        return {"quarantined": quarantined, "unreferenced": unreferenced}

    # ── create ──────────────────────────────────────────────────────────────
    def create(self, mission_id: str, scope: CheckpointScope) -> CheckpointId:
        self._require_enabled()
        mission = self.missions.get_mission(mission_id)
        if mission.checkpoint_id:
            raise CheckpointExists(f"{mission_id} already has checkpoint {mission.checkpoint_id}")
        if mission.status not in NON_TERMINAL:
            raise CheckpointError(f"{mission_id} is {mission.status}; a checkpoint is for a live mission")
        grant = Grant.model_validate_json(mission.grant_json)
        planned = self.validate_scope(scope)
        no_backup = self._backup_floor(scope, grant)
        total = sum(p.meta.size for p in planned)
        if total > self.settings.max_total_bytes:
            raise DiskGuardError(f"scope is {total} bytes; the per-checkpoint limit is {self.settings.max_total_bytes}")
        if self.store.root.exists() and self.store.total_bytes() + total > self.settings.max_store_bytes:
            raise DiskGuardError(f"the checkpoint store would exceed {self.settings.max_store_bytes} bytes")

        checkpoint_id = "C-" + secrets.token_hex(8)
        try:
            self.hooks.before_capture(planned)
            self.store.new_checkpoint(checkpoint_id)
            manifest = self._capture(checkpoint_id, mission_id, scope, planned, no_backup)
            self.store.commit(checkpoint_id, manifest)
        except BaseException:
            self.store.discard(checkpoint_id)
            raise
        now = self._now()
        with self.missions.store._write() as conn:
            conn.execute("UPDATE missions SET checkpoint_id=?, updated_at=? WHERE id=? AND checkpoint_id IS NULL",
                         (checkpoint_id, now, mission_id))
            insert_event(conn, mission_id, "note", "core",
                         {"event": "checkpoint.created", "checkpoint": checkpoint_id, "files": len(planned),
                          "bytes": total, "no_backup": no_backup,
                          "hashes": {p.real: m["sha256"] for p, m in zip(planned, manifest["files"], strict=True)}},
                         now)
        return CheckpointId(checkpoint_id, no_backup)

    def validate_scope(self, scope: CheckpointScope) -> list[_Planned]:
        """Everything in scope must be a regular file Core can read, inside the stacks root after realpath,
        under the size limit. Anything else is REFUSED, never skipped. Nothing is written."""
        planned: list[_Planned] = []
        seen: set[str] = set()

        def add(path: str, stack: str | None, kind: str) -> None:
            try:
                real, symlink = safefs.resolve_in_root(self.stacks_root, path)
                if real in seen:
                    return
                fd, meta = safefs.open_regular(self.stacks_root, real, max_bytes=self.settings.max_file_bytes)
                os.close(fd)
            except safefs.UnsafePath as exc:
                raise ScopeRefused(str(exc)) from None
            seen.add(real)
            planned.append(_Planned(path, real, symlink, stack, kind, meta))

        stacks = set(scope.stacks) | {s.split("/", 1)[0] for s in scope.services}
        for stack in sorted(stacks):
            try:
                found = self.provider.discover_compose(stack)
            except ProviderError as exc:
                raise ScopeRefused(str(exc)) from None
            if not any(c.kind == "compose" for c in found):
                raise ScopeRefused(f"{stack}: no compose file found")
            for compose in found:
                add(compose.path, stack, compose.kind)
        for path in scope.files:
            add(path, None, "file")
        return planned

    def _backup_floor(self, scope: CheckpointScope, grant: Grant) -> bool:
        now, no_backup = self._now(), False
        for path in scope.data_paths:
            status = self.backup.status(path, now=now, recent_s=grant.backup.recent_s)
            if status.state == "ok":
                continue
            if not grant.backup.acknowledged_no_backup:
                raise BackupFloorError(path, status.state)
            no_backup = True
        return no_backup

    def _capture(self, checkpoint_id: str, mission_id: str, scope: CheckpointScope, planned: list[_Planned],
                 no_backup: bool) -> dict[str, Any]:
        files, compose_docs = [], {}
        for index, item in enumerate(planned):
            try:
                fd, meta = safefs.open_regular(self.stacks_root, item.real, max_bytes=self.settings.max_file_bytes)
            except safefs.UnsafePath as exc:
                raise ScopeRefused(str(exc)) from None
            try:
                if (meta.dev, meta.ino) != (item.meta.dev, item.meta.ino):
                    raise ScopeRefused(f"{item.real}: is not the file that was validated")
                acl = safefs.read_acl(fd)
                try:
                    data = safefs.read_all(fd, self.settings.max_file_bytes)
                except safefs.UnsafePath as exc:
                    raise ScopeRefused(str(exc)) from None
                now = os.fstat(fd)
                if (now.st_dev, now.st_ino) != (meta.dev, meta.ino):
                    raise ScopeRefused(f"{item.real}: changed while it was captured")
            finally:
                os.close(fd)
            self.store.write_payload(checkpoint_id, index, data)
            files.append({"idx": index, "path": item.real, "given_path": item.path, "symlink": item.symlink,
                          "stack": item.stack, "kind": item.kind, "sha256": safefs.sha256_bytes(data),
                          "mode": meta.mode, "uid": meta.uid, "gid": meta.gid, "size": len(data), "acl": acl})
            if item.kind in ("compose", "override"):
                compose_docs.setdefault(item.stack, []).append(data)

        services = set(scope.services)
        for stack in scope.stacks:
            services |= {f"{stack}/{name}" for name in _compose_services(compose_docs.get(stack, []))}
        try:
            facts = {
                "services": {s: (f.to_dict() if (f := self.provider.inspect(s)) else None) for s in sorted(services)},
                "ports": self.provider.listening_ports(),
                "mounts": self.provider.mount_table(),
                "units": self.provider.unit_hashes(scope.units) if scope.units else {},
            }
        except ProviderError as exc:
            raise ScopeRefused(str(exc)) from None
        return {"v": MANIFEST_V, "id": checkpoint_id, "mission_id": mission_id, "created_at": self._now(),
                "stacks_root": self.stacks_root, "no_backup": no_backup, "files": files, "facts": facts,
                "scope": {"stacks": list(scope.stacks), "services": list(scope.services),
                          "units": list(scope.units)}}

    # ── post-change manifest ────────────────────────────────────────────────
    def _manifest(self, checkpoint_id: str) -> dict[str, Any]:
        try:
            if not self.store.is_committed(checkpoint_id):
                raise CheckpointNotFound(f"{checkpoint_id}: no committed checkpoint")
            manifest = self.store.read_manifest(checkpoint_id)
        except StoreError:
            raise CheckpointNotFound(f"{checkpoint_id}: no committed checkpoint") from None
        except (OSError, ValueError):
            raise CheckpointCorrupt(f"{checkpoint_id}: manifest unreadable") from None
        if manifest.get("v") != MANIFEST_V or manifest.get("id") != checkpoint_id:
            raise CheckpointCorrupt(f"{checkpoint_id}: manifest does not match")
        return manifest

    def _verified_payload(self, checkpoint_id: str, entry: dict[str, Any]) -> bytes:
        try:
            data = self.store.read_payload(checkpoint_id, entry["idx"])
        except OSError:
            raise CheckpointCorrupt(f"{checkpoint_id}: payload {entry['idx']} missing") from None
        if safefs.sha256_bytes(data) != entry["sha256"]:
            raise CheckpointCorrupt(f"{checkpoint_id}: payload {entry['idx']} does not match its manifest hash")
        return data

    def _current(self, entry: dict[str, Any]) -> tuple[str | None, bytes | None, str | None, safefs.FileMeta | None]:
        """(sha256, bytes, problem, meta). problem is set when the file is present but cannot be read safely."""
        try:
            sha, data, meta = safefs.file_state(self.stacks_root, entry["path"],
                                                max_bytes=self.settings.max_file_bytes)
            return sha, data, None, meta
        except safefs.UnsafePath as exc:
            return None, None, str(exc), None

    def capture_post(self, checkpoint_id: str) -> None:
        self._require_enabled()
        manifest = self._manifest(checkpoint_id)
        files, changed = {}, []
        for entry in manifest["files"]:
            sha, _data, problem, _meta = self._current(entry)
            files[entry["path"]] = {"sha256": sha, "problem": problem is not None}
            if sha != entry["sha256"]:
                changed.append(entry["path"])
        self.store.write_post(checkpoint_id, {"v": MANIFEST_V, "post_at": self._now(), "files": files})
        self._event(manifest["mission_id"], "checkpoint.post_captured", "core",
                    {"checkpoint": checkpoint_id, "files": len(files), "changed": changed,
                     "hashes": {p: v["sha256"] for p, v in files.items()}})

    # ── restore ─────────────────────────────────────────────────────────────
    def restore(self, checkpoint_id: str, *, ack_drift: bool = False, actor: str = "core") -> RestoreReport:
        self._require_enabled()
        if ack_drift and not actor.startswith("operator:"):
            raise ValueError("acknowledging drift is an operator decision: pass actor='operator:<name>'")
        manifest = self._manifest(checkpoint_id)
        mission_id = manifest["mission_id"]
        try:
            mission = self.missions.get_mission(mission_id)
        except MissionNotFound:
            raise CheckpointNotFound(f"{checkpoint_id}: its mission no longer exists") from None
        if mission.checkpoint_id != checkpoint_id:
            raise CheckpointNotFound(f"{checkpoint_id}: not the checkpoint of {mission_id}")
        payloads = {e["idx"]: self._verified_payload(checkpoint_id, e) for e in manifest["files"]}
        post = self.store.read_post(checkpoint_id)
        self._event(mission_id, "checkpoint.restore_started", actor,
                    {"checkpoint": checkpoint_id, "ack_drift": ack_drift, "post_manifest": post is not None})

        report = RestoreReport(status="done")
        pending: list[tuple[dict[str, Any], bytes | None]] = []
        for entry in manifest["files"]:
            current, data, problem, meta = self._current(entry)
            if problem is not None:
                report.drifted.append({"path": entry["path"], "expected": None, "current": None,
                                       "reason": problem})
                continue
            expected = entry["sha256"]
            if post is not None:
                expected = (post["files"].get(entry["path"]) or {}).get("sha256", entry["sha256"])
                if (post["files"].get(entry["path"]) or {}).get("problem"):
                    expected = "<unreadable>"
            if current == entry["sha256"] and _metadata_matches(entry, meta):
                report.unchanged.append(entry["path"])
                continue
            if current != expected and current != entry["sha256"]:
                report.drifted.append({"path": entry["path"], "expected": expected, "current": current})
            pending.append((entry, data))

        uncomparable = [d for d in report.drifted if d.get("reason")]
        if report.drifted and (not ack_drift or uncomparable):
            report.status = "refused"
            report.reason = ("a file cannot be compared or its drifted bytes cannot be kept" if uncomparable
                             else "files changed since the post-change manifest")
            self._event(mission_id, "checkpoint.restore_refused", actor,
                        {"checkpoint": checkpoint_id, "drifted": report.drifted, "reason": report.reason})
            try:
                transition(self.missions.store, mission_id, "needs_operator", "core")
            except IllegalTransition:
                pass                                   # already there, or terminal: the report still says why
            return report

        drift_paths = {d["path"] for d in report.drifted}
        for entry, data in pending:
            if entry["path"] in drift_paths and data is not None:
                report.drift_saved.append(
                    self.store.save_drift(checkpoint_id, entry["idx"], safefs.sha256_bytes(data), data))
        written_stacks: set[str] = set()
        for entry, _data in pending:
            try:
                skipped = safefs.write_atomic(self.stacks_root, entry["path"], payloads[entry["idx"]],
                                              mode=entry["mode"], uid=entry["uid"], gid=entry["gid"],
                                              acl=entry.get("acl"))
            except safefs.UnsafePath as exc:
                report.status = "refused"
                report.reason = str(exc)
                self._event(mission_id, "checkpoint.restore_refused", actor,
                            {"checkpoint": checkpoint_id, "reason": report.reason, "written": report.written})
                try:
                    transition(self.missions.store, mission_id, "needs_operator", "core")
                except IllegalTransition:
                    pass
                return report
            report.written.append(entry["path"])
            if skipped:
                report.meta_skipped.append({"path": entry["path"], "skipped": skipped})
            if entry.get("stack"):
                written_stacks.add(entry["stack"])
            self.hooks.after_file_restored(len(report.written))

        self._recreate_and_compare(manifest, written_stacks, report)
        self._event(mission_id, "checkpoint.restore_done", actor,
                    {"checkpoint": checkpoint_id, "written": report.written, "unchanged": report.unchanged,
                     "drift_saved": report.drift_saved,
                     "unrestored": [{"service": u["service"], "fields": u["fields"]} for u in report.unrestored],
                     "meta_skipped": report.meta_skipped, "observational": report.observational})
        return report

    def _recreate_and_compare(self, manifest: dict[str, Any], written_stacks: set[str], report: RestoreReport) -> None:
        captured = manifest["facts"]["services"]
        affected = []
        for service, before in captured.items():
            if service.split("/", 1)[0] in written_stacks:
                affected.append(service)
                continue
            try:
                now = self.provider.inspect(service)
            except ProviderError:
                affected.append(service)
                continue
            if _facts_dict(now) != before:
                affected.append(service)
        if affected:
            result = self.provider.recreate(affected)
            if not result.ok:
                report.unrestored.append({"service": ",".join(affected), "fields": ["recreate"],
                                          "reason": result.error or "recreate failed"})
        for service, before in captured.items():
            try:
                after = _facts_dict(self.provider.inspect(service))
            except ProviderError as exc:
                report.unrestored.append({"service": service, "fields": ["inspect"], "reason": str(exc)})
                continue
            if after == before:
                continue
            if before is None or after is None:
                report.unrestored.append({"service": service, "fields": ["existence"]})
                continue
            fields = [f for f in RESTORABLE_FIELDS if before.get(f) != after.get(f)]
            report.unrestored.append({"service": service, "fields": fields})
        observed = {}
        try:
            for name, now in (("ports", self.provider.listening_ports()), ("mounts", self.provider.mount_table())):
                then = manifest["facts"].get(name, [])
                if now != then:
                    observed[name] = {"added": sorted(set(now) - set(then)), "removed": sorted(set(then) - set(now))}
            units = manifest["facts"].get("units", {})
            if units:
                now_units = self.provider.unit_hashes(list(units))
                if now_units != units:
                    observed["units"] = sorted(u for u in units if now_units.get(u) != units[u])
        except ProviderError:
            observed["error"] = "observation failed"
        report.observational = observed

    # ── purge and retention ─────────────────────────────────────────────────
    def purge(self, checkpoint_id: str) -> bool:
        """Remove the directory; the mission row keeps its reference. Refused while a mission that references the
        checkpoint is not terminal. Returns False when there was nothing to remove."""
        self._require_enabled()
        self.store.dir_for(checkpoint_id)
        for mission in self._referencing(checkpoint_id):
            if mission.status not in ("completed", "failed", "rolled_back", "cancelled"):
                raise CheckpointInUse(f"{mission.id} is {mission.status} and still references {checkpoint_id}")
        if not self.store.dir_for(checkpoint_id).exists():
            return False
        self.store.remove(checkpoint_id)
        for mission in self._referencing(checkpoint_id):
            self._event(mission.id, "checkpoint.purged", "core", {"checkpoint": checkpoint_id})
        return True

    def purge_expired(self) -> list[str]:
        """Purge every checkpoint whose mission closed at least `retention_days` ago."""
        self._require_enabled()
        cutoff = self._now() - self.settings.retention_days * 86400
        purged = []
        for mission in self.missions.list_missions():
            if (mission.checkpoint_id and mission.closed_at is not None and mission.closed_at <= cutoff
                    and self.store.dir_for(mission.checkpoint_id).exists()
                    and self.purge(mission.checkpoint_id)):
                purged.append(mission.checkpoint_id)
        return purged

    def _referencing(self, checkpoint_id: str) -> list[Mission]:
        return [m for m in self.missions.list_missions() if m.checkpoint_id == checkpoint_id]


def _metadata_matches(entry: dict[str, Any], meta: safefs.FileMeta | None) -> bool:
    """Same mode, and the same owner wherever Core could set one (only root can chown)."""
    if meta is None or meta.mode != entry["mode"]:
        return False
    return os.geteuid() != 0 or (meta.uid, meta.gid) == (entry["uid"], entry["gid"])


def _facts_dict(facts: ContainerFacts | None) -> dict[str, Any] | None:
    return facts.to_dict() if facts is not None else None


def _compose_services(documents: Sequence[bytes]) -> list[str]:
    names: set[str] = set()
    for data in documents:
        try:
            doc = yaml.safe_load(data)
        except yaml.YAMLError:
            raise ScopeRefused("a compose file in scope is not valid YAML") from None
        if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
            names |= {str(k) for k in doc["services"]}
    return sorted(names)


__all__ = [
    "BackupFloorError", "CheckpointCorrupt", "CheckpointDisabled", "CheckpointError", "CheckpointExists",
    "CheckpointId", "CheckpointInUse", "CheckpointNotFound", "CheckpointScope", "CheckpointService",
    "CheckpointSettings", "DiskGuardError", "RestoreReport", "ScopeRefused",
]
