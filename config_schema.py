"""
config_schema.py — Planet Express config data model, with no load-time I/O.

Split out of config.py so this schema can be imported and used to build/validate a
config (e.g. scripts/setup_wizard.py, before a config file exists on disk) without
triggering config.py's module-level _load_config() — which reads CONFIG_FILE and
raises SystemExit if it's missing. config.py imports these same classes, so every
existing `from config import PlanetExpressConfig` (etc.) call site is unaffected.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ExcludedService(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stack: str
    service: str


class SudoUnitGrant(BaseModel):
    """Permission for a specific systemd unit name (e.g. 'casa-startup.service')."""
    model_config = ConfigDict(extra="forbid")
    unit: str
    actions: list[Literal["start", "stop", "restart"]] = ["start", "stop", "restart"]


class SudoGlobGrant(BaseModel):
    """Permission for a glob pattern of unit names (e.g. '*.mount')."""
    model_config = ConfigDict(extra="forbid")
    glob: str
    actions: list[Literal["start", "stop", "restart"]] = ["start", "stop"]


class SudoAllowlist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: list[SudoUnitGrant] = []
    globs: list[SudoGlobGrant] = []


class PlanetExpressConfig(BaseModel):
    # extra="forbid": a misspelled key (e.g. "forbidden_stack") must be a hard error, not
    # silently ignored — pydantic's default would otherwise drop it and fall back to the
    # field's default (an empty list, for forbidden_stacks), silently disabling a safety
    # list the operator thought they'd set.
    model_config = ConfigDict(extra="forbid")

    stacks_root: Path
    forbidden_stacks: list[str] = []
    paused_containers: list[str] = []
    mounts: dict[str, str] = {}
    exclude_services: list[ExcludedService] = []
    # /install only ever writes a LAN-only Traefik router (no auth of its own) —
    # restricted to this deployment's own LAN-only domain convention so a mistyped or
    # malicious domain can't silently expose a brand-new, unreviewed container to the
    # public internet. Defaults to this host's existing convention for compatibility
    # with config.yaml files written before this field existed.
    lan_only_domain: str = "casalan.com"

    @field_validator("lan_only_domain")
    @classmethod
    def _lan_only_domain_lowercase(cls, v: str) -> str:
        # DNS names are case-insensitive; the /install command lowercases the
        # requested domain before comparing against this — normalize here too so a
        # mixed-case config value (e.g. "CasaLan.com") doesn't reject every valid request.
        return v.lower()
    # Empty by default -- a fresh install grants zero sudo actions until the operator
    # explicitly declares them here. Enforced in casa_bender.py's _safety_check(),
    # independent of whatever a plan's LLM-generated commands claim to need.
    sudo_allowlist: SudoAllowlist = SudoAllowlist()

    @field_validator("stacks_root")
    @classmethod
    def _stacks_root_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"stacks_root must be an absolute path, got {v!r}")
        return v
