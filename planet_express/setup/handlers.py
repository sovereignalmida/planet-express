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
import re
from dataclasses import dataclass
from typing import Literal

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


def _owner(ctx) -> tuple[int, int] | Refuse:
    p = ctx.step.params
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
        return Satisfied(f"{path} already exists") if not found else Proceed()

    def act(self, ctx):
        p = ctx.step.params
        uid, gid = _owner(ctx)
        missing = self._missing(ctx.host, p["path"])
        if isinstance(missing, Refuse):
            raise StepFailure(missing.reason)
        for component in missing:
            try:
                made = ctx.host.mkdir(component, int(p["mode"], 8), uid, gid)
            except HostError as exc:
                raise StepFailure(str(exc), "applied" if component != missing[0] else "not_applied") from exc
            ctx.evidence(created_directory=component, **made.identity)
            ctx.log(f"created {component} ({p['mode']}, {p['owner']}:{p['group']})")

    def verify(self, ctx) -> Effect:
        st = ctx.host.lstat(ctx.step.params["path"])
        return "applied" if st is not None and st.kind == "dir" else "not_applied"

    reconcile = verify


# ----------------------------------------------------------------------------------------------------------
class FileWrite:
    """Compare-and-swap, atomic replace, and undo evidence recorded before the rename."""

    def _desired(self, ctx) -> bytes:
        return render(ctx.step.params["content"], ctx.secrets)

    def check(self, ctx):
        p = ctx.step.params
        path, parent = p["path"], _parent(p["path"])
        ids = _owner(ctx)
        if isinstance(ids, Refuse):
            return ids
        missing = unresolved_secrets(p["content"], ctx.secrets)
        if missing:
            return Refuse(f"no value for secret(s): {', '.join(missing)}")
        parent_stat = ctx.host.lstat(parent)
        if parent_stat is None or parent_stat.kind != "dir":
            return Refuse(f"{parent} does not exist; an earlier step has to create it")

        current = ctx.host.lstat(path)
        wanted = sha256(self._desired(ctx))
        if current is None:
            if p.get("expected_sha256"):
                return Refuse(f"{path} existed when the plan was made but is gone now")
            return Proceed()
        if current.kind != "file":
            return Refuse(f"{path} is a {current.kind}, not a regular file")
        try:
            existing = sha256(ctx.host.read_bytes(path))
        except HostError as exc:
            return Refuse(str(exc))
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
        if p.get("expect_absent"):
            return Refuse(f"{path} exists now, but this step was approved to create it")
        if existing != p.get("expected_sha256"):
            return Refuse(f"{path} changed since the plan was made; refusing to overwrite it")
        return Proceed()

    def act(self, ctx):
        p = ctx.step.params
        path, parent = p["path"], _parent(p["path"])
        name = path.rsplit("/", 1)[1]
        uid, gid = _owner(ctx)
        data = self._desired(ctx)
        wanted = sha256(data)
        current = ctx.host.lstat(path)
        previous = sha256(ctx.host.read_bytes(path)) if current is not None else None
        record = {"path": path, "created_file": current is None, "previous_sha256": previous,
                  "written_sha256": wanted, "backup": None}
        if current is not None:
            backup = f"{ctx.step.id}.bak"
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
            ctx.host.commit_staged(parent, temp, name)
        except HostError as exc:
            try:
                ctx.host.discard_staged(parent, temp)
            except HostError:
                pass
            raise StepFailure(str(exc)) from exc
        ctx.evidence(phase="committed", path=path)
        ctx.log(f"wrote {path} (mode {p['mode']}, {p['owner']}:{p['group']}, sha256 {wanted[:12]})")

    def verify(self, ctx) -> Effect:
        p = ctx.step.params
        current = ctx.host.lstat(p["path"])
        if current is None or current.kind != "file":
            return "not_applied"
        try:
            now = sha256(ctx.host.read_bytes(p["path"]))
        except HostError:
            return "unknown"
        if now == sha256(self._desired(ctx)):
            ids = _owner(ctx)
            ok = (not isinstance(ids, Refuse) and current.perms == int(p["mode"], 8)
                  and (current.uid, current.gid) == ids)
            return "applied" if ok else "unknown"
        # Not what we wrote. Untouched if it is what we saw before, otherwise something else changed it.
        before = next((e.get("previous_sha256") for e in reversed(ctx.prior_evidence()) if "previous_sha256" in e),
                      p.get("expected_sha256"))
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


HANDLERS = {"dir.ensure": DirEnsure(), "file.write": FileWrite(), "verify.smoke": VerifySmoke()}
