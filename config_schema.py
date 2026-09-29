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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Risk = Literal["R0", "R1", "R2", "R3", "R4"]


# The origins a human drives directly, and therefore the ones a ceiling can differ between.
# Kept here rather than imported from policy.py, which imports this module.
DIRECT_ORIGINS = ("dashboard-direct", "telegram-direct")

# What a direct origin may reach when the config says nothing about it.
#
# The dashboard ships at R3 because that is what its stack controls need to exist at all:
# taking a stack down is R2, and a ceiling of R1 refuses it before the passphrase is ever
# asked for, leaving a DOWN button that can never work. What makes it safe is the elevated
# session -- policy.requires_elevation() puts everything above R1 behind the passphrase, and
# only for origins that can prove one. Telegram is absent here and keeps the shared R1,
# because a chat message cannot.
#
# A default, not a field default: writing it into the model would make every config that
# forbids R3 fail to load, since a forbidden risk may not also be directly requestable. It is
# filtered against forbidden_risks below instead, so forbidden always wins.
DEFAULT_DIRECT_RISKS: dict[str, list[Risk]] = {"dashboard-direct": ["R1", "R2", "R3"]}


class AutonomyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direct_request_risks: list[Risk] = ["R1"]
    # Per-origin overrides of the ceiling above (T47). One number for every direct origin was
    # wrong once the dashboard grew an elevated session: elevation is something a browser can
    # ask for and prove, and a chat message cannot, so the two surfaces should not be obliged
    # to trust each other's requests equally. An origin with no entry here takes the built-in
    # default below, and failing that the shared value above.
    direct_request_risks_by_origin: dict[str, list[Risk]] = {}
    forbidden_risks: list[Risk] = ["R4"]
    cooldown_seconds: int = Field(default=1800, ge=0)
    max_attempts_per_day: int = Field(default=3, ge=1)

    def direct_risks_for(self, origin: str) -> list[Risk]:
        """The ceiling for one direct origin.

        Its own override if the config wrote one, else the built-in default for that origin,
        else the shared value. A forbidden risk is never reachable through the default -- the
        validator already refuses one written by hand, and this is the same rule for the one
        nobody wrote.
        """
        if origin in self.direct_request_risks_by_origin:
            return self.direct_request_risks_by_origin[origin]
        if origin in DEFAULT_DIRECT_RISKS:
            return [risk for risk in DEFAULT_DIRECT_RISKS[origin]
                    if risk not in self.forbidden_risks]
        return self.direct_request_risks

    @model_validator(mode="after")
    def _validate_risks(self) -> "AutonomyConfig":
        if "R4" not in self.forbidden_risks:
            raise ValueError("R4 must be forbidden")
        if "R0" in self.forbidden_risks:
            raise ValueError("R0 must not be forbidden")
        for origin, risks in [(None, self.direct_request_risks)] + sorted(
                self.direct_request_risks_by_origin.items()):
            where = "direct_request_risks" if origin is None else f"{origin}'s ceiling"
            if "R0" in risks:
                raise ValueError(f"R0 must not be directly requestable ({where})")
            # Checked per origin, not just on the shared list: an override naming a forbidden
            # risk is a contradiction the old single check could not have seen.
            if set(self.forbidden_risks) & set(risks):
                raise ValueError(
                    f"a risk cannot be both forbidden and directly requestable ({where})")
        unknown = sorted(set(self.direct_request_risks_by_origin) - set(DIRECT_ORIGINS))
        if unknown:
            # Fail rather than ignore: a ceiling written for an origin that cannot originate
            # anything looks like it is in force and is not.
            raise ValueError(
                f"direct_request_risks_by_origin: {', '.join(unknown)} "
                f"is not a direct origin (expected {', '.join(DIRECT_ORIGINS)})")
        return self


class ExcludedService(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stack: str
    service: str


class SudoUnitGrant(BaseModel):
    """Permission for a specific systemd unit name (e.g. 'casa-stacks.service')."""
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


class LaunchLink(BaseModel):
    """A launch link the operator declares because no route can be read into one honestly.

    `name` is the CONTAINER name, the same key the derived links use. They were keyed by
    Traefik service name until the join moved to the container's address, and a declared
    link under the old key silently matched nothing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    href: str
    zone: Literal["lan", "public"] = "lan"

    @field_validator("href")
    @classmethod
    def _http_only(cls, v: str) -> str:
        # This value becomes an href the operator clicks. Anything that is not plain http(s)
        # -- javascript:, data:, file: -- has no business in a launch button.
        if not v.startswith(("http://", "https://")):
            raise ValueError("a launch link must be an http:// or https:// URL")
        return v


class PlanetExpressConfig(BaseModel):
    # extra="forbid": a misspelled key (e.g. "forbidden_stack") must be a hard error, not
    # silently ignored — pydantic's default would otherwise drop it and fall back to the
    # field's default (an empty list, for forbidden_stacks), silently disabling a safety
    # list the operator thought they'd set.
    model_config = ConfigDict(extra="forbid")

    autonomy: AutonomyConfig = AutonomyConfig()
    stacks_root: Path
    forbidden_stacks: list[str] = []
    paused_containers: list[str] = []
    backup_jobs: list[Literal["daily", "weekly"]] = ["daily", "weekly"]
    # Retired in slice 5b-5 with the shell planner it switched on. Still *accepted* so a host that
    # set it explicitly starts after the upgrade instead of failing config validation before it can
    # even say why — `extra="forbid"` would otherwise stop core dead on a key that used to be
    # valid (Codex, T44). Ignored, warned about once at load, and removable at leisure.
    legacy_plans_enabled: bool | None = None
    mounts: dict[str, str] = {}
    exclude_services: list[ExcludedService] = []
    # The escape hatch for the Launch-links feature: a container whose route this host cannot
    # turn into a URL, or cannot join to the container. container_urls() only emits a link
    # from a rule that is Host() terms joined by `||`, so a compound rule like
    # adventurelog's `(Host || Host) && !(PathPrefix ...)` -- whose host root really is
    # launchable -- gets its link declared here instead of guessed at. Only docker-provider
    # routers are joined to a container, so a file-provider route (a host-networked service)
    # needs one too. `name` is the container name.
    links: list[LaunchLink] = []
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
    # explicitly declares them here. Enforced in casa_bender.py's _check_sudo_allowlist(),
    # independent of whatever a plan's LLM-generated commands claim to need.
    sudo_allowlist: SudoAllowlist = SudoAllowlist()

    @field_validator("backup_jobs")
    @classmethod
    def _validate_backup_jobs(cls, jobs: list[str]) -> list[str]:
        if not jobs:
            raise ValueError("backup_jobs must contain at least one job")
        if len(jobs) != len(set(jobs)):
            raise ValueError("backup_jobs must not contain duplicates")
        return jobs

    @field_validator("stacks_root")
    @classmethod
    def _stacks_root_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"stacks_root must be an absolute path, got {v!r}")
        return v
