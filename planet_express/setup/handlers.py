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
        if not data_dir or not path.startswith(f"{data_dir}/snapshots/"):
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


# ----------------------------------------------------------------------------------------------------------
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"       # unpinned, as deploy.sh's own bootstrap is
REQUIRED_MODULES = "import yaml, pydantic, flask"          # what the setup and dashboard code need at least
_FETCH = "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])"


class PythonEnv:
    """A virtualenv for the install, built by the service user (as deploy.sh does) with a fixed umask, so the
    dashboard's own account can read what it must run from. Setup installs; it does not upgrade, so an
    environment that already imports its dependencies is left alone."""

    def _python(self, p) -> str:
        return f"{p['venv_dir']}/bin/python"

    def _imports_ok(self, ctx) -> bool:
        p = ctx.step.params
        if ctx.host.lstat(self._python(p)) is None:
            return False
        return ctx.host.run([self._python(p), "-c", REQUIRED_MODULES], as_user=p["run_user"], timeout=60).rc == 0

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
        if created:
            ctx.evidence(phase="creating_venv", venv_dir=p["venv_dir"])
            args = ["python3", "-m", "venv"] + (["--without-pip"] if p["bootstrap_pip"] else []) + [p["venv_dir"]]
            made = ctx.host.run(args, as_user=user, cwd=cwd, timeout=300, umask=0o022)
            if made.rc != 0:
                raise StepFailure(f"creating the virtualenv failed: {self._tail(made)}", "unknown")
            top = ctx.host.lstat(p["venv_dir"])
            if top is not None:
                ctx.evidence(created_venv=p["venv_dir"], **top.identity)
            ctx.log(f"created the virtualenv {p['venv_dir']}")
        if p["bootstrap_pip"] and ctx.host.run([python, "-m", "pip", "--version"], as_user=user, timeout=30).rc != 0:
            script = f"{p['venv_dir']}/get-pip.py"
            got = ctx.host.run(["python3", "-c", _FETCH, GET_PIP_URL, script], as_user=user, cwd=cwd, timeout=120)
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

    @staticmethod
    def _tail(result) -> str:
        return " | ".join((result.err or result.out).strip().splitlines()[-2:])[:300]

    def verify(self, ctx) -> Effect:
        return "applied" if self._imports_ok(ctx) else "not_applied"

    reconcile = verify


HANDLERS = {"dir.ensure": DirEnsure(), "file.write": FileWrite(), "verify.smoke": VerifySmoke(),
            "state.snapshot": StateSnapshot(), "python.env": PythonEnv()}
