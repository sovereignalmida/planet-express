"""The checkpoint service (contract §7, brief slice 5).

`create` validates scope, enforces the backup floor and the disk guard, then captures files and host facts into
the store and records the reference on the mission row. `capture_post` takes the post-change manifest.
`restore` is compare-and-swap: any file that is not in a state this checkpoint knows (the post-change hash, or
the original hash when it was already restored) blocks the whole restore and nothing is written, unless an
operator acknowledges the drift, in which case the drifted bytes are kept first. `purge` removes the directory
and leaves the mission's reference. Entry points are for the supervisor and tests; no operator RPC lives here.

File contents never appear in an event, a log line or an error string: only paths, hashes and field names.

Residual races, stated plainly. There is no cross-process lock on the files a mission touches, and POSIX has no
"replace this inode only if it is still that inode". Restore therefore narrows the window rather than closing it:
every destination is hashed through the same directory fd and O_NOFOLLOW open immediately before its rename, and
a mismatch aborts and is reported as drift. A writer that changes the file between that check and the rename
still wins the race; the supervisor must have proven the session dead (and fenced) before it restores.

The `operator:` actor check in `restore(ack_drift=True)` is a guard against internal callers, not authentication.
The authenticated identity is derived by the slice 7 RPC layer from the dashboard or Telegram session and is
never taken from request parameters.
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
from ..state import (
    NON_TERMINAL,
    TERMINAL,
    IllegalTransition,
    MissionNotFound,
    transition,
)
from ..store import MissionStore
from . import safefs
from .backup import BackupFreshness
from .provider import (
    RESTORABLE_FIELDS,
    CheckpointProvider,
    ContainerFacts,
    ProviderError,
    comparable_labels,
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


class ScopeChanged(CheckpointError):
    """A compose file changed between validation and capture so that the files it references no longer match
    the validated set. The checkpoint was discarded."""


class CheckpointId(str):
    """The checkpoint id, carrying the one flag the caller needs: `no_backup` (the grant was acknowledged without
    a verified backup, so the caller classes the mission Critical; this module never changes a tier)."""
    no_backup: bool = False
    facts_unavailable: tuple[str, ...] = ()       # stacks whose compose file was not valid YAML: files captured, no service facts

    def __new__(cls, value: str, no_backup: bool = False, facts_unavailable: tuple[str, ...] = ()):
        obj = super().__new__(cls, value)
        obj.no_backup = no_backup
        obj.facts_unavailable = facts_unavailable
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
    status: str                                      # done | refused | failed
    drifted: list[dict[str, str | None]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    drift_saved: list[str] = field(default_factory=list)
    unrestored: list[dict[str, Any]] = field(default_factory=list)       # restorable facts that still differ
    meta_skipped: list[dict[str, Any]] = field(default_factory=list)     # owner/ACL that could not be set
    observational: dict[str, Any] = field(default_factory=dict)          # reported, never asserted
    reason: str | None = None
    written_stacks: set[str] = field(default_factory=set)

    @property
    def partial(self) -> bool:
        """Some files were written and the restore did not finish: the host is half-restored."""
        return self.status != "done" and bool(self.written)

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
    base: str = ""       # directory its relative references resolve against


class _Hooks:
    """Test seams: fault injection between steps. Production leaves every one as a no-op."""

    def after_file_restored(self, count: int) -> None:
        pass

    def before_capture(self, planned: "list[_Planned]") -> None:
        pass

    def after_file_read(self, real: str) -> None:
        pass

    def before_reserve(self, mission_id: str) -> None:
        pass


class CheckpointService:
    def __init__(self, *, mission_store: MissionStore, root: Path | str, stacks_root: Path | str,
                 provider: CheckpointProvider, backup: BackupFreshness,
                 settings: CheckpointSettings | None = None, enabled: Callable[[], bool] | bool = True,
                 trace: Callable[[str, str], None] | None = None, hooks: _Hooks | None = None,
                 baseline_provider: Callable[[], Sequence[Any]] | None = None):
        self.missions = mission_store
        # Slice 8: returns the normalised scan findings (`verify.baseline.Finding`) at this moment. When set, a
        # checkpoint is not created without one (a failed scan is a refusal): verification compares against it.
        self.baseline_provider = baseline_provider
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
        self.store.purge_quarantine(self._now())
        referenced = {m.checkpoint_id for m in self.missions.list_missions() if m.checkpoint_id}
        unreferenced = [c for c in self.store.list_ids() if c not in referenced]
        return {"quarantined": quarantined, "unreferenced": unreferenced,
                "purge_pending": self.store.pending_removals()}

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

        checkpoint_id, manifest = None, None
        for attempt in (1, 2):
            checkpoint_id = "C-" + secrets.token_hex(8)
            try:
                self.hooks.before_capture(planned)
                self.store.new_checkpoint(checkpoint_id)
                # The scan baseline is taken PER ATTEMPT, so the one committed describes the host the committed
                # capture saw (a ScopeChanged retry takes a fresh one).
                baseline_text, baseline_sha, baseline_count = self._take_baseline()
                manifest = self._capture(checkpoint_id, mission_id, scope, planned, no_backup)
                manifest["baseline"] = None
                if baseline_text is not None:
                    self.store.write_baseline(checkpoint_id, baseline_text.encode("utf-8"))
                    manifest["baseline"] = {"sha256": baseline_sha, "findings": baseline_count}
                self.store.commit(checkpoint_id, manifest)
                break
            except ScopeChanged:
                self.store.discard(checkpoint_id)
                if attempt == 2:
                    raise
                planned = self.validate_scope(scope)          # one retry against what the files say now
            except BaseException:
                self.store.discard(checkpoint_id)
                raise
        self.hooks.before_reserve(mission_id)
        # Reserve the mission's slot atomically: the loser of a race keeps nothing.
        terminal = tuple(TERMINAL)
        now = self._now()
        reserved = False
        with self.missions.store._write() as conn:
            cur = conn.execute(
                "UPDATE missions SET checkpoint_id=?, updated_at=? WHERE id=? AND checkpoint_id IS NULL "
                f"AND status NOT IN ({','.join('?' * len(terminal))})", (checkpoint_id, now, mission_id, *terminal))
            if cur.rowcount == 1:
                reserved = True
                insert_event(conn, mission_id, "note", "core",
                             {"event": "checkpoint.created", "checkpoint": checkpoint_id, "files": len(planned),
                              "bytes": sum(f["size"] for f in manifest["files"]), "no_backup": no_backup,
                              "facts_unavailable": manifest["facts_unavailable"],
                              "hashes": {f["path"]: f["sha256"] for f in manifest["files"]}}, now)
        if not reserved:
            self.store.remove(checkpoint_id)
            raise CheckpointExists(f"{mission_id} already has a checkpoint, or is no longer live")
        return CheckpointId(checkpoint_id, no_backup, tuple(manifest["facts_unavailable"]))

    def retire_unused(self, mission_id: str) -> str | None:
        """Forget a checkpoint no session ever ran under, so the next start takes a fresh one.

        A start that fails after the checkpoint is taken but before any session row exists (the checkpoint itself, a blind
        tripwire baseline) leaves the mission waiting in `awaiting_decision` with that checkpoint attached. Retried hours later,
        after the operator has edited compose files, it would describe a host that no longer exists: a rollback would overwrite
        the operator's edits and verification would compare against a stale scan. (A failure after the session row is inserted,
        such as a launcher error, is not covered: the mission has had a session.)
        Not eligible (returns None) unless the mission is in `awaiting_decision` and has NEVER had a session: from then on the
        checkpoint is the only record of the host before the agent touched it, and must be kept. Eligible but impossible to remove
        raises CheckpointError and changes nothing. Returns the retired checkpoint id."""
        self._require_enabled()
        mission = self.missions.get_mission(mission_id)
        old = mission.checkpoint_id
        if not old or mission.status != "awaiting_decision":
            return None
        now = self._now()
        with self.missions.store._write() as conn:           # one write lock from the checks to the update: nothing can change between them
            if conn.execute("SELECT 1 FROM mission_sessions WHERE mission_id=?", (mission_id,)).fetchone():
                return None
            row = conn.execute("SELECT status, checkpoint_id FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None or row["status"] != "awaiting_decision" or row["checkpoint_id"] != old:
                return None
            # The files go first. If they cannot be moved aside nothing has changed: the mission keeps its checkpoint (stale but
            # safe) and the next start tries again. If they are moved but not fully deleted, the leftover is `.purging-<id>` and
            # the startup scan finishes it. Only then is the reference cleared, in the same transaction.
            try:
                self.store.remove(old)
            except OSError as exc:
                # Eligible but not removable: raised, because quietly launching under a stale checkpoint is the exact failure
                # this method exists to prevent. The transaction is abandoned, so nothing has changed and the caller can wait.
                log.warning("could not remove checkpoint %s for mission %s", old, mission_id, exc_info=True)
                raise CheckpointError(f"the unused checkpoint {old} could not be removed ({type(exc).__name__})") from None
            conn.execute("UPDATE missions SET checkpoint_id=NULL, updated_at=? WHERE id=? AND checkpoint_id=?", (now, mission_id, old))
            insert_event(conn, mission_id, "note", "core", {"event": "checkpoint.retired", "checkpoint": old,
                                                             "why": "no session ever ran under it; a fresh one is taken at the next start"}, now)
        return old

    def _take_baseline(self) -> tuple[str | None, str | None, int]:
        if self.baseline_provider is None:
            return None, None, 0
        from ..verify.baseline import baseline_document
        try:
            findings = list(self.baseline_provider())
            text, sha = baseline_document(findings)
        except Exception as exc:                        # noqa: BLE001 - no baseline, no checkpoint
            raise CheckpointError(f"the baseline scan failed ({type(exc).__name__}); no checkpoint was taken") from None
        return text, sha, len(findings)

    def validate_scope(self, scope: CheckpointScope) -> list[_Planned]:
        """Everything in scope must be a regular file Core can read, inside the stacks root after realpath,
        under the size limit. Anything else is REFUSED, never skipped. Files a compose file names in `env_file:`
        and `include:` are in scope too, under the same rules. Nothing is written."""
        planned: list[_Planned] = []
        seen: set[str] = set()

        def add(path: str, stack: str | None, kind: str, required: bool = True, depth: int = 0,
                child_base: str | None = None) -> None:
            try:
                real, symlink = safefs.resolve_in_root(self.stacks_root, path)
                if real in seen:
                    return
                try:
                    fd, meta = safefs.open_regular(self.stacks_root, real, max_bytes=self.settings.max_file_bytes)
                except safefs.UnsafePath:
                    if not required and safefs.is_absent(self.stacks_root, real):
                        return
                    raise
                try:
                    data = safefs.read_all(fd, self.settings.max_file_bytes) if kind in _PARSED else b""
                finally:
                    os.close(fd)
            except safefs.UnsafePath as exc:
                raise ScopeRefused(str(exc)) from None
            seen.add(real)
            base = child_base or os.path.dirname(real)
            planned.append(_Planned(path, real, symlink, stack, kind, meta, base))
            if kind in _PARSED:
                if depth >= MAX_INCLUDE_DEPTH:
                    raise ScopeRefused(f"{real}: includes are nested more than {MAX_INCLUDE_DEPTH} deep")
                for ref, ref_kind, ref_required, ref_base in _references(data, base):
                    add(ref, stack, ref_kind, ref_required, depth + 1, ref_base)

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
        files, compose_docs, parsed = [], {}, []
        captured = 0
        store_base = self.store.total_bytes() if self.store.root.exists() else 0
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
                self.hooks.after_file_read(item.real)
                # A torn read: size or mtime moved under us, we read a different length than the file had, or a
                # second pass over the same fd hashes differently from the bytes we kept.
                after = os.fstat(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                again = safefs.sha256_bytes(safefs.read_all(fd, self.settings.max_file_bytes + 1))
                if ((after.st_size, after.st_mtime_ns) != (meta.size, meta.mtime_ns) or len(data) != meta.size
                        or again != safefs.sha256_bytes(data)):
                    raise ScopeRefused(f"{item.real}: changed while it was captured")
            except safefs.UnsafePath as exc:
                raise ScopeRefused(str(exc)) from None
            finally:
                os.close(fd)
            # What was actually read is what counts against the limits, not what fstat promised.
            captured += len(data)
            if len(data) > self.settings.max_file_bytes:
                raise ScopeRefused(f"{item.real}: {len(data)} bytes exceeds the {self.settings.max_file_bytes} byte limit")
            if captured > self.settings.max_total_bytes:
                raise DiskGuardError(f"captured {captured} bytes; the per-checkpoint limit is "
                                     f"{self.settings.max_total_bytes}")
            if store_base + captured > self.settings.max_store_bytes:
                raise DiskGuardError(f"the checkpoint store would exceed {self.settings.max_store_bytes} bytes")
            self.store.write_payload(checkpoint_id, index, data)
            files.append({"idx": index, "path": item.real, "given_path": item.path, "symlink": item.symlink,
                          "stack": item.stack, "kind": item.kind, "sha256": safefs.sha256_bytes(data),
                          "mode": meta.mode, "uid": meta.uid, "gid": meta.gid, "size": len(data), "acl": acl})
            if item.kind in _PARSED:
                compose_docs.setdefault(item.stack, []).append(data)
                parsed.append((item, data))
        self._check_references(planned, parsed)

        services, unavailable = set(scope.services), []
        for stack in scope.stacks:
            names = _compose_services(compose_docs.get(stack, []))
            if names is None:
                unavailable.append(stack)           # invalid YAML: files are still captured, facts are not
            else:
                services |= {f"{stack}/{name}" for name in names}
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
                "facts_unavailable": sorted(unavailable),
                "scope": {"stacks": list(scope.stacks), "services": list(scope.services),
                          "units": list(scope.units)}}

    def _check_references(self, planned: list[_Planned], parsed: list[tuple[_Planned, bytes]]) -> None:
        """Expand the references again from the bytes that were actually captured. If the set differs from the
        validated one, a compose file changed in between and what was captured is not what was validated."""
        validated = {p.real for p in planned}
        expected = {p.real for p in planned if p.kind in ("include", "env_file")}
        found: set[str] = set()
        for item, data in parsed:
            for ref, _kind, required, _base in _references(data, item.base):
                try:
                    real, _ = safefs.resolve_in_root(self.stacks_root, ref)
                except safefs.UnsafePath:
                    raise ScopeChanged(f"{item.real}: a reference changed during capture") from None
                if not required and safefs.is_absent(self.stacks_root, real):
                    continue
                found.add(real)
        if found - validated or expected - found:
            raise ScopeChanged("a compose file changed between validation and capture")

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

    def manifest(self, checkpoint_id: str) -> dict[str, Any]:
        """The committed, immutable manifest (read-only: tripwires compare their baseline against it)."""
        self._require_enabled()
        return self._manifest(checkpoint_id)

    def baseline(self, checkpoint_id: str) -> list[Any] | None:
        """The scan baseline taken with this checkpoint, or None when it has none. Its hash must match the manifest's:
        a baseline that was altered after the commit raises CheckpointCorrupt rather than being believed."""
        self._require_enabled()
        manifest = self._manifest(checkpoint_id)
        recorded = manifest.get("baseline")
        if not recorded:
            return None
        try:
            data = self.store.read_baseline(checkpoint_id)
        except (OSError, StoreError):
            data = None
        if data is None or safefs.sha256_bytes(data) != recorded.get("sha256"):
            raise CheckpointCorrupt(f"{checkpoint_id}: the scan baseline is missing or does not match its manifest hash")
        from ..verify.baseline import load_baseline
        try:
            return load_baseline(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CheckpointCorrupt(f"{checkpoint_id}: the scan baseline is unreadable") from None

    def capture_post(self, checkpoint_id: str) -> None:
        """Take the post-change manifest. The caller does this only once the session is provably dead."""
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
        """Restore every captured file (compare-and-swap) and recreate the affected services.

        Never raises for a failure after the restore started: it reports `refused` or `failed`, says which files
        were already written (`partial`), records the event and moves the mission to needs_operator."""
        self._require_enabled()
        # The actor string is NOT authentication. The authenticated operator identity is derived by the RPC layer
        # (slice 7) from the dashboard or Telegram session and passed here; it is never taken from request
        # params. This check only stops internal callers from acknowledging drift as `core`.
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
        if post is None:
            # Without a post-change manifest there is nothing to tell the session's changes from someone else's.
            # The caller captures it once the session is provably dead, then asks again.
            report.status, report.reason = "refused", "needs_post_capture"
            self._event(mission_id, "checkpoint.restore_refused", actor,
                        {"checkpoint": checkpoint_id, "reason": report.reason})
            return report
        try:
            self._restore_files(checkpoint_id, manifest, payloads, post, ack_drift, actor, report)
        except Exception as exc:                    # noqa: BLE001 - nothing may escape a half-done restore
            report.status = "failed"
            report.reason = _safe_reason(exc)
            self._stop(mission_id, checkpoint_id, actor, report)
            return report
        if report.status == "refused":
            self._stop(mission_id, checkpoint_id, actor, report)
            return report
        try:
            self._recreate_and_compare(manifest, report)
        except Exception as exc:                    # noqa: BLE001
            report.status = "failed"
            report.reason = f"recreate or comparison failed: {_safe_reason(exc)}"
            self._stop(mission_id, checkpoint_id, actor, report)
            return report
        self._event(mission_id, "checkpoint.restore_done", actor,
                    {"checkpoint": checkpoint_id, "written": report.written, "unchanged": report.unchanged,
                     "drift_saved": report.drift_saved,
                     "unrestored": [{"service": u["service"], "fields": u["fields"]} for u in report.unrestored],
                     "meta_skipped": report.meta_skipped, "observational": report.observational})
        return report

    def _stop(self, mission_id: str, checkpoint_id: str, actor: str, report: RestoreReport) -> None:
        """Record a restore that did not finish, and hand the mission to the operator."""
        try:
            self._event(mission_id, "checkpoint.restore_refused", actor,
                        {"checkpoint": checkpoint_id, "status": report.status, "reason": report.reason,
                         "partial": report.partial, "written": report.written, "drifted": report.drifted})
        except Exception:                           # noqa: BLE001
            log.warning("checkpoint %s: could not record the restore outcome", checkpoint_id)
        try:
            transition(self.missions.store, mission_id, "needs_operator", "core")
        except IllegalTransition:
            pass                                    # already there, or terminal: the report still says why

    def _restore_files(self, checkpoint_id: str, manifest: dict[str, Any], payloads: dict[int, bytes],
                       post: dict[str, Any], ack_drift: bool, actor: str, report: RestoreReport) -> None:
        pending: list[tuple[dict[str, Any], bytes | None, str | None]] = []
        for entry in manifest["files"]:
            current, data, problem, meta = self._current(entry)
            if problem is not None:
                report.drifted.append({"path": entry["path"], "expected": None, "current": None, "reason": problem})
                continue
            recorded = post["files"].get(entry["path"]) or {}
            expected = "<unreadable>" if recorded.get("problem") else recorded.get("sha256", entry["sha256"])
            if current == entry["sha256"] and _metadata_matches(entry, meta):
                report.unchanged.append(entry["path"])
                continue
            if current != expected and current != entry["sha256"]:
                report.drifted.append({"path": entry["path"], "expected": expected, "current": current})
            pending.append((entry, data, current))

        uncomparable = [d for d in report.drifted if d.get("reason")]
        if report.drifted and (not ack_drift or uncomparable):
            report.status = "refused"
            report.reason = ("a file cannot be compared or its drifted bytes cannot be kept" if uncomparable
                             else "files changed since the post-change manifest")
            return

        drift_paths = {d["path"] for d in report.drifted}
        for entry, data, _seen in pending:
            if entry["path"] in drift_paths and data is not None:
                report.drift_saved.append(
                    self.store.save_drift(checkpoint_id, entry["idx"], safefs.sha256_bytes(data), data))
        for entry, _data, seen in pending:
            try:
                skipped = safefs.write_atomic(self.stacks_root, entry["path"], payloads[entry["idx"]],
                                              mode=entry["mode"], uid=entry["uid"], gid=entry["gid"],
                                              acl=entry.get("acl"), expect=(seen,),
                                              max_bytes=self.settings.max_file_bytes)
            except safefs.Drifted as exc:
                report.drifted.append({"path": entry["path"], "expected": seen, "current": exc.current})
                report.status, report.reason = "refused", "a file changed while it was being restored"
                return
            except safefs.UnsafePath as exc:
                report.status, report.reason = "refused", str(exc)
                return
            report.written.append(entry["path"])
            if skipped:
                report.meta_skipped.append({"path": entry["path"], "skipped": skipped})
            if entry.get("stack"):
                report.written_stacks.add(entry["stack"])
            self.hooks.after_file_restored(len(report.written))

    def _recreate_and_compare(self, manifest: dict[str, Any], report: RestoreReport) -> None:
        # Version-1 checkpoints created before generated labels were excluded contain Docker/Compose bookkeeping. Keep
        # those manifests restorable by comparing the same service-owned projection on both sides.
        captured = {service: _comparable_facts(before)
                    for service, before in manifest["facts"]["services"].items()}
        affected = []
        for service, before in captured.items():
            if service.split("/", 1)[0] in report.written_stacks:
                affected.append(service)
                continue
            try:
                now = self.provider.inspect(service)
            except ProviderError:
                affected.append(service)
                continue
            if before is not None and _facts_dict(now) != before:
                affected.append(service)
        if affected:
            result = self.provider.recreate(affected)
            if not result.ok:
                report.unrestored.append({"service": ",".join(affected), "fields": ["recreate"],
                                          "reason": result.error or "recreate failed"})
        for stack in manifest.get("facts_unavailable", []):
            report.unrestored.append({"service": stack, "fields": ["unverifiable"],
                                      "reason": "the compose file was not valid YAML when captured"})
        for service, before in captured.items():
            if before is None:
                # Nothing was observed at capture (no container, or none inspectable): there is nothing to
                # compare against, so this is unknown, never "no difference".
                report.unrestored.append({"service": service, "fields": ["unverifiable"],
                                          "reason": "no container facts were captured"})
                continue
            try:
                after = _facts_dict(self.provider.inspect(service))
            except ProviderError as exc:
                report.unrestored.append({"service": service, "fields": ["inspect"], "reason": str(exc)})
                continue
            if after == before:
                continue
            if after is None:
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
            if mission.status not in TERMINAL:
                raise CheckpointInUse(f"{mission.id} is {mission.status} and still references {checkpoint_id}")
        if not self.store.dir_for(checkpoint_id).exists():
            return False
        if not self.store.remove(checkpoint_id):
            log.warning("checkpoint %s: removal incomplete; it is unusable and will be retried at the next "
                        "startup scan", checkpoint_id)
            return False
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


_PARSED = ("compose", "override", "include")
MAX_INCLUDE_DEPTH = 5


def _metadata_matches(entry: dict[str, Any], meta: safefs.FileMeta | None) -> bool:
    """Same mode, and the same owner wherever Core could set one (only root can chown)."""
    if meta is None or meta.mode != entry["mode"]:
        return False
    return os.geteuid() != 0 or (meta.uid, meta.gid) == (entry["uid"], entry["gid"])


def _facts_dict(facts: ContainerFacts | None) -> dict[str, Any] | None:
    return facts.to_dict() if facts is not None else None


def _comparable_facts(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    facts = dict(raw)
    labels = facts.get("labels")
    if isinstance(labels, dict):
        facts["labels"] = comparable_labels(labels)
    return facts


def _compose_services(documents: Sequence[bytes]) -> list[str] | None:
    """Service names across the compose documents, or None when any of them is not valid YAML."""
    names: set[str] = set()
    for data in documents:
        try:
            doc = yaml.safe_load(data)
        except yaml.YAMLError:
            return None
        if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
            names |= {str(k) for k in doc["services"]}
    return sorted(names)


def _references(data: bytes, base_dir: str) -> list[tuple[str, str, bool, str | None]]:
    """(path, kind, required, child_base) for every `env_file:` and `include:` entry in one compose document.
    `child_base` is where the included file's own relative references resolve: the include's `project_directory`
    when it sets one, else the included file's directory (None). An include's own `env_file` resolves against
    the including file's directory. Invalid YAML
    yields nothing (the caller records facts as unavailable). A path with interpolation cannot be resolved by
    Core and refuses the scope."""
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[tuple[str, str, bool, str | None]] = []

    def take(value: Any, kind: str, inherited: bool = True, child_base: str | None = None) -> None:
        items = value if isinstance(value, list) else [value]
        for item in items:
            required = inherited
            if isinstance(item, dict):
                required = inherited and item.get("required", True) is not False
                item = item.get("path")
            if isinstance(item, list):
                for sub_item in item:
                    take(sub_item, kind, required, child_base)
                continue
            if not isinstance(item, str) or not item:
                continue
            if "$" in item:
                raise ScopeRefused(f"{item}: a path with variables cannot be captured")
            out.append((item if item.startswith("/") else os.path.join(base_dir, item), kind, required, child_base))

    for entry in doc.get("include") if isinstance(doc.get("include"), list) else [doc.get("include")]:
        if isinstance(entry, dict):
            project_dir = entry.get("project_directory")
            if isinstance(project_dir, str) and "$" in project_dir:
                raise ScopeRefused(f"{project_dir}: a path with variables cannot be captured")
            child = None
            if isinstance(project_dir, str) and project_dir:
                child = project_dir if project_dir.startswith("/") else os.path.join(base_dir, project_dir)
            take(entry.get("path"), "include", entry.get("required", True) is not False, child)
            if entry.get("env_file") is not None:
                take(entry["env_file"], "env_file")
            else:
                # No env_file on the include: Compose loads a `.env` implicitly. Which directory it reads is not
                # confirmed (the project_directory, the included file's own directory, or the including project's),
                # so every candidate that exists is captured. Over-capturing is safe; missing one is not.
                first = entry.get("path")
                first = first[0] if isinstance(first, list) and first else first
                candidates = [child] if child else []
                if isinstance(first, str) and first and "$" not in first:
                    included = first if first.startswith("/") else os.path.join(base_dir, first)
                    candidates.append(os.path.dirname(included))
                candidates.append(base_dir)
                for directory in dict.fromkeys(candidates):
                    out.append((os.path.join(directory, ".env"), "env_file", False, None))
        elif entry is not None:
            take(entry, "include")
    services = doc.get("services")
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict) and service.get("env_file") is not None:
                take(service["env_file"], "env_file")
    return out


def _safe_reason(exc: BaseException) -> str:
    """A reason fit for an event: our own errors say what they mean, anything else is its type (and OS reason)."""
    if isinstance(exc, (safefs.UnsafePath, ProviderError, CheckpointError)):
        return str(exc)[:300]
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return type(exc).__name__


__all__ = [
    "BackupFloorError", "CheckpointCorrupt", "CheckpointDisabled", "CheckpointError", "CheckpointExists",
    "CheckpointId", "CheckpointInUse", "CheckpointNotFound", "CheckpointScope", "CheckpointService",
    "CheckpointSettings", "DiskGuardError", "RestoreReport", "ScopeRefused",
]
