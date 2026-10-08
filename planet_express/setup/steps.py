"""The setup step catalogue: every change setup can make is one of these typed kinds.

A kind is its params model plus its risk class, reversibility and root need. There is no
"run this command" kind: `apply` maps each kind to code that does exactly that one thing, so what the
operator reviews in the plan is the whole of what can happen. Adding a kind adds a capability.
Risk classes are the runbook ones (`planet_express/execution/runbook.py`): R0 read-only, R1 contained
and reversible, R2 system-wide or stops things, R3 grants privilege or touches the boot image.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

Risk = Literal["R0", "R1", "R2", "R3", "R4"]

# Absolute, no `..`, and none of the characters systemd unit directives or shell-ish contexts treat
# specially. Deliberately stricter than the filesystem: scripts/render_template.py rejects the same set.
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._@+/-]*$")
_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def _absolute_path(value: str) -> str:
    if not _SAFE_PATH.fullmatch(value) or ".." in value.split("/"):
        raise ValueError(f"not a safe absolute path: {value!r}")
    return value


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"not a safe name: {value!r}")
    return value


AbsPath = Annotated[str, AfterValidator(_absolute_path)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, AfterValidator(_name)]
Mode = Annotated[str, Field(pattern=r"^0[0-7]{3}$")]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Expecting(_Params):
    """A step that may overwrite a file records what it expects to find, so `apply` can refuse when the
    file changed after the operator reviewed the plan (compare-and-swap, as `compose.write` does).
    Replacing without an expectation is not representable."""

    if_exists: Literal["keep", "replace"] = "keep"
    expect_absent: bool = False
    expected_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _expectation_is_coherent(self):
        if self.expect_absent and self.expected_sha256:
            raise ValueError("a file cannot be expected both absent and present")
        if self.if_exists == "replace" and not (self.expect_absent or self.expected_sha256):
            raise ValueError("replacing a file needs the expectation it will be compared against")
        return self


class DirEnsure(_Params):
    path: AbsPath
    mode: Mode = "0755"
    owner: Name = "root"
    group: Name = "root"


class PythonEnv(_Params):
    install_dir: AbsPath
    venv_dir: AbsPath
    requirements: str = "requirements.txt"
    # MOS ships Python without ensurepip, so pip is fetched into the venv from the network.
    bootstrap_pip: bool = False


class FileWrite(_Expecting):
    path: AbsPath
    content: str
    mode: Mode = "0644"
    owner: Name = "root"
    group: Name = "root"
    # True when `content` carries `{{secret:NAME}}` placeholders: previews mask them, mode stays tight.
    has_secrets: bool = False


class SudoersInstall(_Expecting):
    path: AbsPath
    content: str
    run_user: Name


class AccessProvision(_Params):
    """The dashboard's own identity and exactly the read access it needs. systemd hosts use POSIX
    ACLs (scripts/web_access.py); MOS has no setfacl, so it uses groups and plain permission bits."""
    method: Literal["acl", "groups"]
    install_dir: AbsPath
    run_user: Name
    web_user: Name = "planetexpress-web"
    rpc_group: Name = "planetexpress-rpc"
    state_dir: AbsPath | None = None
    logs_dir: AbsPath | None = None
    config_path: AbsPath | None = None
    venv_dir: AbsPath | None = None


class DashboardInit(_Params):
    env_file: AbsPath
    operator: Name | None = None
    # Names of secrets held beside the plan, never the values.
    passphrase_ref: Name | None = None
    totp_ref: Name | None = None


class ServiceInstall(_Expecting):
    # An installed unit may carry local edits (a mount gate on casa-stacks, say), so the default keeps it.
    # MOS regenerates its init scripts from the pool at every boot, so it replaces.
    flavour: Literal["systemd", "sysvinit"]
    name: Name
    path: AbsPath
    content: str
    # sysvinit only: /etc/default/<name>, which points the script at persistent paths.
    defaults_path: AbsPath | None = None
    defaults_content: str | None = None


class BootHookInstall(_Params):
    """Merge a marked block into each hook file; never replace the file. An operator's own commands
    in post-start.sh must survive. `hooks` maps file name to the block body, which must not `exit`."""

    dest_dir: AbsPath
    hooks: dict[Name, str]
    marker: Name = "planetexpress"
    # What each hook file looked like when the plan was made. A file in `expect_absent` must still not exist;
    # a file in `expected_sha256` must still hash to that; a hook in neither cannot be merged into (the plan
    # refuses to build that case rather than merge into a file nobody has read).
    expect_absent: list[Name] = []
    expected_sha256: dict[Name, Sha256] = {}
    # Hashes of the whole-file versions this project once shipped for hand installation. A hook file that is
    # byte-for-byte one of these is entirely ours, so it is replaced by the marked version; anything else is
    # merged into, never replaced. Exact hashes, not a guess from the content.
    legacy_sha256: dict[Name, list[Sha256]] = {}

    @model_validator(mode="after")
    def _blocks_cannot_end_the_script(self):
        unbound = [n for n in self.hooks if n not in self.expect_absent and n not in self.expected_sha256]
        if unbound:
            raise ValueError(f"{', '.join(unbound)}: a hook file needs an expectation (absent or a hash) before it is merged into")
        for name, body in self.hooks.items():
            if any(line.split("#")[0].strip().startswith("exit") for line in body.splitlines()):
                raise ValueError(f"{name}: a merged block must not exit; it would skip the operator's own commands")
        return self


class StateSnapshot(_Params):
    """The safety net before changing an install that already has state, as deploy.sh does."""

    install_dir: AbsPath
    label: Name = "pre-setup"
    run_user: Name


class ServiceEnable(_Params):
    flavour: Literal["systemd", "sysvinit"]
    name: Name
    start: bool = False


class VerifySmoke(_Params):
    """Run Leela's read-only status scan once, as the service user (not root) with the environment the
    service will have, so it tests the permissions that matter."""

    install_dir: AbsPath
    venv_dir: AbsPath
    run_user: Name
    # CASA_* paths only: the service's own configuration, nothing else reaches the process.
    env: dict[Name, AbsPath]

    @model_validator(mode="after")
    def _only_casa_paths(self):
        bad = [k for k in self.env if not k.startswith("CASA_")]
        if bad:
            raise ValueError(f"only CASA_* variables may be set: {bad}")
        return self


class Kind:
    def __init__(self, model: type[_Params], risk: Risk, reversible: bool, needs_root: bool, label: str):
        self.model, self.risk, self.reversible, self.needs_root, self.label = (
            model, risk, reversible, needs_root, label)


# `risk` and `needs_root` here are the defaults; the builder raises a file.write to R2 and root when
# it targets a system path (see plan._file_step).
CATALOGUE: dict[str, Kind] = {
    "dir.ensure": Kind(DirEnsure, "R1", True, False, "Create a directory"),
    "python.env": Kind(PythonEnv, "R1", True, False, "Create the Python environment"),
    "file.write": Kind(FileWrite, "R1", True, False, "Write a file"),
    "sudoers.install": Kind(SudoersInstall, "R3", True, True, "Grant scoped sudo"),
    "access.provision": Kind(AccessProvision, "R2", False, True, "Provision dashboard access"),
    "dashboard.init": Kind(DashboardInit, "R2", True, True, "Initialise the dashboard login"),
    "service.install": Kind(ServiceInstall, "R2", True, True, "Install a service"),
    "boot_hook.install": Kind(BootHookInstall, "R3", True, True, "Install MOS boot hooks"),
    "service.enable": Kind(ServiceEnable, "R2", True, True, "Enable a service"),
    "state.snapshot": Kind(StateSnapshot, "R1", True, False, "Snapshot the current state"),
    "verify.smoke": Kind(VerifySmoke, "R0", True, False, "Check Planet Express can see Docker"),
}
