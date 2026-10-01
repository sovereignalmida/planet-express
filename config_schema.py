"""
config_schema.py — Planet Express config data model, with no load-time I/O.

Split out of config.py so this schema can be imported and used to build/validate a
config (e.g. scripts/setup_wizard.py, before a config file exists on disk) without
triggering config.py's module-level _load_config() — which reads CONFIG_FILE and
raises SystemExit if it's missing. config.py imports these same classes, so every
existing `from config import PlanetExpressConfig` (etc.) call site is unaffected.
"""

import re
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


# A collector system id, as it appears in beszel's `systems.id` (15 alphanumeric characters
# today). Constrained rather than taken as free text because this value is interpolated into a
# PocketBase filter expression -- `filter=(system='<id>')` -- so a quote or a backslash in it
# would be an injection into the query that selects which host's numbers get rendered. The
# class is deliberately wider than PocketBase's own ids and narrower than anything with
# syntax: no quotes, no spaces, no `&&`.
_SYSTEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_system_id(value: str) -> str:
    if not _SYSTEM_ID_RE.match(value):
        raise ValueError(
            "a system id must be 1-64 characters of letters, digits, '-' or '_' "
            f"(beszel's systems.id), got {value!r}")
    return value


class HostEntry(BaseModel):
    """One expected host in the multi-host inventory.

    The inventory is config and not collector output, because a host that is only known from
    the collector's answer has no name, no link and no row the moment the collector cannot
    answer for it -- it silently disappears, which is the bug rather than the fix. Identity
    comes from here; live values are looked up by `system_id` and filled in.

    There is deliberately NO per-entry "this one is the local host" flag. PE can answer that
    question itself from the container names it reads off the local docker socket, and a
    declared answer to a question PE can derive is a defect generator: a single flag and
    unique ids are cardinality checks, and they pass happily while pointing at the wrong
    system. The optional pin on MultiHostConfig is the one override, and it is one id for the
    whole inventory rather than a boolean per entry.
    """

    model_config = ConfigDict(extra="forbid")

    # beszel's `systems.id`. The stable handle: the name is editable in beszel's UI and the
    # address can change, so neither of those is an identity.
    system_id: str
    name: str
    # The host's own UI. Optional because a host need not have one, and None renders as no
    # button -- never as an empty href, which is a dead button that looks live. This is the
    # only source of the link: beszel stores none, and a link is exactly the field we would
    # not want a remote host able to set.
    link: str | None = None

    @field_validator("system_id")
    @classmethod
    def _system_id_shape(cls, v: str) -> str:
        return _validate_system_id(v)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        # A blank name would render as a host card with no label rather than as "unknown":
        # absent is not empty, so say so at the only moment anyone can fix it.
        if not v.strip():
            raise ValueError("a host entry needs a name")
        return v

    @field_validator("link")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        # Same rule as LaunchLink.href, same reason: this becomes an href the operator
        # clicks, and javascript:/data:/file: have no business behind a host button. "" is
        # refused rather than coerced to None -- an absent link is written as an absent key.
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("a host link must be an http:// or https:// URL")
        return v


class MultiHostConfig(BaseModel):
    """The expected-host inventory, plus the one optional locality override (T48).

    EVERY RULE HERE IS A RULE ABOUT VALUES. Nothing in this model may consult
    `model_fields_set`, and that is a hard constraint rather than a style preference:
    ConfigService compares a draft against the live config by `model_dump()`, so a rule that
    turned on whether a key had been *written* would be invisible to that diff. It could then
    be changed by DELETING a line -- same dump, no changed field, no locked field, no
    passphrase prompt -- which is an escalation that appears nowhere. That was T47's sharpest
    finding. Stating the inventory as a list of explicit entries is what makes presence
    unnecessary: an entry either dumps or it does not.
    """

    model_config = ConfigDict(extra="forbid")

    # Empty means PE does not query the collector AT ALL -- not "queries it and shows
    # nothing". The difference is load-bearing: with an empty inventory every row the
    # collector returned would be an unlisted one, including this host's own, and the
    # surface-unlisted-rows rule would then render this machine a second time as read-only
    # remote data beside the controllable copy the docker socket already provides. Off means
    # no request, so there are no rows for any rule to act on.
    hosts: list[HostEntry] = []
    # The operator's override for an installation where derivation cannot decide (too few
    # local containers, or two systems inside the 3x margin). One pinned id for the whole
    # inventory, never a per-entry boolean. A pin that disagrees with a successful derivation
    # is refused at apply time, where both answers exist -- it cannot be checked here,
    # because deriving locality needs the local docker socket and this module does no I/O.
    local_system_id: str | None = None

    @field_validator("local_system_id")
    @classmethod
    def _pin_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_system_id(v)

    @model_validator(mode="after")
    def _validate_inventory(self) -> "MultiHostConfig":
        seen: set[str] = set()
        for entry in self.hosts:
            if entry.system_id in seen:
                # Two entries for one system id is not a harmless duplicate: both rows look
                # up the same live values, so the host renders twice with one set of numbers
                # under two names, and there is no way to tell which name was meant.
                raise ValueError(
                    f"multi_host.hosts: duplicate system_id {entry.system_id!r}")
            seen.add(entry.system_id)
        if self.local_system_id is not None and not self.hosts:
            # Refused rather than ignored, same convention as an unknown direct origin above:
            # an empty inventory means the collector is never queried, so there are no
            # systems for a pin to name and the pin cannot have any effect. A setting that
            # looks like it is in force and is not is worse than an error.
            raise ValueError(
                "multi_host.local_system_id is set but multi_host.hosts is empty, so the "
                "collector is never queried and the pin can never apply")
        # A pin naming a system that is NOT an inventory entry is deliberately accepted. The
        # local host already renders from the docker socket with working controls, so an
        # operator has no reason to list it as an expected remote host -- the pin's whole job
        # is to say "this collector row is me", which is a statement about the collector's
        # answer and not about the inventory. Requiring a matching entry would force the
        # local host into the inventory, and every inventory entry renders, which is the
        # duplicate rendering this feature exists to avoid.
        return self


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
    # Other hosts (T48). Default empty, which means PE queries no collector and this host
    # renders exactly as it does today. The collector's own credentials are NOT here: they
    # live in /etc/planetexpress-dashboard.env, read by config.beszel_credentials().
    multi_host: MultiHostConfig = MultiHostConfig()
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
