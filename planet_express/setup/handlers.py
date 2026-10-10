"""Setup step handlers: what each catalogue kind actually does to the host.

One class per kind, all written against the `Host` interface (`host.py`), never the filesystem. The
contract (docs/designs/setup-apply.md):

* `check`     read-only, immediately before acting: `Satisfied` (nothing to do), `Proceed`, or `Refuse`.
* `act`       the change. Records undo evidence in the journal *before* the point of no return.
* `verify`    reads state back and decides `applied`, `not_applied` or `unknown`. Never an exit code.
* `reconcile` after a crash, from host state alone: did the step happen?

The compare-and-swap rule is `compose_files.write_compose`'s: a file that is not what the operator
approved is never overwritten.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from typing import ClassVar, Literal

from planet_express.setup.host import HostError, _split, is_temp_name, new_temp_name

Effect = Literal["applied", "not_applied", "unknown"]
_PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")


@dataclass(frozen=True)
class Satisfied:
    reason: str


@dataclass(frozen=True)
class Proceed:
    pass


@dataclass(frozen=True)
class Refuse:
    reason: str


class StepFailure(Exception):
    """`act` could not finish. `effect` says what the host may now look like."""

    def __init__(self, reason: str, effect: Effect = "not_applied"):
        super().__init__(reason)
        self.reason, self.effect = reason, effect


class Irreversible(Exception):
    """Undo has nothing to revert for this step, or cannot. Named in the undo report; it does not stop the undo."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Context:
    host: object
    journal: object
    step: object                    # planet_express.setup.plan.Step
    secrets: dict
    evidence_dir: str               # a private directory on the host's filesystem, for backups
    evidence_ids: tuple = (0, 0)    # its owner; backups belong to the same account

    def log(self, line: str) -> None:
        self.journal.append("log", step=self.step.id, line=line)

    def evidence(self, **data) -> None:
        self.journal.append("evidence", step=self.step.id, data=data)

    def prior_evidence(self) -> list[dict]:
        record = self.journal.steps().get(self.step.id)
        return list(record.evidence) if record else []


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unresolved_secrets(content: str, secrets: dict) -> list[str]:
    return sorted({m for m in _PLACEHOLDER.findall(content) if m not in secrets})


def render(content: str, secrets: dict) -> bytes:
    """Substitute `{{secret:NAME}}` placeholders. Done at the last moment, in memory; a placeholder with no
    value is an error, never written out literally."""
    missing = unresolved_secrets(content, secrets)
    if missing:
        raise StepFailure(f"no value for secret(s): {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda m: secrets[m.group(1)], content).encode("utf-8")


def _parent(path: str) -> str:
    return _split(path)[0]


def _owner(ctx, p: dict | None = None) -> tuple[int, int] | Refuse:
    p = p if p is not None else ctx.step.params
    user = ctx.host.lookup_user(p["owner"])
    group = ctx.host.lookup_group(p["group"])
    if user is None:
        return Refuse(f"no such user: {p['owner']}")
    if group is None:
        return Refuse(f"no such group: {p['group']}")
    return user[0], group


# ----------------------------------------------------------------------------------------------------------
class DirEnsure:
    def _missing(self, host, path):
        """(missing components top-down, or a Refuse)."""
        missing, probe = [], path
        while True:
            st = host.lstat(probe)
            if st is not None:
                if st.kind != "dir":
                    return Refuse(f"{probe} is a {st.kind}, not a directory")
                return list(reversed(missing))
            missing.append(probe)
            if probe == "/":
                return Refuse("no existing ancestor")
            probe = _parent(probe)

    def check(self, ctx):
        path = ctx.step.params["path"]
        ids = _owner(ctx)
        if isinstance(ids, Refuse):
            return ids
        found = self._missing(ctx.host, path)
        if isinstance(found, Refuse):
            return found
        if not found and ctx.step.params.get("tighten"):
            excess = self._excess(ctx)
            if excess:
                return Proceed()
        return Satisfied(f"{path} already exists") if not found else Proceed()

    @staticmethod
    def _excess(ctx) -> int:
        """Permission bits the directory has that the plan does not allow (0 when it is as private as planned)."""
        found = ctx.host.lstat(ctx.step.params["path"])
        return 0 if found is None or found.kind != "dir" else found.perms & ~int(ctx.step.params["mode"], 8) & 0o7777

    def act(self, ctx):
        p = ctx.step.params
        uid, gid = _owner(ctx)
        missing = self._missing(ctx.host, p["path"])
        if isinstance(missing, Refuse):
            raise StepFailure(missing.reason)
        if not missing and p.get("tighten") and self._excess(ctx):
            before = ctx.host.lstat(p["path"])
            ctx.evidence(tightened_directory=p["path"], previous_mode=before.perms, **before.identity)
            try:
                after = ctx.host.chmod_dir(p["path"], before.perms & int(p["mode"], 8))
            except HostError as exc:
                raise StepFailure(str(exc)) from exc
            ctx.log(f"tightened {p['path']} from {before.perms:04o} to {after.perms:04o}")
            return
        for component in missing:
            ctx.evidence(creating_directory=component)       # before it exists: a crash can leave it, never unrecorded
            try:
                made = ctx.host.mkdir(component, int(p["mode"], 8), uid, gid)
            except HostError as exc:
                raise StepFailure(str(exc), "applied" if component != missing[0] else "not_applied") from exc
            ctx.evidence(created_directory=component, **made.identity)
            ctx.log(f"created {component} ({p['mode']}, {p['owner']}:{p['group']})")

    def verify(self, ctx) -> Effect:
        st = ctx.host.lstat(ctx.step.params["path"])
        if st is None or st.kind != "dir":
            return "not_applied"
        return "not_applied" if ctx.step.params.get("tighten") and self._excess(ctx) else "applied"

    reconcile = verify

    def inverse(self, ctx) -> str:
        """Remove the directories this step made (newest first) if they are empty and still the same inode; put a
        tightened directory back to its old mode if nobody has changed it since."""
        notes = []
        entries = ctx.prior_evidence()
        recorded = {e["created_directory"] for e in entries if "created_directory" in e}
        for entry in reversed(entries):
            if "creating_directory" in entry and entry["creating_directory"] not in recorded:
                # Intent without the identity that follows creation: the process died, or mkdir failed. Nothing
                # proves a directory at this path is setup's (an operator may have made it since), so it stays.
                path = entry["creating_directory"]
                if ctx.host.lstat(path) is not None:
                    notes.append(f"{path} exists but setup cannot prove it made it, so it is left in place")
            elif "tightened_directory" in entry:
                path = entry["tightened_directory"]
                found = ctx.host.lstat(path)
                if found is None or found.kind != "dir" or (found.dev, found.ino) != (entry["dev"], entry["ino"]):
                    raise StepFailure(f"{path} is not the directory setup tightened; it is left as it is")
                if found.perms == entry["previous_mode"]:
                    notes.append(f"{path} is already back to {found.perms:04o}")
                    continue
                tightened = entry["previous_mode"] & int(ctx.step.params["mode"], 8)
                if found.perms != tightened:
                    raise StepFailure(f"{path} was changed to {found.perms:04o} after setup tightened it; left as it is")
                ctx.host.chmod_dir(path, entry["previous_mode"])
                notes.append(f"restored {path} to {entry['previous_mode']:04o}")
            elif "created_directory" in entry:
                path = entry["created_directory"]
                found = ctx.host.lstat(path)
                if found is None:
                    notes.append(f"{path} is already gone")
                    continue
                if found.kind != "dir" or (found.dev, found.ino) != (entry["dev"], entry["ino"]):
                    raise StepFailure(f"{path} is not the directory setup created; it is left as it is")
                if ctx.host.listdir(path):
                    raise StepFailure(f"{path} is not empty, so it is left in place")
                ctx.host.rmdir(path)
                notes.append(f"removed {path}")
        return "; ".join(notes) or "no directory had been created"


# ----------------------------------------------------------------------------------------------------------
class _AtomicFile:
    """Compare-and-swap, backup, a journaled intent, a staged file and an atomic rename: the one careful way
    this package puts a file on the host. `file.write`, `sudoers.install` and `dashboard.init` are all this,
    differing only in where the bytes come from and what is checked before the rename."""

    def norm(self, ctx) -> dict:
        """path, mode, owner, group, has_secrets, if_exists, expect_absent, expected_sha256."""
        raise NotImplementedError

    def desired(self, ctx) -> bytes:
        raise NotImplementedError

    def validate_staged(self, ctx, parent: str, temp: str) -> None:
        """Called with the staged file in place and before it is renamed; raise StepFailure to abort."""

    # -- the guard ---------------------------------------------------------------------------------------------
    @staticmethod
    def _cas_violation(p: dict, current_sha: str | None, wanted: str | None) -> str | None:
        """Why the file is not what the plan was built against, or None if it is."""
        path = p["path"]
        if current_sha is None:
            if p.get("expected_sha256"):
                return f"{path} existed when the plan was made but is gone now"
            return None
        if wanted is not None and current_sha == wanted:
            return None
        if p.get("expect_absent"):
            return f"{path} exists now, but this step was approved to create it"
        if current_sha != p.get("expected_sha256"):
            return f"{path} changed since the plan was made; refusing to overwrite it"
        return None

    # -- check -------------------------------------------------------------------------------------------------
    def check(self, ctx):
        p = self.norm(ctx)
        path, parent = p["path"], _parent(p["path"])
        ids = _owner(ctx, p)
        if isinstance(ids, Refuse):
            return ids
        missing = self.unresolved(ctx, p)
        if missing:
            return Refuse(f"no value for secret(s): {', '.join(missing)}")
        parent_stat = ctx.host.lstat(parent)
        if parent_stat is None or parent_stat.kind != "dir":
            return Refuse(f"{parent} does not exist; an earlier step has to create it")
        current = ctx.host.lstat(path)
        if current is None:
            reason = self._cas_violation(p, None, None)
            return Refuse(reason) if reason else Proceed()
        if current.kind != "file":
            return Refuse(f"{path} is a {current.kind}, not a regular file")
        try:
            existing = sha256(ctx.host.read_bytes(path))
        except HostError as exc:
            return Refuse(str(exc))
        wanted = sha256(self.desired(ctx))
        if existing == wanted:
            # The bytes are right; the file is only "as planned" if its mode and owner are too. A config the
            # service user is meant to read, left root:root 0600, would otherwise be reported as success.
            planned_mode = int(p["mode"], 8)
            if current.perms != planned_mode or (current.uid, current.gid) != ids:
                return Refuse(f"{path} has the planned content but is mode {current.perms:04o} owned by "
                              f"{current.uid}:{current.gid}, and the plan says mode {p['mode']} owned by "
                              f"{ids[0]}:{ids[1]} ({p['owner']}:{p['group']}); fix that by hand or remove the file")
            return Satisfied(f"{path} already has the planned content")
        if p["if_exists"] == "keep":
            return Satisfied(f"{path} exists and is kept, not overwritten")
        reason = self._cas_violation(p, existing, wanted)
        return Refuse(reason) if reason else Proceed()

    def unresolved(self, ctx, p) -> list[str]:
        return []

    # -- act ---------------------------------------------------------------------------------------------------
    def act(self, ctx):
        self.write(ctx, self.norm(ctx), self.desired(ctx))

    def write(self, ctx, p: dict, data: bytes) -> None:
        path, parent = p["path"], _parent(p["path"])
        name = path.rsplit("/", 1)[1]
        uid, gid = _owner(ctx, p)
        wanted = sha256(data)
        current = ctx.host.lstat(path)
        previous = sha256(ctx.host.read_bytes(path)) if current is not None else None
        # Re-checked immediately before acting, the compose.write rule: not just at `check`.
        reason = self._cas_violation(p, previous, wanted)
        if reason:
            raise StepFailure(reason)
        record = {"path": path, "created_file": current is None, "previous_sha256": previous,
                  "written_sha256": wanted, "backup": None,
                  "previous_mode": current.perms if current else None,
                  "previous_owner": [current.uid, current.gid] if current else None}
        if current is not None:
            backup = p.get("backup_name") or f"{ctx.step.id}.bak"
            backup_path = f"{ctx.evidence_dir}/{backup}"
            if ctx.host.lstat(ctx.evidence_dir) is None:
                ctx.host.mkdir(ctx.evidence_dir, 0o700, *ctx.evidence_ids)
            if ctx.host.lstat(backup_path) is None:
                ctx.host.copy_private(path, ctx.evidence_dir, backup, *ctx.evidence_ids)
            elif sha256(ctx.host.read_bytes(backup_path)) != previous:
                raise StepFailure(f"{backup_path} exists but does not hold the file being replaced")
            record["backup"] = backup_path
        # The temp file's name is journaled BEFORE the file exists. Recording its identity afterwards cannot
        # close the window between creating it and writing that record; a name chosen up front can, because
        # O_EXCL guarantees that if the create succeeds this step made it, so a crash leaves a name to clean up.
        temp = new_temp_name()
        ctx.evidence(phase="intent", parent=parent, temp=temp, **record)
        try:
            temp, staged = ctx.host.stage_file(parent, data, int(p["mode"], 8), uid, gid, name=temp)
        except HostError as exc:
            raise StepFailure(str(exc)) from exc
        # And its identity, durable BEFORE the rename: later proof that a file at this path is the one this
        # step wrote (device and inode, not content).
        ctx.evidence(phase="staged", parent=parent, temp=temp, staged=staged.identity)
        try:
            self.validate_staged(ctx, parent, temp)
            ctx.host.commit_staged(parent, temp, name)
        except (HostError, StepFailure) as exc:
            try:
                ctx.host.discard_staged(parent, temp)
            except HostError:
                pass
            if isinstance(exc, StepFailure):
                raise
            raise StepFailure(str(exc)) from exc
        ctx.evidence(phase="committed", path=path)
        ctx.log(f"wrote {path} (mode {p['mode']}, {p['owner']}:{p['group']}, sha256 {wanted[:12]})")

    # -- verify / reconcile ------------------------------------------------------------------------------------
    def verify(self, ctx) -> Effect:
        p = self.norm(ctx)
        current = ctx.host.lstat(p["path"])
        if current is None or current.kind != "file":
            return "not_applied"
        try:
            now = sha256(ctx.host.read_bytes(p["path"]))
        except HostError:
            return "unknown"
        if now == sha256(self.desired(ctx)):
            ids = _owner(ctx, p)
            ok = (not isinstance(ids, Refuse) and current.perms == int(p["mode"], 8)
                  and (current.uid, current.gid) == ids)
            return "applied" if ok else "unknown"
        # Not what we wrote. Untouched if it is what we saw before, otherwise something else changed it.
        # Only this file's own evidence: a step that writes several files journals one record per file.
        before = next((e.get("previous_sha256") for e in reversed(ctx.prior_evidence())
                       if "previous_sha256" in e and e.get("path") == p["path"]), p.get("expected_sha256"))
        return "not_applied" if now == before else "unknown"

    def reconcile(self, ctx) -> Effect:
        """After a crash: decide from the host, then tidy a leftover staged file if it is provably ours."""
        outcome = self.verify(ctx)
        entries = ctx.prior_evidence()
        identity = {e["temp"]: e["staged"] for e in entries if e.get("phase") == "staged"}
        for entry in entries:
            if entry.get("phase") not in ("intent", "staged") or "temp" not in entry:
                continue
            parent, temp = entry["parent"], entry["temp"]
            left = ctx.host.lstat(f"{parent}/{temp}")
            if left is None or left.kind != "file" or not is_temp_name(temp):
                continue
            # With the identity journaled, require it. Without (a crash between the intent and the stage
            # record) the exclusive create plus a 48-bit random name is the proof.
            if temp in identity and left.identity != identity[temp]:
                continue
            ctx.host.discard_staged(parent, temp)
            ctx.log(f"removed the leftover staged file {temp}")
        return outcome


    # -- undo --------------------------------------------------------------------------------------------------
    def after_inverse(self, ctx) -> None:
        """Anything the system needs to be told once files are back (a reload); raise StepFailure if it fails."""

    def inverse(self, ctx) -> str:
        """Put back every file this step wrote, newest first. A file is only touched if it is still the one this
        step made (device and inode, then content); anything else means someone changed it since, and undo
        refuses rather than overwrite their work."""
        entries = ctx.prior_evidence()
        for entry in entries:                                    # a previous undo may have died holding a temp file
            if entry.get("phase") == "undo_intent" and is_temp_name(entry["temp"]):
                left = ctx.host.lstat(f"{entry['parent']}/{entry['temp']}")
                if left is not None and left.kind == "file":
                    ctx.host.discard_staged(entry["parent"], entry["temp"])
        intents = [e for e in entries if e.get("phase") == "intent"]
        if not intents:
            return "nothing had been written"
        staged = {e["temp"]: e["staged"] for e in entries if e.get("phase") == "staged"}
        by_path: dict[str, list[dict]] = {}
        for intent in intents:
            by_path.setdefault(intent["path"], []).append(intent)
        committed = {e["path"] for e in entries if e.get("phase") == "committed"}
        notes = [self._undo_path(ctx, path, candidates, staged, committed) for path, candidates in reversed(by_path.items())]
        self.after_inverse(ctx)
        return "; ".join(notes)

    def _identity_unreliable(self, ctx, path: str) -> bool:
        return not ctx.host.stable_inodes(path)

    def _wears_its_stat(self, ctx, path, candidate, current) -> bool:
        return staged_stat_matches(ctx, path, candidate, current, self.norm(ctx))

    def _undo_path(self, ctx, path: str, candidates: list[dict], staged: dict, committed: set) -> str:
        current = ctx.host.lstat(path)
        if current is None:
            if any(c["created_file"] for c in candidates):
                return f"{path} is already gone"
            raise StepFailure(f"{path} was replaced by setup and is missing now; the original is in {candidates[0]['backup']}")
        if current.kind != "file":
            raise StepFailure(f"{path} is a {current.kind} now, not the file setup wrote; leaving it alone")
        try:
            now = sha256(ctx.host.read_bytes(path))
        except HostError as exc:
            raise StepFailure(str(exc)) from exc
        ours = next((c for c in candidates if staged.get(c["temp"]) == current.identity), None)
        if ours is None and path in committed and self._identity_unreliable(ctx, path):
            # FAT renumbers inodes at every mount, and a MOS init script is copied afresh at every boot, so after a
            # reboot identity proves nothing. Fall back to what can still be checked: the bytes setup wrote and
            # the mode and owner it gave them.
            ours = next((c for c in candidates if c["written_sha256"] == now
                         and self._wears_its_stat(ctx, path, c, current)), None)
        if ours is None:
            if any(c["previous_sha256"] == now for c in candidates if c["previous_sha256"]):
                return f"{path} is already back to what it was"
            if path not in committed:
                return f"setup never wrote {path} (an attempt was abandoned before the rename), so it is left alone"
            raise StepFailure(f"{path} is not the file setup wrote; it was changed or replaced since, so it is left as it is")
        if now != ours["written_sha256"]:
            raise StepFailure(f"{path} was edited after setup wrote it; it is left as it is")
        parent = _parent(path)
        if ours["created_file"]:
            ctx.host.unlink(path)
            return f"removed {path}"
        backup = ours.get("backup")
        if not backup or ours.get("previous_mode") is None or not ours.get("previous_owner"):
            raise StepFailure(f"the journal has no complete record of what {path} was; restore it by hand from {backup}")
        try:
            original = ctx.host.read_bytes(backup)
        except HostError as exc:
            raise StepFailure(f"the backup of {path} cannot be read: {exc}") from exc
        if sha256(original) != ours["previous_sha256"]:
            raise StepFailure(f"the backup {backup} does not hold what {path} was; it is not restored over")
        temp = new_temp_name()
        ctx.evidence(phase="undo_intent", parent=parent, temp=temp)
        uid, gid = ours["previous_owner"]
        try:
            ctx.host.stage_file(parent, original, ours["previous_mode"], uid, gid, name=temp)
            ctx.host.commit_staged(parent, temp, path.rsplit("/", 1)[1])
        except HostError as exc:
            try:
                ctx.host.discard_staged(parent, temp)
            except HostError:
                pass
            raise StepFailure(str(exc)) from exc
        return f"restored {path}"


def staged_stat_matches(ctx, path, candidate, current, p: dict) -> bool:
    """On a filesystem without stable inodes: is this file still wearing the mode and owner setup gave it? `p` is
    the step's normalised parameters (a unit or sudoers grant only gets its mode and owner there). A step with no
    fixed mode or owner (a boot hook keeps its file's own) is judged by its bytes alone."""
    if "mode" not in p or "owner" not in p:
        return True
    ids = _owner(ctx, p)
    return current.perms == int(p["mode"], 8) and not isinstance(ids, Refuse) and (current.uid, current.gid) == ids


class FileWrite(_AtomicFile):
    def norm(self, ctx) -> dict:
        return ctx.step.params

    def desired(self, ctx) -> bytes:
        return render(ctx.step.params["content"], ctx.secrets)

    def unresolved(self, ctx, p) -> list[str]:
        return unresolved_secrets(p["content"], ctx.secrets)


class SudoersInstall(_AtomicFile):
    """A sudoers.d grant. Validated with `visudo -c -f` on the staged file BEFORE it is renamed into place: a
    broken file in /etc/sudoers.d can lock every administrator out of sudo, so an invalid one never gets there.
    The staged name contains a dot, which sudo ignores, so the half-written file is never read as policy."""

    def norm(self, ctx) -> dict:
        p = dict(ctx.step.params)
        p.update(mode="0440", owner="root", group="root", has_secrets=False)
        return p

    def desired(self, ctx) -> bytes:
        return ctx.step.params["content"].encode("utf-8")

    def validate_staged(self, ctx, parent, temp):
        result = ctx.host.run(["visudo", "-c", "-f", f"{parent}/{temp}"], timeout=30)
        if result.rc == 127:
            raise StepFailure("visudo was not found, so the grant cannot be validated; is sudo installed?")
        if result.rc != 0:
            raise StepFailure("visudo rejected the sudoers grant, so it was not installed: "
                              + " | ".join((result.err or result.out).strip().splitlines()[-2:])[:300])

    def verify(self, ctx) -> Effect:
        outcome = super().verify(ctx)
        if outcome == "applied" and ctx.host.run(["visudo", "-c"], timeout=30).rc != 0:
            return "unknown"          # the file is ours, but the system's sudoers no longer parses
        return outcome

    def after_inverse(self, ctx) -> None:
        if ctx.host.run(["visudo", "-c"], timeout=30).rc != 0:
            raise StepFailure("the sudoers grant was put back, but the system's sudoers does not parse; check it with visudo")


# ----------------------------------------------------------------------------------------------------------
class DashboardInit(_AtomicFile):
    """The dashboard's env file: a session secret and the first operator. Reuses `dashboard_operators`' pure
    functions (so the format cannot drift from the tool operators later use to add people) and `web_auth`'s
    hashing (so what is written is exactly what the dashboard verifies against). The passphrase is hashed
    here; only the hash is stored. The TOTP secret has to be stored, which is why this file is root-only."""

    KEY = "PE_DASHBOARD_SECRET_KEY"

    def norm(self, ctx) -> dict:
        p = ctx.step.params
        return {"path": p["env_file"], "mode": "0600", "owner": "root", "group": "root", "has_secrets": True,
                "if_exists": "replace", "expect_absent": p.get("expect_absent", False),
                "expected_sha256": p.get("expected_sha256"), "content": ""}

    def _modules(self):
        try:
            import web_auth
            from scripts import dashboard_operators
        except (ImportError, SystemExit) as exc:
            # SystemExit too: an older checkout's dashboard_operators imports `config`, which exits when no config
            # exists yet. A stale checkout is a refusal with a reason, never a crash.
            raise StepFailure(f"the dashboard's own modules could not be imported ({exc}); are the Python "
                              "dependencies installed, and is this checkout up to date?") from exc
        return web_auth, dashboard_operators

    def _operator(self, ctx):
        p = ctx.step.params
        if not p.get("operator"):
            return None
        return p["operator"], ctx.secrets[p["passphrase_ref"]], ctx.secrets[p["totp_ref"]]

    def _merged(self, ctx, text: str, *, key: str) -> str:
        web_auth, ops = self._modules()
        values = ops.parse_env(text)
        if not values.get(self.KEY):
            values[self.KEY] = key
        if len(values[self.KEY]) < 32:
            raise ValueError(f"{self.KEY} must contain at least 32 characters")
        operator = self._operator(ctx)
        if operator is not None and operator[0] not in web_auth.load_operators(values):
            values = ops.apply_operator_change(values, "add", operator[0], passphrase=operator[1], totp_secret=operator[2])
        return ops.render_env(text, values)

    def check(self, ctx):
        p = self.norm(ctx)
        path, parent = p["path"], _parent(p["path"])
        p_ = ctx.step.params
        missing = [r for r in (p_.get("passphrase_ref"), p_.get("totp_ref")) if r and r not in ctx.secrets]
        if missing:
            return Refuse(f"no value for secret(s): {', '.join(missing)}")
        parent_stat = ctx.host.lstat(parent)
        if parent_stat is None or parent_stat.kind != "dir":
            return Refuse(f"{parent} does not exist; an earlier step has to create it")
        current = ctx.host.lstat(path)
        text = ""
        if current is not None:
            if current.kind != "file":
                return Refuse(f"{path} is a {current.kind}, not a regular file")
            try:
                raw = ctx.host.read_bytes(path)
            except HostError as exc:
                return Refuse(str(exc))
            reason = self._cas_violation(p, sha256(raw), None)
            if reason:
                return Refuse(reason)
            text = raw.decode("utf-8", "replace")
        elif p["expected_sha256"]:
            return Refuse(f"{path} existed when the plan was made but is gone now")
        try:
            merged = self._merged(ctx, text, key="x" * 48)
        except StepFailure as exc:
            return Refuse(exc.reason)
        except ValueError as exc:
            return Refuse(str(exc))
        # Satisfied only when nothing would change (key present, operator present) AND the file is as planned.
        if merged == text and current is not None:
            if current.perms != 0o600 or (current.uid, current.gid) != (0, 0):
                return Refuse(f"{path} is already as planned but is mode {current.perms:04o} owned by "
                              f"{current.uid}:{current.gid}; it holds secrets and has to be 0600 root:root")
            return Satisfied(f"{path} already has the session secret and the operator")
        return Proceed()

    def act(self, ctx):
        p = self.norm(ctx)
        path = p["path"]
        current = ctx.host.lstat(path)
        text = ctx.host.read_bytes(path).decode("utf-8", "replace") if current is not None else ""
        import secrets as _secrets
        try:
            merged = self._merged(ctx, text, key=_secrets.token_urlsafe(48))
        except ValueError as exc:
            raise StepFailure(str(exc)) from exc
        self.write(ctx, p, merged.encode("utf-8"))
        operator = self._operator(ctx)
        if operator is not None:
            ctx.log(f"added the dashboard operator '{operator[0]}' (the passphrase is stored only as a hash)")

    def verify(self, ctx) -> Effect:
        p = self.norm(ctx)
        current = ctx.host.lstat(p["path"])
        if current is None or current.kind != "file":
            return "not_applied"
        try:
            web_auth, ops = self._modules()
            values = ops.parse_env(ctx.host.read_bytes(p["path"]).decode("utf-8", "replace"))
            operators = web_auth.load_operators(values)
        except (HostError, ValueError, StepFailure):
            return "unknown"
        if len(values.get(self.KEY, "")) < 32:
            return "not_applied"
        operator = self._operator(ctx)
        if operator is not None:
            found = operators.get(operator[0])
            if (found is None or not web_auth.verify_passphrase(found.passphrase_hash, operator[1])
                    or found.totp_secret != operator[2]):
                return "not_applied"
        return "applied" if current.perms == 0o600 and (current.uid, current.gid) == (0, 0) else "unknown"


# ----------------------------------------------------------------------------------------------------------
class AccessProvision:
    """The dashboard's own account, and exactly the read access it needs. The commands are not invented here:
    systemd hosts get the ones `scripts/web_access.py` plans (pure, already reviewed), run directly because we
    are root; MOS has no ACLs, so it gets the account plus readable code and a traversable home, while data,
    logs and the env files stay private by the modes the earlier steps gave them."""

    def _commands(self, ctx) -> list[list[str]]:
        p = ctx.step.params
        host = ctx.host
        user_exists = lambda name: host.lookup_user(name) is not None
        group_exists = lambda name: host.lookup_group(name) is not None

        def in_group(user, group):
            groups, gid = host.user_groups(user), host.lookup_group(group)
            return groups is not None and gid is not None and gid in groups

        if p["method"] == "acl":
            try:
                from scripts.web_access import plan_web_access
            except ImportError as exc:
                raise StepFailure(f"scripts/web_access.py could not be imported: {exc}") from exc

            def other_can_traverse(path):
                found = host.lstat(str(path))
                return found is not None and bool(found.perms & 0o001)
            try:
                return plan_web_access(
                    p["install_dir"], p["run_user"], p["web_user"], p["rpc_group"],
                    user_exists=user_exists, group_exists=group_exists, in_group=in_group,
                    other_can_traverse=other_can_traverse, entries=host.listdir(p["install_dir"]),
                    config_file=p.get("config_path"))
            except ValueError as exc:
                raise StepFailure(str(exc)) from exc

        web, rpc = p["web_user"], p["rpc_group"]
        commands = []
        if not group_exists(rpc):
            commands.append(["groupadd", "--system", rpc])
        if not user_exists(web):
            commands.append(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent",
                             "--shell", "/usr/sbin/nologin", "--user-group", web])
        for user in (p["run_user"], web):
            if not in_group(user, rpc):
                commands.append(["usermod", "-aG", rpc, user])
        # No ACLs here, so the same allowlist is expressed in mode bits: what the dashboard serves is made
        # readable, and every other top-level entry (untracked env files, .git, data, logs) loses its "other"
        # bits. It is the same rule web_access.py applies with a deny ACL, so a credential file left beside the
        # code is never readable by the dashboard's account.
        try:
            from scripts.web_access import READABLE_DIRS, STATE_DIR
        except ImportError as exc:
            raise StepFailure(f"scripts/web_access.py could not be imported: {exc}") from exc
        install = p["install_dir"]
        commands.append(["chmod", "a+rx", install])
        for entry in sorted(host.listdir(install)):
            path = f"{install}/{entry}"
            if entry == STATE_DIR or entry in READABLE_DIRS or entry.endswith(".py"):
                commands.append(["chmod", "-R", "a+rX", path])
            else:
                commands.append(["chmod", "-R", "o-rwx", path])
        commands += [["chmod", "-R", "a+rX", p["venv_dir"]], ["chmod", "o+x", p["home_dir"]]]
        return commands

    def check(self, ctx):
        p = ctx.step.params
        if ctx.host.lookup_user(p["run_user"]) is None:
            return Refuse(f"no such user: {p['run_user']}")
        if p["method"] == "acl" and ctx.host.run(["setfacl", "--version"], timeout=15).rc != 0:
            return Refuse("setfacl was not found; install the acl package (for example `apt install acl`) and re-run")
        try:
            self._commands(ctx)
        except StepFailure as exc:
            return Refuse(exc.reason)
        return Proceed()          # every command is safe to repeat, so there is no "already done" short cut

    def act(self, ctx):
        for command in self._commands(ctx):
            ctx.evidence(command=command)
            result = ctx.host.run(command, timeout=300)
            if result.rc != 0:
                tail = " | ".join((result.err or result.out).strip().splitlines()[-2:])[:300]
                raise StepFailure(f"`{command[0]}` failed (exit {result.rc}): {tail}", "unknown")
            ctx.log(f"ran {command[0]} {command[1] if len(command) > 1 else ''}".rstrip())

    _ACL_FORMS: ClassVar[dict] = {"---": {"---"}, "x": {"--x"}, "rx": {"r-x"}, "rX": {"r-x", "r--"}, "r": {"r--"}}

    def verify(self, ctx) -> Effect:
        """Every grant the plan makes, read back: a crash between two commands must not look finished."""
        p = ctx.step.params
        web, rpc = p["web_user"], p["rpc_group"]
        gid = ctx.host.lookup_group(rpc)
        if ctx.host.lookup_user(web) is None or gid is None:
            return "not_applied"
        for user in (p["run_user"], web):
            groups = ctx.host.user_groups(user)
            if groups is None or gid not in groups:
                return "not_applied"
        try:
            commands = self._commands(ctx)
        except (StepFailure, HostError):
            return "unknown"
        if p["method"] == "acl":
            expected = {}
            for command in commands:
                if command[0] == "setfacl" and "-d" not in command and "-m" in command:
                    expected[command[-1]] = command[command.index("-m") + 1].split(":")[-1]   # the last one wins
            for path, perm in expected.items():
                shown = ctx.host.run(["getfacl", "-p", "--omit-header", path], timeout=15)
                entries = {line.split("#")[0].strip() for line in shown.out.splitlines()}
                if shown.rc != 0 or not any(f"user:{web}:{form}" in entries for form in self._ACL_FORMS.get(perm, {perm})):
                    return "not_applied"
            return "applied"
        # groups: the commands are chmods; judge the mode bits they were meant to produce.
        for command in commands:
            if command[0] != "chmod":
                continue
            path = command[-1]
            found = ctx.host.lstat(path)
            if found is None:
                return "not_applied"
            if command[1:-1] == ["o-rwx"] or command[1:-1] == ["-R", "o-rwx"]:
                if found.perms & 0o007:
                    return "not_applied"
            elif found.kind == "dir" and not found.perms & 0o001 or (found.kind == "file" and not found.perms & 0o004):
                return "not_applied"
        return "applied"

    reconcile = verify

    def inverse(self, ctx) -> str:
        raise Irreversible("the dashboard account, the group memberships and the read grants are left in place; "
                           "on their own they give nobody access to anything private")


# ----------------------------------------------------------------------------------------------------------
class VerifySmoke:
    """Leela's status scan, once, as the service user. Read-only: it changes nothing."""

    def check(self, ctx):
        p = ctx.step.params
        if ctx.host.lstat(f"{p['venv_dir']}/bin/python") is None:
            return Refuse(f"{p['venv_dir']}/bin/python does not exist; the environment step has to run first")
        if ctx.host.lookup_user(p["run_user"]) is None:
            return Refuse(f"no such user: {p['run_user']}")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        result = ctx.host.run([f"{p['venv_dir']}/bin/python", "casa_leela.py", "--status"], timeout=120,
                              as_user=p["run_user"], env=dict(p["env"]), cwd=p["install_dir"])
        if result.rc != 0:
            tail = (result.err or result.out).strip().splitlines()[-3:]
            raise StepFailure(f"the status scan failed (exit {result.rc}): {' | '.join(tail)[:300]}")
        try:
            report = json.loads(result.out)
        except json.JSONDecodeError as exc:
            raise StepFailure("the status scan did not return JSON") from exc
        if not isinstance(report, dict) or "containers" not in report:
            raise StepFailure("the status scan returned no container list")
        ctx.log(f"Leela saw {len(report['containers'])} container(s)")

    def verify(self, ctx) -> Effect:
        return "not_applied"          # a read-only check: success means nothing changed

    reconcile = verify

    def inverse(self, ctx) -> str:
        raise Irreversible("it only reads")


# ----------------------------------------------------------------------------------------------------------
class StateSnapshot:
    """The safety net before changing an install that already has state: `scripts/state_snapshot.py`, which is
    standard-library only (so it needs no virtualenv) and is what deploy.sh runs."""

    def check(self, ctx):
        p = ctx.step.params
        script = f"{p['install_dir']}/scripts/state_snapshot.py"
        found = ctx.host.lstat(script)
        if found is None or found.kind != "file":
            return Refuse(f"{script} is missing, so no snapshot can be taken; the checkout is incomplete")
        if ctx.host.lookup_user(p["run_user"]) is None:
            return Refuse(f"no such user: {p['run_user']}")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        result = ctx.host.run(["python3", f"{p['install_dir']}/scripts/state_snapshot.py", "create", "--label", p["label"]],
                              timeout=300, as_user=p["run_user"], env=dict(p["env"]), cwd=p["install_dir"])
        if result.rc != 0:
            tail = (result.err or result.out).strip().splitlines()[-2:]
            raise StepFailure(f"the snapshot failed (exit {result.rc}): {' | '.join(tail)[:300]}", "unknown")
        lines = [line for line in result.out.splitlines() if line.strip()]
        path = lines[-1].strip() if lines else ""
        data_dir = p["env"].get("CASA_DATA_DIR", "")
        # The script prints the new snapshot's path. Trust it only if it is where snapshots live.
        # Compared as a normalised path: `.../snapshots/../../elsewhere` starts with the right text and is not it.
        if (not data_dir or posixpath.normpath(path) != path or posixpath.normpath(data_dir) != data_dir
                or not path.startswith(f"{data_dir}/snapshots/")):
            raise StepFailure("the snapshot script reported a location outside the data directory", "unknown")
        ctx.evidence(snapshot=path)
        ctx.log(f"snapshot taken at {path}")

    def verify(self, ctx) -> Effect:
        taken = [e["snapshot"] for e in ctx.prior_evidence() if "snapshot" in e]
        if not taken:
            return "not_applied"
        made = ctx.host.lstat(taken[-1])
        manifest = ctx.host.lstat(f"{taken[-1]}/manifest.json")
        return "applied" if made is not None and made.kind == "dir" and manifest is not None else "unknown"

    # After a crash with no record of a snapshot, taking another is harmless: names are timestamped.
    reconcile = verify

    def inverse(self, ctx) -> str:
        raise Irreversible("the snapshot is the safety net, so it is kept")


# ----------------------------------------------------------------------------------------------------------
GET_PIP_URL = "https://raw.githubusercontent.com/pypa/get-pip/af54dfe793b24685f8dc4ebba0630d9f2d77653c/public/get-pip.py"          # an immutable commit, not the moving bootstrap.pypa.io copy
GET_PIP_SHA256 = "fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6"
# Run by the venv's own python: version floor, then each requirement installed and at least the named version.
_CHECK_ENV = (
    "import sys, json, re\n"
    "from importlib import metadata\n"
    "if sys.version_info < (3, 11): sys.exit(2)\n"
    "def num(v): return tuple(int(x) for x in re.findall(r'\\d+', v.split('+')[0])[:4])\n"
    "for line in json.loads(sys.argv[1]):\n"
    "    m = re.fullmatch(r'([A-Za-z0-9_.-]+)\\s*(?:(>=|==)\\s*([0-9][^,;\\s]*))?', line)\n"
    "    if not m: continue\n"
    "    try: have = metadata.version(m.group(1))\n"
    "    except metadata.PackageNotFoundError: sys.exit(3)\n"
    "    if m.group(2) == '>=' and num(have) < num(m.group(3)): sys.exit(4)\n"
    "    if m.group(2) == '==' and num(have) != num(m.group(3)): sys.exit(4)\n"
)
_FETCH = ("import hashlib, sys, urllib.request\n"
          "data = urllib.request.urlopen(sys.argv[1], timeout=60).read()\n"
          "if hashlib.sha256(data).hexdigest() != sys.argv[3]: sys.exit('the pip installer does not match its pinned digest')\n"
          "open(sys.argv[2], 'wb').write(data)\n")


class PythonEnv:
    """A virtualenv for the install, built by the service user (as deploy.sh does) with a fixed umask, so the
    dashboard's own account can read what it must run from. Setup installs; it does not upgrade, so an
    environment that already imports its dependencies is left alone."""

    def _python(self, p) -> str:
        return f"{p['venv_dir']}/bin/python"

    def _imports_ok(self, ctx) -> bool:
        """The venv's own interpreter is new enough and every package in requirements.txt is installed at (at
        least) the version it names. Judged by the venv, not by the system python that would build a new one."""
        p = ctx.step.params
        if ctx.host.lstat(self._python(p)) is None:
            return False
        try:
            wanted = ctx.host.read_bytes(f"{p['install_dir']}/{p['requirements']}").decode("utf-8")
        except (HostError, UnicodeDecodeError):
            return False
        lines = [line.split("#")[0].strip() for line in wanted.splitlines()]
        done = ctx.host.run([self._python(p), "-c", _CHECK_ENV, json.dumps([x for x in lines if x])],
                            as_user=p["run_user"], timeout=60)
        return done.rc == 0

    def check(self, ctx):
        p = ctx.step.params
        requirements = f"{p['install_dir']}/{p['requirements']}"
        found = ctx.host.lstat(requirements)
        if found is None or found.kind != "file":
            return Refuse(f"{requirements} is missing")
        if ctx.host.lookup_user(p["run_user"]) is None:
            return Refuse(f"no such user: {p['run_user']}")
        probe = ctx.host.run(["python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
                             as_user=p["run_user"], timeout=30)
        if probe.rc == 127:
            return Refuse("python3 was not found")
        if probe.rc != 0:
            return Refuse("python3 is older than 3.11, which this project needs")
        if self._imports_ok(ctx):
            return Satisfied(f"{p['venv_dir']} already has its dependencies")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        python, user, cwd = self._python(p), p["run_user"], p["install_dir"]
        created = ctx.host.lstat(python) is None
        directory_was_new = ctx.host.lstat(p["venv_dir"]) is None
        if created:
            ctx.evidence(phase="creating_venv", venv_dir=p["venv_dir"], directory_was_new=directory_was_new)
            args = ["python3", "-m", "venv"] + (["--without-pip"] if p["bootstrap_pip"] else []) + [p["venv_dir"]]
            made = ctx.host.run(args, as_user=user, cwd=cwd, timeout=300, umask=0o022)
            if made.rc != 0:
                raise StepFailure(f"creating the virtualenv failed: {self._tail(made)}", "unknown")
            top = ctx.host.lstat(p["venv_dir"])
            if top is not None and directory_was_new:
                # Only a directory that did not exist before is ours to remove later; one that was already
                # there (empty, say) is not, whatever `venv` put into it.
                ctx.evidence(created_venv=p["venv_dir"], **top.identity)
            ctx.log(f"created the virtualenv {p['venv_dir']}")
        if p["bootstrap_pip"] and ctx.host.run([python, "-m", "pip", "--version"], as_user=user, timeout=30).rc != 0:
            script = f"{p['venv_dir']}/get-pip.py"
            got = ctx.host.run(["python3", "-c", _FETCH, GET_PIP_URL, script, GET_PIP_SHA256], as_user=user, cwd=cwd, timeout=120)
            if got.rc != 0:
                raise StepFailure(f"fetching pip failed (does this host have internet access?): {self._tail(got)}", "unknown")
            installed = ctx.host.run([python, script, "--quiet", "--disable-pip-version-check"],
                                     as_user=user, cwd=cwd, timeout=300, umask=0o022)
            ctx.host.unlink(script)
            if installed.rc != 0:
                raise StepFailure(f"installing pip failed: {self._tail(installed)}", "unknown")
            ctx.log("bootstrapped pip into the virtualenv")
        deps = ctx.host.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
                             f"{p['install_dir']}/{p['requirements']}"], as_user=user, cwd=cwd, timeout=900, umask=0o022)
        if deps.rc != 0:
            raise StepFailure(f"installing the requirements failed: {self._tail(deps)}", "unknown")
        ctx.log("installed the requirements")
        if any("created_venv" in e for e in ctx.prior_evidence()):
            try:
                ctx.evidence(venv_digest=ctx.host.tree_digest(p["venv_dir"]))    # the proof undo will need
            except HostError:
                pass                                                              # undo then refuses to delete it

    @staticmethod
    def _tail(result) -> str:
        return " | ".join((result.err or result.out).strip().splitlines()[-2:])[:300]

    def verify(self, ctx) -> Effect:
        return "applied" if self._imports_ok(ctx) else "not_applied"

    reconcile = verify

    def inverse(self, ctx) -> str:
        made = [e for e in ctx.prior_evidence() if "created_venv" in e]
        if not made:
            raise Irreversible("setup did not create this environment, so it leaves it (and what was installed into it) alone")
        entry = made[-1]
        found = ctx.host.lstat(entry["created_venv"])
        if found is None:
            return f"{entry['created_venv']} is already gone"
        recorded = [e["venv_digest"] for e in ctx.prior_evidence() if "venv_digest" in e]
        if not recorded:
            raise StepFailure(f"setup has no record of how {entry['created_venv']} looked when it finished building it, "
                              "so it is not deleted")
        try:
            now = ctx.host.tree_digest(entry["created_venv"])
        except HostError as exc:
            raise StepFailure(str(exc)) from exc
        if now != recorded[-1]:
            raise StepFailure(f"{entry['created_venv']} has changed since setup built it (files or packages added or "
                              "removed), so it is left in place rather than deleted")
        try:
            ctx.host.remove_tree(entry["created_venv"], {"dev": entry["dev"], "ino": entry["ino"]})
        except HostError as exc:
            raise StepFailure(str(exc)) from exc
        return f"removed the virtualenv {entry['created_venv']}"


# ----------------------------------------------------------------------------------------------------------
class ServiceInstall(_AtomicFile):
    """A unit file (systemd) or init script (sysvinit), written like any other file. systemd then re-reads
    its units. On sysvinit the script's /etc/default file is written first so the script never exists without
    the paths it needs; an existing, different /etc/default file is never overwritten here."""

    def norm(self, ctx) -> dict:
        p = dict(ctx.step.params)
        p.update(mode="0755" if p["flavour"] == "sysvinit" else "0644", owner="root", group="root", has_secrets=False)
        return p

    def desired(self, ctx) -> bytes:
        return ctx.step.params["content"].encode("utf-8")

    def _defaults(self, ctx) -> dict | None:
        p = ctx.step.params
        if not p.get("defaults_path"):
            return None
        return {"path": p["defaults_path"], "mode": "0644", "owner": "root", "group": "root", "has_secrets": False,
                "if_exists": p.get("defaults_if_exists", "keep"), "expect_absent": p.get("defaults_expect_absent", False),
                "expected_sha256": p.get("defaults_expected_sha256"), "backup_name": f"{ctx.step.id}.defaults.bak"}

    def _defaults_todo(self, ctx):
        """(needs_write, Refuse or None) for the /etc/default file."""
        d = self._defaults(ctx)
        wanted = sha256(ctx.step.params["defaults_content"].encode("utf-8"))
        current = ctx.host.lstat(d["path"])
        if current is None:
            reason = self._cas_violation(d, None, wanted)
            return (False, Refuse(reason)) if reason else (True, None)
        if current.kind != "file":
            return False, Refuse(f"{d['path']} is a {current.kind}, not a regular file")
        try:
            have = sha256(ctx.host.read_bytes(d["path"]))
        except HostError as exc:
            return False, Refuse(str(exc))
        if have == wanted:
            # Right bytes are not enough: both init scripts source this file as root, so a copy that someone
            # else can write is a way to run their commands at service start.
            if current.perms != 0o644 or (current.uid, current.gid) != (0, 0):
                return False, Refuse(f"{d['path']} has the planned content but is mode {current.perms:04o} owned by "
                                     f"{current.uid}:{current.gid}; it must be 0644 root:root. Fix that by hand or remove the file")
            return False, None
        if d["if_exists"] == "keep":
            return False, None
        reason = self._cas_violation(d, have, wanted)
        return (False, Refuse(reason)) if reason else (True, None)

    def _systemd_state(self, ctx) -> tuple[str, bool] | None:
        """(LoadState, NeedDaemonReload) as systemd reports them, or None if it cannot be asked."""
        shown = ctx.host.run(["systemctl", "show", "-p", "LoadState", "-p", "NeedDaemonReload", ctx.step.params["name"]],
                             timeout=30)
        if shown.rc != 0:
            return None
        values = dict(line.split("=", 1) for line in shown.out.splitlines() if "=" in line)
        return values.get("LoadState", ""), values.get("NeedDaemonReload", "no").strip() == "yes"

    def check(self, ctx):
        outcome = super().check(ctx)
        if isinstance(outcome, Refuse):
            return outcome
        if self._defaults(ctx) is not None:
            needed, refusal = self._defaults_todo(ctx)
            if refusal:
                return refusal
            if needed and isinstance(outcome, Satisfied):
                outcome = Proceed()
        if ctx.step.params["flavour"] == "systemd" and isinstance(outcome, Satisfied):
            # The unit is on disk; whether systemd has read it is a separate fact (a crash or a failed reload
            # between the two must not be remembered as done).
            state = self._systemd_state(ctx)
            if state is not None and state[1]:
                return Proceed()
            if state is not None and state[0] != "loaded":
                return Refuse(f"systemd cannot load {ctx.step.params['name']} (LoadState={state[0] or 'unknown'}); "
                              "fix the unit before continuing")
        return outcome

    def act(self, ctx):
        p = ctx.step.params
        d = self._defaults(ctx)
        if d is not None:
            data = p["defaults_content"].encode("utf-8")
            needed, refusal = self._defaults_todo(ctx)
            if refusal:
                raise StepFailure(refusal.reason)
            if needed:
                self.write(ctx, d, data)
        present = sha256(ctx.host.read_bytes(p["path"])) if ctx.host.lstat(p["path"]) else None
        if present != sha256(self.desired(ctx)):          # on a resume the script may already be in place
            super().act(ctx)
        if p["flavour"] == "systemd":
            result = ctx.host.run(["systemctl", "daemon-reload"], timeout=60)
            if result.rc != 0:
                raise StepFailure("the unit was written but systemd would not reload: "
                                  + " | ".join((result.err or result.out).strip().splitlines()[-2:])[:300], "unknown")

    def _identity_unreliable(self, ctx, path: str) -> bool:
        # MOS copies its init scripts out of the persistent install at every boot: same bytes, a new file each time.
        return ctx.step.params["flavour"] == "sysvinit" or super()._identity_unreliable(ctx, path)

    def _wears_its_stat(self, ctx, path, candidate, current) -> bool:
        return ctx.step.params["flavour"] == "sysvinit" or super()._wears_its_stat(ctx, path, candidate, current)

    def after_inverse(self, ctx) -> None:
        if ctx.step.params["flavour"] == "systemd" and ctx.host.run(["systemctl", "daemon-reload"], timeout=60).rc != 0:
            raise StepFailure("the unit file was removed, but systemd would not reload; run `systemctl daemon-reload`")

    def verify(self, ctx) -> Effect:
        outcome = super().verify(ctx)
        d = self._defaults(ctx)
        if outcome == "applied" and ctx.step.params["flavour"] == "systemd":
            state = self._systemd_state(ctx)
            if state is None:
                return "unknown"
            if state[1] or state[0] != "loaded":
                return "not_applied"
        if outcome == "applied" and d is not None:
            current = ctx.host.lstat(d["path"])
            if current is None:
                return "not_applied"
            wrong_metadata = current.perms != 0o644 or (current.uid, current.gid) != (0, 0)
            if wrong_metadata and (d["if_exists"] == "replace" or sha256(ctx.host.read_bytes(d["path"])) ==
                                   sha256(ctx.step.params["defaults_content"].encode("utf-8"))):
                return "unknown"
            if d["if_exists"] == "replace" and sha256(ctx.host.read_bytes(d["path"])) != \
                    sha256(ctx.step.params["defaults_content"].encode("utf-8")):
                return "not_applied"
        return outcome


# ----------------------------------------------------------------------------------------------------------
def _block(marker: str, body: str) -> str:
    return f"# BEGIN {marker} (managed by Planet Express setup; edits inside this block are overwritten)\n" \
           f"{body.rstrip(chr(10))}\n# END {marker}\n"


def merge_block(current: str | None, marker: str, body: str) -> str:
    """`current` with the marked block present exactly once and everything else untouched. A file that does not
    exist yet becomes a minimal script. Half a block (a BEGIN without its END) is refused, never guessed at."""
    block = _block(marker, body)
    if current is None:
        return "#!/bin/sh\n" + block
    begin = re.compile(rf"^# BEGIN {re.escape(marker)}\b.*\n", re.MULTILINE)
    end = re.compile(rf"^# END {re.escape(marker)}\s*\n?", re.MULTILINE)
    starts, ends = list(begin.finditer(current)), list(end.finditer(current))
    if not starts and not ends:
        if current and not current.endswith("\n"):
            current += "\n"
        return current + ("\n" if current else "") + block
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() < starts[0].start():
        raise StepFailure(f"the {marker} markers in this file are not one BEGIN followed by one END; fix them by hand")
    return current[: starts[0].start()] + block + current[ends[0].end():]


class _HookFile(_AtomicFile):
    def __init__(self, outer, name):
        self.outer, self.name = outer, name

    def norm(self, ctx) -> dict:
        step = ctx.step.params
        path = f"{step['dest_dir']}/{self.name}"
        current = ctx.host.lstat(path)
        return {"path": path, "mode": f"{current.perms:04o}" if current and current.kind == "file" else "0600",
                "owner": "root", "group": "root", "has_secrets": False, "if_exists": "replace",
                "expect_absent": self.name in step["expect_absent"],
                "expected_sha256": step["expected_sha256"].get(self.name),
                "backup_name": f"{ctx.step.id}.{self.name}.bak"}

    def _current(self, ctx) -> str | None:
        path = f"{ctx.step.params['dest_dir']}/{self.name}"
        if ctx.host.lstat(path) is None:
            return None
        try:
            return ctx.host.read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StepFailure(f"{path} is not text, so nothing can be merged into it") from exc

    def desired(self, ctx) -> bytes:
        step = ctx.step.params
        path = f"{step['dest_dir']}/{self.name}"
        current = self._current(ctx)
        is_ours = (current is not None and ctx.host.lstat(path).kind == "file"
                   and sha256(current.encode("utf-8")) in step["legacy_sha256"].get(self.name, []))
        if is_ours:
            current = None                     # one of our own whole-file copies: replaced, not merged into
        return merge_block(current, step["marker"], step["hooks"][self.name]).encode("utf-8")

    def check(self, ctx):
        try:
            return super().check(ctx)
        except StepFailure as exc:
            return Refuse(exc.reason)


class BootHookInstall:
    """Merge a marked block into each boot hook file. The operator's own commands survive; a file that is
    byte-for-byte one of this project's old whole-file copies is replaced. Every file is compare-and-swapped
    against the hash the plan saw."""

    def _files(self, ctx):
        return [_HookFile(self, n) for n in ctx.step.params["hooks"]]

    def check(self, ctx):
        outcomes = [f.check(ctx) for f in self._files(ctx)]
        for outcome in outcomes:
            if isinstance(outcome, Refuse):
                return outcome
        if all(isinstance(o, Satisfied) for o in outcomes):
            return Satisfied("the boot hooks already carry the Planet Express block")
        return Proceed()

    def act(self, ctx):
        for hook in self._files(ctx):
            outcome = hook.check(ctx)
            if isinstance(outcome, Refuse):
                raise StepFailure(outcome.reason)
            if isinstance(outcome, Proceed):
                hook.act(ctx)

    def inverse(self, ctx) -> str:
        # Every hook file is a path in this step's evidence, so one pass puts them all back (or refuses).
        return self._files(ctx)[0].inverse(ctx)

    def verify(self, ctx) -> Effect:
        results = {f.verify(ctx) for f in self._files(ctx)}
        if results == {"applied"}:
            return "applied"
        return "not_applied" if results == {"not_applied"} else "unknown"

    def reconcile(self, ctx) -> Effect:
        results = {f.reconcile(ctx) for f in self._files(ctx)}
        if results == {"applied"}:
            return "applied"
        # Some hooks done and some not is the ordinary shape of a crash between two renames. Re-running is safe:
        # a finished hook is satisfied and a pending one is still guarded by its compare-and-swap.
        return "not_applied" if results <= {"applied", "not_applied"} else "unknown"


# ----------------------------------------------------------------------------------------------------------
class ServiceEnable:
    """Make the service start at boot, and start it now if the plan says so. On sysvinit (MOS) boot start is
    the boot hook's job, so enabling is only starting. State is read from the init system, not from exit codes
    of the commands that changed it."""

    @staticmethod
    def _unit(p) -> str:
        return f"/etc/systemd/system/{p['name']}.service" if p["flavour"] == "systemd" else f"/etc/init.d/{p['name']}"

    def _enabled(self, ctx, p) -> bool:
        if p["flavour"] != "systemd":
            return True
        done = ctx.host.run(["systemctl", "is-enabled", p["name"]], timeout=30)
        return done.rc == 0 and done.out.strip() in ("enabled", "enabled-runtime")

    def _running(self, ctx, p) -> bool:
        if p["flavour"] == "systemd":
            return ctx.host.run(["systemctl", "is-active", p["name"]], timeout=30).out.strip() == "active"
        return ctx.host.run([f"/etc/init.d/{p['name']}", "status"], timeout=30).rc == 0

    def check(self, ctx):
        p = ctx.step.params
        if ctx.host.lstat(self._unit(p)) is None:
            return Refuse(f"{self._unit(p)} does not exist; the install step has to run first")
        if self._enabled(ctx, p) and (not p["start"] or self._running(ctx, p)):
            return Satisfied(f"{p['name']} is already " + ("enabled and running" if p["start"] else "enabled"))
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        if not self._enabled(ctx, p):
            ctx.evidence(enabling=p["name"])
            done = ctx.host.run(["systemctl", "enable", p["name"]], timeout=60)
            if done.rc != 0:
                raise StepFailure(f"enabling {p['name']} failed: "
                                  + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300], "unknown")
            if self._enabled(ctx, p):
                ctx.evidence(enabled_done=p["name"])
        if p["start"] and not self._running(ctx, p):
            ctx.evidence(starting=p["name"])
            argv = ["systemctl", "start", p["name"]] if p["flavour"] == "systemd" else [f"/etc/init.d/{p['name']}", "start"]
            done = ctx.host.run(argv, timeout=120)
            if done.rc != 0:
                raise StepFailure(f"starting {p['name']} failed: "
                                  + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300], "unknown")
            if self._running(ctx, p):
                ctx.evidence(started_done=p["name"])

    def verify(self, ctx) -> Effect:
        p = ctx.step.params
        return "applied" if self._enabled(ctx, p) and (not p["start"] or self._running(ctx, p)) else "not_applied"

    def reconcile(self, ctx) -> Effect:
        """After a crash the host decides; if it shows the transition this step began, that becomes the proof
        undo needs (the process died between the command and writing it down)."""
        outcome = self.verify(ctx)
        p = ctx.step.params
        entries = ctx.prior_evidence()
        if any("enabling" in e for e in entries) and not any("enabled_done" in e for e in entries) and self._enabled(ctx, p):
            ctx.evidence(enabled_done=p["name"])
        if any("starting" in e for e in entries) and not any("started_done" in e for e in entries) and self._running(ctx, p):
            ctx.evidence(started_done=p["name"])
        return outcome

    def inverse(self, ctx) -> str:
        """Stop what this step started and disable what it enabled; a service that was already enabled or
        running before setup is left exactly so."""
        p = ctx.step.params
        entries = ctx.prior_evidence()
        notes = []
        if any("started_done" in e for e in entries):
            argv = ["systemctl", "stop", p["name"]] if p["flavour"] == "systemd" else [f"/etc/init.d/{p['name']}", "stop"]
            done = ctx.host.run(argv, timeout=120)
            if done.rc != 0 and self._running(ctx, p):
                raise StepFailure(f"stopping {p['name']} failed: " + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300])
            notes.append(f"stopped {p['name']}")
        if p["flavour"] == "systemd" and any("enabled_done" in e for e in entries):
            done = ctx.host.run(["systemctl", "disable", p["name"]], timeout=60)
            if done.rc != 0 and self._enabled(ctx, p):
                raise StepFailure(f"disabling {p['name']} failed: " + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300])
            notes.append(f"disabled {p['name']}")
        if not notes:
            raise Irreversible(f"{p['name']} was already set up before setup ran, so it is left as it was")
        return "; ".join(notes)


# ----------------------------------------------------------------------------------------------------------
# Removal (uninstall). Every removal is compare-and-swapped against the hash the plan saw, keeps a private
# copy first, journals its intent before acting, and can be put back by `inverse` if nothing has moved since.
def _backup_name(ctx, path: str) -> str:
    return f"{ctx.step.id}.{path.strip('/').replace('/', '_')}.rm.bak"


def _remove_with_backup(ctx, path: str, expected: str) -> None:
    """Unlink `path` if it is a regular file hashing to `expected`, after copying it into the evidence directory."""
    current = ctx.host.lstat(path)
    if current is None:
        return
    if current.kind != "file":
        raise StepFailure(f"{path} is a {current.kind}, not a regular file")
    try:
        found = sha256(ctx.host.read_bytes(path))
    except HostError as exc:
        raise StepFailure(str(exc)) from exc
    if found != expected:
        raise StepFailure(f"{path} changed since the plan was made; it is not removed")
    backup = f"{ctx.evidence_dir}/{_backup_name(ctx, path)}"
    if ctx.host.lstat(ctx.evidence_dir) is None:
        ctx.host.mkdir(ctx.evidence_dir, 0o700, *ctx.evidence_ids)
    if ctx.host.lstat(backup) is None:
        ctx.host.copy_private(path, ctx.evidence_dir, backup.rsplit("/", 1)[1], *ctx.evidence_ids)
    elif sha256(ctx.host.read_bytes(backup)) != expected:
        raise StepFailure(f"{backup} exists but does not hold the file being removed")
    ctx.evidence(phase="removing", path=path, previous_sha256=expected, previous_mode=current.perms,
                 previous_owner=[current.uid, current.gid], backup=backup)
    ctx.host.unlink(path)
    ctx.evidence(phase="removed", path=path)
    ctx.log(f"removed {path} (a private copy is kept for undo)")


def _restore_removed(ctx, entry: dict) -> str:
    path = entry["path"]
    current = ctx.host.lstat(path)
    if current is not None:
        try:
            if current.kind == "file" and sha256(ctx.host.read_bytes(path)) == entry["previous_sha256"]:
                return f"{path} is already back"
        except HostError:
            pass
        raise StepFailure(f"something else is at {path} now, so the removed file is not put back over it "
                          f"(its copy is in {entry['backup']})")
    try:
        data = ctx.host.read_bytes(entry["backup"])
    except HostError as exc:
        raise StepFailure(f"the copy of {path} cannot be read: {exc}") from exc
    if sha256(data) != entry["previous_sha256"]:
        raise StepFailure(f"the copy {entry['backup']} does not hold what {path} was; it is not restored")
    parent, temp = _parent(path), new_temp_name()
    ctx.evidence(phase="undo_intent", parent=parent, temp=temp)
    uid, gid = entry["previous_owner"]
    try:
        ctx.host.stage_file(parent, data, entry["previous_mode"], uid, gid, name=temp)
        ctx.host.commit_staged(parent, temp, path.rsplit("/", 1)[1])
    except HostError as exc:
        try:
            ctx.host.discard_staged(parent, temp)
        except HostError:
            pass
        raise StepFailure(str(exc)) from exc
    return f"restored {path}"


def _undo_removals(ctx) -> list[str]:
    entries = ctx.prior_evidence()
    for entry in entries:                                    # a previous undo may have died holding a temp file
        if entry.get("phase") == "undo_intent" and is_temp_name(entry["temp"]):
            left = ctx.host.lstat(f"{entry['parent']}/{entry['temp']}")
            if left is not None and left.kind == "file":
                ctx.host.discard_staged(entry["parent"], entry["temp"])
    done = {e["path"] for e in entries if e.get("phase") == "removed"}
    seen, notes = set(), []
    for entry in reversed([e for e in entries if e.get("phase") == "removing"]):
        if entry["path"] in seen:
            continue
        seen.add(entry["path"])
        if entry["path"] in done or ctx.host.lstat(entry["path"]) is None:     # only what really was removed
            notes.append(_restore_removed(ctx, entry))
    return notes


class FileRemove:
    def check(self, ctx):
        p = ctx.step.params
        current = ctx.host.lstat(p["path"])
        if current is None:
            return Satisfied(f"{p['path']} is already gone")
        if current.kind != "file":
            return Refuse(f"{p['path']} is a {current.kind}, not a regular file")
        try:
            found = sha256(ctx.host.read_bytes(p["path"]))
        except HostError as exc:
            return Refuse(str(exc))
        if found != p["expected_sha256"]:
            return Refuse(f"{p['path']} changed since the plan was made; it is not removed")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        _remove_with_backup(ctx, p["path"], p["expected_sha256"])
        if p.get("reload_systemd") and ctx.host.run(["systemctl", "daemon-reload"], timeout=60).rc != 0:
            raise StepFailure("the file was removed, but systemd would not reload", "unknown")

    def verify(self, ctx) -> Effect:
        p = ctx.step.params
        current = ctx.host.lstat(p["path"])
        if current is None:
            return "applied"
        try:
            return "not_applied" if sha256(ctx.host.read_bytes(p["path"])) == p["expected_sha256"] else "unknown"
        except HostError:
            return "unknown"

    reconcile = verify

    def inverse(self, ctx) -> str:
        notes = _undo_removals(ctx)
        if ctx.step.params.get("reload_systemd") and ctx.host.run(["systemctl", "daemon-reload"], timeout=60).rc != 0:
            raise StepFailure("the unit file was put back, but systemd would not reload")
        return "; ".join(notes) or "nothing had been removed"


class ServiceDisable:
    """Stop a service and turn off its start at boot, remembering what was actually changed."""

    def _enabled(self, ctx, p) -> bool:
        if p["flavour"] != "systemd":
            return False
        done = ctx.host.run(["systemctl", "is-enabled", p["name"]], timeout=30)
        return done.rc == 0 and done.out.strip() in ("enabled", "enabled-runtime")

    def _running(self, ctx, p) -> bool:
        if p["flavour"] == "systemd":
            return ctx.host.run(["systemctl", "is-active", p["name"]], timeout=30).out.strip() == "active"
        return ctx.host.run([f"/etc/init.d/{p['name']}", "status"], timeout=30).rc == 0

    def check(self, ctx):
        p = ctx.step.params
        if not self._enabled(ctx, p) and not self._running(ctx, p):
            return Satisfied(f"{p['name']} is already stopped and not enabled")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        was_enabled, was_running = self._enabled(ctx, p), self._running(ctx, p)
        ctx.evidence(was_enabled=was_enabled, was_running=was_running)
        if was_running:
            argv = ["systemctl", "stop", p["name"]] if p["flavour"] == "systemd" else [f"/etc/init.d/{p['name']}", "stop"]
            done = ctx.host.run(argv, timeout=120)
            if done.rc != 0 and self._running(ctx, p):
                raise StepFailure(f"stopping {p['name']} failed: " + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300], "unknown")
            ctx.evidence(stopped_done=p["name"])
        if was_enabled:
            done = ctx.host.run(["systemctl", "disable", p["name"]], timeout=60)
            if done.rc != 0 and self._enabled(ctx, p):
                raise StepFailure(f"disabling {p['name']} failed: " + " | ".join((done.err or done.out).strip().splitlines()[-2:])[:300], "unknown")
            ctx.evidence(disabled_done=p["name"])

    def verify(self, ctx) -> Effect:
        p = ctx.step.params
        return "not_applied" if self._enabled(ctx, p) or self._running(ctx, p) else "applied"

    def reconcile(self, ctx) -> Effect:
        outcome = self.verify(ctx)
        entries = ctx.prior_evidence()
        flags = {k: v for e in entries for k, v in e.items() if k in ("was_enabled", "was_running")}
        if outcome == "applied":
            if flags.get("was_running") and not any("stopped_done" in e for e in entries):
                ctx.evidence(stopped_done=ctx.step.params["name"])
            if flags.get("was_enabled") and not any("disabled_done" in e for e in entries):
                ctx.evidence(disabled_done=ctx.step.params["name"])
        return outcome

    def inverse(self, ctx) -> str:
        """Turn back on only what this step turned off."""
        p = ctx.step.params
        entries = ctx.prior_evidence()
        notes = []
        if any("disabled_done" in e for e in entries):
            done = ctx.host.run(["systemctl", "enable", p["name"]], timeout=60)
            if done.rc != 0 and not self._enabled(ctx, p):
                raise StepFailure(f"enabling {p['name']} again failed")
            notes.append(f"enabled {p['name']} again")
        if any("stopped_done" in e for e in entries):
            argv = ["systemctl", "start", p["name"]] if p["flavour"] == "systemd" else [f"/etc/init.d/{p['name']}", "start"]
            done = ctx.host.run(argv, timeout=120)
            if done.rc != 0 and not self._running(ctx, p):
                raise StepFailure(f"starting {p['name']} again failed")
            notes.append(f"started {p['name']} again")
        if not notes:
            raise Irreversible(f"{p['name']} was already stopped and not enabled, so there is nothing to turn back on")
        return "; ".join(notes)


def strip_block(current: str, marker: str) -> str | None:
    """`current` without the marked block, or None if it has none. Half a block is refused, never guessed at."""
    begin = re.compile(rf"^# BEGIN {re.escape(marker)}\b.*\n", re.MULTILINE)
    end = re.compile(rf"^# END {re.escape(marker)}\s*\n?", re.MULTILINE)
    starts, ends = list(begin.finditer(current)), list(end.finditer(current))
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() < starts[0].start():
        raise StepFailure(f"the {marker} markers in this file are not one BEGIN followed by one END; fix them by hand")
    rest = current[: starts[0].start()] + current[ends[0].end():]
    return rest.rstrip("\n") + "\n" if rest.strip() else ""


def _is_empty_script(text: str) -> bool:
    return text.strip() in ("", "#!/bin/sh", "#!/bin/bash")


class _HookStripFile(_AtomicFile):
    """One hook file with the block taken out, written the careful way (compare-and-swap, backup, rename)."""

    def __init__(self, name: str):
        self.name = name

    def norm(self, ctx) -> dict:
        step = ctx.step.params
        path = f"{step['dest_dir']}/{self.name}"
        current = ctx.host.lstat(path)
        return {"path": path, "mode": f"{current.perms:04o}" if current and current.kind == "file" else "0600",
                "owner": "root", "group": "root", "has_secrets": False, "if_exists": "replace",
                "expect_absent": False, "expected_sha256": step["hooks"][self.name],
                "backup_name": f"{ctx.step.id}.{self.name}.strip.bak"}

    def desired(self, ctx) -> bytes:
        path = f"{ctx.step.params['dest_dir']}/{self.name}"
        text = strip_block(ctx.host.read_bytes(path).decode("utf-8"), ctx.step.params["marker"])
        return (text if text is not None else ctx.host.read_bytes(path).decode("utf-8")).encode("utf-8")


class BootHookRemove:
    """Take Planet Express's block out of each MOS boot hook. A hook that held only that block is removed."""

    def _plan(self, ctx):
        """[(name, path, action, current_sha)] where action is "gone", "none", "strip" or "delete"."""
        p, out = ctx.step.params, []
        for name, expected in p["hooks"].items():
            path = f"{p['dest_dir']}/{name}"
            current = ctx.host.lstat(path)
            if current is None:
                out.append((name, path, "gone", expected))
                continue
            if current.kind != "file":
                raise StepFailure(f"{path} is a {current.kind}, not a regular file")
            data = ctx.host.read_bytes(path)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StepFailure(f"{path} is not text") from exc
            stripped = strip_block(text, p["marker"])
            if stripped is None:
                out.append((name, path, "none", sha256(data)))
            elif sha256(data) != expected:
                raise StepFailure(f"{path} changed since the plan was made; its block is not removed")
            else:
                out.append((name, path, "delete" if _is_empty_script(stripped) else "strip", expected))
        return out

    def check(self, ctx):
        try:
            actions = self._plan(ctx)
        except (StepFailure, HostError) as exc:
            return Refuse(exc.reason if isinstance(exc, StepFailure) else str(exc))
        if all(a in ("gone", "none") for _, _, a, _ in actions):
            return Satisfied("the boot hooks no longer carry the Planet Express block")
        return Proceed()

    def act(self, ctx):
        for name, path, action, expected in self._plan(ctx):
            if action == "delete":
                _remove_with_backup(ctx, path, expected)
            elif action == "strip":
                hook = _HookStripFile(name)
                outcome = hook.check(ctx)
                if isinstance(outcome, Refuse):
                    raise StepFailure(outcome.reason)
                if isinstance(outcome, Proceed):
                    hook.act(ctx)

    def verify(self, ctx) -> Effect:
        p = ctx.step.params
        try:
            for name in p["hooks"]:
                path = f"{p['dest_dir']}/{name}"
                if ctx.host.lstat(path) is None:
                    continue
                if strip_block(ctx.host.read_bytes(path).decode("utf-8"), p["marker"]) is not None:
                    return "not_applied"
        except (HostError, StepFailure, UnicodeDecodeError):
            return "unknown"
        return "applied"

    def reconcile(self, ctx) -> Effect:
        outcome = self.verify(ctx)
        _HookStripFile(next(iter(ctx.step.params["hooks"]))).reconcile(ctx)     # tidy a leftover staged file
        return outcome

    def inverse(self, ctx) -> str:
        notes = _undo_removals(ctx)
        first = _HookStripFile(next(iter(ctx.step.params["hooks"])))
        notes.append(first.inverse(ctx))
        return "; ".join(n for n in notes if n)


HANDLERS = {"dir.ensure": DirEnsure(), "file.write": FileWrite(), "verify.smoke": VerifySmoke(),
            "sudoers.install": SudoersInstall(), "dashboard.init": DashboardInit(),
            "access.provision": AccessProvision(),
            "state.snapshot": StateSnapshot(), "python.env": PythonEnv(),
            "service.install": ServiceInstall(), "boot_hook.install": BootHookInstall(),
            "service.enable": ServiceEnable(), "file.remove": FileRemove(), "service.disable": ServiceDisable(),
            "boot_hook.remove": BootHookRemove()}
