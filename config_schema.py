"""
config_schema.py — Planet Express config data model, with no load-time I/O.

Split out of config.py so this schema can be imported and used to build/validate a
config (e.g. scripts/setup_wizard.py, before a config file exists on disk) without
triggering config.py's module-level _load_config() — which reads CONFIG_FILE and
raises SystemExit if it's missing. config.py imports these same classes, so every
existing `from config import PlanetExpressConfig` (etc.) call site is unaffected.
"""

import posixpath
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from planet_express.missions.launcher.config import (
    DEFAULT_UID_RANGE as _DEFAULT_UID_RANGE,
    GROUP_NAME as _GROUP_NAME,
    MAX_LOG_GROUPS as _MAX_SESSION_GROUPS,
    SYSTEMD_DYNAMIC_UIDS as _SYSTEMD_DYNAMIC_UIDS,
)

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
    # fullmatch, not match: `$` also matches the position before a FINAL newline, so
    # `match()` accepted "abc\n" -- an id that looks validated and names no system, leaving
    # that host unknown forever with nothing to point at (Codex review, S3).
    if not _SYSTEM_ID_RE.fullmatch(value):
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


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.IGNORECASE)
MOUNTINFO_PATH = "/proc/self/mountinfo"      # tests point this at a fake mount table
_UNIX_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# Trust mode (docs/designs/v4-trust-mode.md): the real Docker socket, sudo through the one session rule, and the host's logs.
DEFAULT_SESSION_GROUPS = ("docker", "pe-session", "adm", "systemd-journal")
# missions.* keys the tripwire read before trust mode removed it: accepted in an old config.yaml and ignored.
RETIRED_TRIPWIRE_KEYS = frozenset({"tripwire_interval_s", "tripwire_investigating_interval_s", "tripwire_accept_blind"})
# missions.* keys of the strict model trust mode deleted in stage 4 (the protected set and its discovery, the Docker proxy, the
# Repair side-door gate, scoped worker sudo): accepted in an old config.yaml and ignored.
RETIRED_STRICT_KEYS = frozenset({
    "protected", "protected_overrides", "protected_absent", "live_trees", "cross_host", "package_paths",
    "docker_proxy_ports", "docker_socket", "docker_label_allow", "worker_sudo_rules", "worker_sudo_all_sessions",
})
# missions.checkpoint.* keys of the backup floor trust mode deleted: accepted and ignored.
RETIRED_CHECKPOINT_KEYS = frozenset({"backup_sources"})


class MissionRuntimeConfig(BaseModel):
    """The agent command a mission session runs. argv only: there is no shell string."""
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = []
    # Names copied from Core's environment into the session. Everything else is dropped; a name that
    # looks like a credential is refused outright (sessions get no secret values).
    env_allow: list[str] = []

    @field_validator("argv")
    @classmethod
    def _argv(cls, argv: list[str]) -> list[str]:
        if argv and (not argv[0].startswith("/") or any(not a or "\0" in a for a in argv)):
            raise ValueError("runtime.argv must start with an absolute program path and have no empty or NUL items")
        return argv

    @field_validator("env_allow")
    @classmethod
    def _env_allow(cls, names: list[str]) -> list[str]:
        for name in names:
            if not _ENV_NAME.fullmatch(name) or name.startswith("PE_") or _SECRETISH.search(name):
                raise ValueError(f"runtime.env_allow: {name!r} is not an allowed environment name")
        return names


# `claude-sonnet-4-5`, `anthropic/claude-sonnet-4.5`, `~anthropic/claude-sonnet-latest[1m]`: one to three segments, no `..`, no `//`.
_MODEL_ID = re.compile(r"~?[A-Za-z0-9][A-Za-z0-9._:-]{0,63}(/[A-Za-z0-9][A-Za-z0-9._:-]{0,63}){0,2}(\[[0-9a-z]{1,6}\])?")


class ProviderUsageConfig(BaseModel):
    """What the model provider says about the account (missions/provider_usage.py): credit left and spend, polled by Core with the
    broker's own key (OpenRouter) or an optional read-only admin key (Anthropic), shown in the Console and on the Missions page, and
    one Telegram alert when credit runs low. It never stops anything: the operator decides."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_s: int = Field(default=300, ge=60, le=3600)
    warn_below: float = Field(default=10.0, ge=0, le=100_000)      # USD of credit left: amber, and one Telegram alert
    low_below: float = Field(default=2.0, ge=0, le=100_000)        # USD of credit left: red, and a second alert
    # Anthropic direct: the NAME of a Core environment variable holding an Admin API key (sk-ant-admin...), which reads the
    # organisation's cost report. Empty: no spend figures for Anthropic (a usage key cannot read them).
    admin_key_env: str = ""

    @field_validator("admin_key_env")
    @classmethod
    def _admin_key_env(cls, name: str) -> str:
        if name and (not _ENV_NAME.fullmatch(name) or name.startswith("PE_")):
            raise ValueError("missions.model_broker.usage.admin_key_env is the name of an environment variable, not a PE_ one")
        return name

    @model_validator(mode="after")
    def _order(self):
        if self.low_below > self.warn_below:
            raise ValueError("missions.model_broker.usage.low_below must not be above warn_below")
        return self


class ModelBrokerConfig(BaseModel):
    """The per-session model broker (missions/model_broker.py): Core holds the model API key, a session holds only a
    per-session access code and a loopback base URL. Off by default; with it off a session has no model access of its own."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    upstream_host: str = "api.anthropic.com"        # the only host the broker relays to, over TLS
    upstream_path_prefix: str = ""                  # where the vendor's API is mounted on that host (OpenRouter: /api)
    auth_scheme: Literal["x-api-key", "bearer"] = "x-api-key"    # how the real key is presented upstream (OpenRouter: bearer)
    # Non-secret model settings put in the session's environment, e.g. ANTHROPIC_DEFAULT_SONNET_MODEL for a gateway whose model
    # ids differ from the vendor's. Only these names, and values that look like model ids.
    session_env: dict[str, str] = {}
    # Request fields beyond the standard Messages ones that the operator has vetted (the broker refuses any other top-level field).
    allow_body_keys: list[str] = []
    key_env: str = "ANTHROPIC_API_KEY"              # the NAME of the Core environment variable that holds the real key
    models: list[str] = []                          # model ids a mission may request; empty is refused when enabled
    max_requests: int = Field(default=300, gt=0, le=100_000)       # per session
    max_tokens: int = Field(default=10_000_000, gt=0)              # per session: input + output, cache reads at a tenth
    # rc14.8: PE's own calls (Hermes, Farnsworth, Amy, Fry) through the broker's Core channel, per UTC day
    core_max_requests_per_day: int = Field(default=1000, gt=0, le=100_000)
    core_max_tokens_per_day: int = Field(default=5_000_000, gt=0)
    timeout_s: int = Field(default=300, gt=0, le=3600)
    usage: ProviderUsageConfig = ProviderUsageConfig()             # credit and spend from the provider, shown, never enforced

    @field_validator("upstream_host")
    @classmethod
    def _host(cls, host: str) -> str:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?", host):
            raise ValueError("missions.model_broker.upstream_host is a bare host name (and optional port), no scheme or path")
        if ":" in host and not 1 <= int(host.rpartition(":")[2]) <= 65535:
            raise ValueError("missions.model_broker.upstream_host: the port must be 1-65535")
        return host

    @field_validator("upstream_path_prefix")
    @classmethod
    def _prefix(cls, prefix: str) -> str:
        if prefix and (not re.fullmatch(r"(/[A-Za-z0-9._-]{1,32}){1,4}", prefix) or any(seg in (".", "..") for seg in prefix.split("/"))):
            raise ValueError("missions.model_broker.upstream_path_prefix is a plain path like /api, without a trailing slash")
        return prefix

    @field_validator("allow_body_keys")
    @classmethod
    def _body_keys(cls, keys: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k) for k in keys):
            raise ValueError("missions.model_broker.allow_body_keys: lowercase field names")
        return keys

    @field_validator("session_env")
    @classmethod
    def _session_env(cls, env: dict[str, str]) -> dict[str, str]:
        allowed = {"ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                   "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
        for name, value in env.items():
            if name not in allowed or not _MODEL_ID.fullmatch(value):
                raise ValueError(f"missions.model_broker.session_env: only {sorted(allowed)} with a model id as the value")
        return env

    @field_validator("key_env")
    @classmethod
    def _key_env(cls, name: str) -> str:
        if not _ENV_NAME.fullmatch(name) or name.startswith("PE_"):
            raise ValueError("missions.model_broker.key_env is the name of an environment variable, not a PE_ one")
        return name

    @field_validator("models")
    @classmethod
    def _models(cls, models: list[str]) -> list[str]:
        for model in models:
            if not _MODEL_ID.fullmatch(model):
                raise ValueError(f"missions.model_broker.models: {model[:40]!r} is not a model id")
        return models

    @model_validator(mode="after")
    def _needs_models(self):
        if self.enabled and not self.models:
            raise ValueError("missions.model_broker.enabled needs missions.model_broker.models: name the model ids a mission may use")
        if self.enabled:
            stray = [v for v in self.session_env.values() if v not in self.models]
            if stray:
                raise ValueError(f"missions.model_broker.session_env names {stray[0]!r}, which is not in missions.model_broker.models: "
                                 "the CLI would ask for a model the broker then refuses")
        return self


class MissionBudgetsConfig(BaseModel):
    """Default per-mission ceilings. A grant may only lower them."""
    model_config = ConfigDict(extra="forbid")

    wall_seconds: int = Field(default=1800, gt=0)
    tool_calls: int = Field(default=200, gt=0)
    output_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    retries: int = Field(default=2, ge=0)


class CheckpointConfig(BaseModel):
    """Checkpoint limits (v4 slice 5). Sizes are bytes."""
    model_config = ConfigDict(extra="forbid")

    root: str | None = None          # unset: `checkpoints` next to the state database (MOS: on the data pool)
    max_file_bytes: int = Field(default=1024 * 1024, gt=0)
    max_total_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    max_store_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    retention_days: int = Field(default=90, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _retired_backup_floor(cls, data):
        """Trust mode removed the backup floor; `backup_sources` in an old config.yaml is accepted and ignored."""
        if isinstance(data, dict) and any(k in data for k in RETIRED_CHECKPOINT_KEYS):
            data = {k: v for k, v in data.items() if k not in RETIRED_CHECKPOINT_KEYS}
        return data

    @field_validator("root")
    @classmethod
    def _root(cls, path: str | None) -> str | None:
        if path is not None and (not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path
                                 or path == "/"):
            raise ValueError("missions.checkpoint.root must be a normalised absolute path")
        return path


_VERIFY_SHELLS = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish", "busybox", "env"})


class VerifyConfig(BaseModel):
    """Verification limits (v4 slice 8). The allowlist is the operator's: empty means `exec_probe` is unavailable."""
    model_config = ConfigDict(extra="forbid")

    exec_allowlist: list[str] = []                 # binaries an `exec_probe` may run inside a container (exact argv[0])
    check_timeout_cap_s: int = Field(default=60, ge=1, le=60)
    scan_timeout_s: int = Field(default=120, ge=10, le=600)
    max_retries: int = Field(default=2, ge=0, le=3)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)

    @field_validator("exec_allowlist")
    @classmethod
    def _allowlist(cls, binaries: list[str]) -> list[str]:
        for item in binaries:
            if (not item or len(item) > 128 or any(c.isspace() or c == "\0" for c in item) or item.startswith("-")
                    or posixpath.basename(item) in _VERIFY_SHELLS):
                raise ValueError("missions.verify.exec_allowlist: a binary name or absolute path, never a shell")
        return binaries


class MissionHooksConfig(BaseModel):
    """What the reference adapter's pre-tool hook does when it cannot decide (contract 12). A speed bump, not the boundary."""
    model_config = ConfigDict(extra="forbid")

    blocked_failure: Literal["closed", "open"] = "closed"      # input that might be a blocked class: block (closed) or allow
    other_failure: Literal["closed", "open"] = "open"          # anything else: allow with a loud event (open) or block (closed)
    # Installing the hooks into the agent runtime is runtime-specific. With `install_claude_code` Core copies the hook module
    # into each session's hook directory (`pe_hook.py`) at every launch and writes Claude Code's hook settings to run it with
    # `command[0]`, the interpreter (absolute). Anything after command[0] (an older config's script path or `-m` module) is
    # accepted and ignored: the per-launch copy is what runs, so it can never be stale. Empty means the default.
    command: list[str] = ["/usr/bin/python3"]
    install_claude_code: bool = False
    # A directory Core owns and a session can read but never write (root:root or Core's user, 0755), outside every session
    # workspace: each session gets `<root>/<session id>/settings.json` there, and its hash is checked at every poll.
    root: str | None = None

    @model_validator(mode="after")
    def _install_needs_a_command(self) -> "MissionHooksConfig":
        if not self.command:
            self.command = ["/usr/bin/python3"]
        if not self.command[0].startswith("/") or any(not a or "\0" in a for a in self.command):
            raise ValueError("missions.hooks.command must start with an absolute interpreter path and have no empty or NUL items")
        if self.root is not None and (not self.root.startswith("/") or "\0" in self.root or posixpath.normpath(self.root) != self.root
                                      or self.root == "/"):
            raise ValueError("missions.hooks.root must be a normalised absolute path")
        if self.install_claude_code and self.root is None:
            raise ValueError("missions.hooks.install_claude_code needs missions.hooks.root: the settings must live where the session cannot edit them")
        return self


class MissionConsoleConfig(BaseModel):
    """The Operator Console: conversations with a PE-aware agent, one contained process per operator message. Off by default.
    It needs the model broker (the budget is enforced there) and the hooks installed with forced settings (the agent's state
    persists between turns, so a settings file it left behind must never govern the next turn)."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    claude: str = "/usr/local/bin/claude"          # the claude CLI itself, never a wrapper (the forced settings are CLI flags)
    model: str = "~anthropic/claude-sonnet-latest"
    max_turns: int = Field(default=40, ge=1, le=200)               # the agent's own turns inside one operator message
    # rc14.6: a usage mark per conversation, shown on the page; nothing stops at it. The broker's per-session ceilings are the backstop.
    token_budget: int = Field(default=500_000, ge=10_000, le=50_000_000)
    min_turn_tokens: int = Field(default=20_000, ge=1_000, le=1_000_000)      # no longer used (rc14.6); still accepted so old configs load
    max_output_tokens: int = Field(default=8192, ge=1024, le=32_000)         # declared per request: the broker reserves it up front
    max_open: int = Field(default=10, ge=1, le=50)
    archive_after_days: int = Field(default=3, ge=1, le=90)
    turn_wall_s: int = Field(default=900, ge=60, le=7200)
    turn_tool_calls: int = Field(default=200, ge=1, le=5000)
    tools: list[str] = ["Bash", "Read", "Grep", "Glob", "Write"]
    disallowed_tools: list[str] = ["WebFetch", "WebSearch", "Edit", "NotebookEdit"]

    @field_validator("claude")
    @classmethod
    def _claude(cls, path: str) -> str:
        if (not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path
                or not re.fullmatch(r"claude(-code)?", posixpath.basename(path))):
            raise ValueError("missions.console.claude must be the absolute path of the claude binary itself")
        return path

    @field_validator("model")
    @classmethod
    def _model(cls, model: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9~][A-Za-z0-9._:/~@+-]{0,127}", model):
            raise ValueError("missions.console.model is not a model id")
        return model

    @field_validator("tools", "disallowed_tools")
    @classmethod
    def _tool_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise ValueError("a tool name is letters, digits and underscores (no patterns, no arguments)")
        return names


def uid_range_problem(low: int, high: int) -> str | None:
    """Why a session uid range is unsafe, or None: it may not overlap systemd's DynamicUser range. Pure arithmetic, no host
    reads: whether an account already uses the range is the launcher preflight's job (`check_range_free`, which also reads
    the group database and reports an unreadable one as a failed check)."""
    dlow, dhigh = _SYSTEMD_DYNAMIC_UIDS
    if low <= dhigh and high >= dlow:
        return (f"missions.session_uid_min..session_uid_max ({low}-{high}) overlaps systemd's DynamicUser range "
                f"{dlow}-{dhigh}. Use a range outside it, e.g. {_DEFAULT_UID_RANGE[0]}-{_DEFAULT_UID_RANGE[1]} (the default)")
    return None


class MissionsConfig(BaseModel):
    """v4 mission control. Off by default: with `enabled` false nothing mission-related runs."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Retired in slice 3d: there is no shared worker account any more (every session runs as its own uid from
    # `session_uid_min..max`, contract 22.1). Still accepted so an existing config file keeps loading; never read.
    worker_user: str = "pe-worker"
    # Which launcher starts sessions (contract 22.2). `auto` is `direct` when Core runs as root (MOS) and `sudo`
    # otherwise; an explicit value is never silently downgraded: if its prerequisites are missing the host is
    # proposal-only.
    launcher: Literal["auto", "sudo", "direct"] = "auto"
    # The uid (and same-numbered private gid) range reserved for sessions. The top uid is reserved for preflight.
    # The default is launcher.config.DEFAULT_UID_RANGE (the one source of truth). It was 61000-61999 before, which overlapped
    # systemd's DynamicUser range (61184-65519); a config that still says so is now REFUSED at load, never rewritten: see
    # INSTALL.md "Moving an existing uid range".
    session_uid_min: int = Field(default=_DEFAULT_UID_RANGE[0], gt=0, lt=2**31)
    session_uid_max: int = Field(default=_DEFAULT_UID_RANGE[1], gt=0, lt=2**31)
    # Per-session socket directories live here (root/Core owned, mode 0711).
    sock_root: str = "/run/planetexpress-sessions"
    # The `sessions` cgroup inside the cgroup Core is delegated (one child cgroup per session). The default is the
    # systemd layout (`Delegate=yes` on casa-planetexpress.service); on MOS set it to the tree the boot script makes.
    # The launcher setup writes THIS value into launch.conf, so Core and the helper agree on where sessions live.
    cgroup_root: str = "/sys/fs/cgroup/system.slice/casa-planetexpress.service/sessions"
    # Each session gets its own directory here (and the launcher's launch.conf names it as the workspace root).
    # Unset: `mission-workspaces` next to the state database.
    workspace_root: str | None = None
    # Where the root helper's trust file is (the same path setup wrote it to). Only MOS changes this (its root is RAM, so
    # it sits under the data pool); a wrong value cannot widen anything, the preflight's install check just fails.
    launch_conf: str = "/etc/planetexpress-launch/launch.conf"
    worker_socket: str = "/run/planetexpress/worker.sock"
    runtime: MissionRuntimeConfig = MissionRuntimeConfig()
    budgets: MissionBudgetsConfig = MissionBudgetsConfig()
    checkpoint: CheckpointConfig = CheckpointConfig()
    verify: VerifyConfig = VerifyConfig()
    hooks: MissionHooksConfig = MissionHooksConfig()
    model_broker: ModelBrokerConfig = ModelBrokerConfig()
    console: MissionConsoleConfig = MissionConsoleConfig()
    # Named time windows for standing grants (`window.deny: [backup]` resolves here when the grant is created). Each entry is
    # `HH:MM-HH:MM` (UTC), `day[,day]@HH:MM-HH:MM` or `day[,day]` (see missions/window.py).
    windows: dict[str, list[str]] = {}
    # The supplementary groups EVERY session is started with, whatever its tier (trust mode): `docker` (the real socket),
    # `pe-session` (the one sudoers rule setup writes), `adm` (/var/log) and `systemd-journal` (journalctl). Any group name;
    # the launcher refuses one whose gid is 0, Core's own, or inside the session uid range. The root-owned launch.conf is
    # written from this list at setup (file key `log_groups`), and preflight fails unless its probe holds exactly these
    # (those the host has). `read_log_groups` is the deprecated name, still accepted (see `_deprecated_read_log_groups`).
    session_groups: list[str] = list(DEFAULT_SESSION_GROUPS)
    # Brokered tools: tool name -> {name: base url}, and secret ref -> the NAME of a Core environment variable that
    # holds the key. The keys themselves live only in Core's environment, never in this file or a session.
    tool_sites: dict[str, dict[str, str]] = {}
    tool_secret_env: dict[str, str] = {}

    @model_validator(mode="after")
    def _the_broker_key_stays_out_of_the_session(self):
        # The launcher refuses an environment of more than 64 variables; the supervisor, the broker and the hooks add about 35.
        if self.model_broker.enabled and len(self.runtime.env_allow) + len(self.model_broker.session_env) > 20:
            raise ValueError("missions.runtime.env_allow and missions.model_broker.session_env together name more than 20 variables: "
                             "the session launcher caps the whole environment at 64")
        if self.model_broker.enabled and self.model_broker.key_env in self.runtime.env_allow:
            raise ValueError("missions.runtime.env_allow names missions.model_broker.key_env: the real model key must never reach "
                             "a session (the broker hands the agent its own access code)")
        if self.hooks.install_claude_code and self.runtime.argv and not re.fullmatch(r"claude(-code)?", posixpath.basename(self.runtime.argv[0])):
            raise ValueError("missions.hooks.install_claude_code forces Claude Code's settings by adding flags to missions.runtime.argv, so "
                             "argv[0] must be the claude binary itself, not a wrapper such as env or sh")
        if self.console.enabled:
            if not self.model_broker.enabled:
                raise ValueError("missions.console.enabled needs missions.model_broker.enabled: the conversation budget is enforced by the broker")
            if not self.hooks.install_claude_code:
                raise ValueError("missions.console.enabled needs missions.hooks.install_claude_code: a conversation's state persists between "
                                 "turns, so PE's settings must be the only ones the agent reads")
            if set(self.console.tools) & set(self.console.disallowed_tools):
                raise ValueError("missions.console.tools and disallowed_tools overlap")
            if self.console.model not in self.model_broker.models:
                raise ValueError("missions.console.model must be one of missions.model_broker.models: the broker refuses any other model")
        return self

    @field_validator("windows")
    @classmethod
    def _windows(cls, named: dict[str, list[str]]) -> dict[str, list[str]]:
        from planet_express.missions.window import parse_entry
        for name, entries in named.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not entries or any(parse_entry(e) is None for e in entries):
                raise ValueError(f"missions.windows.{name}: a lowercase name and a list of valid window entries")
        return named

    @field_validator("tool_sites")
    @classmethod
    def _tool_sites(cls, sites: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for tool, urls in sites.items():
            for name, url in urls.items():
                if not re.fullmatch(r"https?://[^\s/@]+(/[^\s]*)?", url) or "@" in url:
                    raise ValueError(f"missions.tool_sites.{tool}.{name}: a plain http(s) URL without credentials")
        return sites

    @field_validator("tool_secret_env")
    @classmethod
    def _tool_secret_env(cls, refs: dict[str, str]) -> dict[str, str]:
        for ref, var in refs.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", ref) or not _ENV_NAME.fullmatch(var) or var.startswith("PE_"):
                raise ValueError("missions.tool_secret_env maps a lowercase secret ref to an environment variable name")
        return refs

    @model_validator(mode="before")
    @classmethod
    def _retired_tripwire_settings(cls, data):
        """Trust mode removed the tripwire (stage 3), and with it `tripwire_interval_s`, `tripwire_investigating_interval_s` and
        `tripwire_accept_blind`. A config.yaml written before then (the live host's has an `accept_blind` list) still loads: the
        keys are accepted and ignored, whatever they hold, because nothing reads them any more and refusing would stop Core."""
        if isinstance(data, dict) and any(k in data for k in RETIRED_TRIPWIRE_KEYS):
            data = {k: v for k, v in data.items() if k not in RETIRED_TRIPWIRE_KEYS}
        return data

    @model_validator(mode="before")
    @classmethod
    def _retired_strict_settings(cls, data):
        """Trust mode stage 4 deleted the protected set (`protected`, `protected_overrides`, `protected_absent`, `live_trees`,
        `cross_host`, `package_paths`), the Docker proxy (`docker_socket`, `docker_label_allow`, `docker_proxy_ports`) and scoped
        worker sudo (`worker_sudo_rules`, `worker_sudo_all_sessions`). A config.yaml that still names them (the live host's has
        `protected_absent`) still loads: they are accepted and ignored, whatever they hold."""
        if isinstance(data, dict) and any(k in data for k in RETIRED_STRICT_KEYS):
            data = {k: v for k, v in data.items() if k not in RETIRED_STRICT_KEYS}
        return data

    @model_validator(mode="before")
    @classmethod
    def _deprecated_read_log_groups(cls, data):
        """`read_log_groups` (before trust mode: the log groups an Observe/Propose session got, only adm and systemd-journal)
        maps onto `session_groups`. It could only ever name groups the new default already holds, and setup rendered it as an
        empty list, so a file that still says it gets the default plus anything it named, never fewer groups than trust mode
        needs. Naming both is refused: one of them would be silently ignored."""
        if isinstance(data, dict) and "read_log_groups" in data:
            data = dict(data)
            old = data.pop("read_log_groups")
            if "session_groups" in data:
                raise ValueError("missions.read_log_groups is the old name of missions.session_groups: set only session_groups")
            if not isinstance(old, list) or not all(isinstance(g, str) for g in old):
                raise ValueError("missions.read_log_groups must be a list of group names")
            data["session_groups"] = list(dict.fromkeys([*DEFAULT_SESSION_GROUPS, *old]))
        return data

    @field_validator("session_groups")
    @classmethod
    def _session_groups(cls, names: list[str]) -> list[str]:
        if (len(set(names)) != len(names) or len(names) > _MAX_SESSION_GROUPS
                or not all(_GROUP_NAME.fullmatch(n) for n in names)):
            raise ValueError(f"missions.session_groups must be at most {_MAX_SESSION_GROUPS} distinct plain group names")
        return names

    @field_validator("worker_user")
    @classmethod
    def _worker_user(cls, name: str) -> str:
        if not _UNIX_NAME.fullmatch(name) or name == "root":
            raise ValueError("missions.worker_user must be a non-root unix account name")
        return name

    @field_validator("sock_root", "workspace_root", "launch_conf")
    @classmethod
    def _plain_absolute_path(cls, path: str | None) -> str | None:
        if path is not None and (not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path
                                 or path == "/"):
            raise ValueError("missions.sock_root, workspace_root and launch_conf must be normalised absolute paths")
        return path

    @model_validator(mode="after")
    def _uid_range(self) -> "MissionsConfig":
        if self.session_uid_max - self.session_uid_min < 2:
            raise ValueError("missions.session_uid_min..session_uid_max needs room for at least two uids "
                             "(the top one is reserved for preflight)")
        import os
        if self.session_uid_min <= os.geteuid() <= self.session_uid_max:
            raise ValueError("the session uid range must not contain the Core user's uid")
        problem = uid_range_problem(self.session_uid_min, self.session_uid_max) if self.enabled else None
        if problem:
            raise ValueError(problem)       # only when missions are on: a config that never used them must still boot
        return self

    @field_validator("worker_socket")
    @classmethod
    def _worker_socket(cls, path: str) -> str:
        if not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path:
            raise ValueError("missions.worker_socket must be a normalised absolute path")
        return path

    @field_validator("cgroup_root")
    @classmethod
    def _cgroup_root(cls, path: str) -> str:
        if (not path.startswith("/") or "\0" in path or posixpath.normpath(path) != path
                or path.rstrip("/") in ("", "/sys", "/sys/fs", "/sys/fs/cgroup")):
            raise ValueError("missions.cgroup_root must be a normalised absolute path below the cgroup mount")
        return path

    @model_validator(mode="after")
    def _cgroup_root_below_a_cgroup2_mount(self) -> "MissionsConfig":
        """When missions are on, the root must be strictly below a cgroup v2 mount point (a delegated subtree)."""
        if not self.enabled:
            return self
        from planet_express.missions.containment_cgroup import cgroup2_mountpoint
        try:
            with open(MOUNTINFO_PATH, encoding="utf-8", errors="replace") as handle:
                mount = cgroup2_mountpoint(self.cgroup_root, handle.read())
        except OSError as exc:
            raise ValueError(f"missions.cgroup_root cannot be verified: cannot read the mount table ({exc.strerror})") from None
        if mount is None:
            raise ValueError(f"missions.cgroup_root {self.cgroup_root} is not on a cgroup v2 mount; "
                             "mission sessions need a delegated cgroup2 subtree")
        if posixpath.normpath(self.cgroup_root) == posixpath.normpath(mount):
            raise ValueError("missions.cgroup_root must be a subdirectory below the cgroup2 mount point, not the mount itself")
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
    # Which command family sudo_allowlist's grants are rendered/checked against --
    # "systemd" means `sudo systemctl <action> <unit>` (casa_bender.py's
    # _SUDO_SYSTEMCTL_RE), "mos" means `sudo service <unit> <action>`
    # (_SUDO_SERVICE_RE) -- note the argument order reverses between the two, it isn't
    # just a renamed command. Defaults to "systemd": every install predating this field
    # ran on systemd, and a fresh install's default (HostControlProvider's only other
    # implementation today) should match that, not silently change behavior. See
    # docs/designs/mos-sudo-gate-scoping.md for why this field has to exist before a
    # MOS sudo gate can be built at all -- Phase 3 deliberately deferred host-type
    # dispatch when only one provider existed; this is that deferred decision.
    host_control_provider: Literal["systemd", "mos"] = "systemd"
    missions: MissionsConfig = MissionsConfig()

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
