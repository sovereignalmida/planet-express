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

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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
Name = Annotated[str, AfterValidator(_name)]
Mode = Annotated[str, Field(pattern=r"^0[0-7]{3}$")]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class FileWrite(_Params):
    path: AbsPath
    content: str
    mode: Mode = "0644"
    owner: Name = "root"
    group: Name = "root"
    if_exists: Literal["keep", "replace"] = "keep"
    # True when `content` carries `{{secret:NAME}}` placeholders: previews mask them, mode stays tight.
    has_secrets: bool = False


class SudoersInstall(_Params):
    path: AbsPath
    content: str
    run_user: Name
    if_exists: Literal["keep", "replace"] = "keep"


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


class ServiceInstall(_Params):
    flavour: Literal["systemd", "sysvinit"]
    name: Name
    path: AbsPath
    content: str
    # An installed unit may carry local edits (a mount gate on casa-stacks, say), so the default keeps it.
    # MOS regenerates its init scripts from the pool at every boot, so it replaces.
    if_exists: Literal["keep", "replace"] = "keep"
    # sysvinit only: /etc/default/<name>, which points the script at persistent paths.
    defaults_path: AbsPath | None = None
    defaults_content: str | None = None


class BootHookInstall(_Params):
    dest_dir: AbsPath
    hooks: dict[Name, str]


class ServiceEnable(_Params):
    flavour: Literal["systemd", "sysvinit"]
    name: Name
    start: bool = False


class VerifySmoke(_Params):
    install_dir: AbsPath
    config_path: AbsPath


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
    "verify.smoke": Kind(VerifySmoke, "R0", True, False, "Check Planet Express can see Docker"),
}
