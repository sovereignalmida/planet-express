"""What the wizard collects. `plan()` turns these plus a discovery report into a reviewable plan.

Strict on purpose: unknown fields are rejected, names are validated, and secrets are `SecretStr`
so they cannot leak through a repr or a log line.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from config_schema import MissionsConfig, WorkerSudoRuleConfig, uid_range_problem
from planet_express.setup.steps import AbsPath, Name

_MISSIONS_DEFAULTS = MissionsConfig()      # the one source of the defaults, so config, Core and setup cannot drift

# Powers are risk tiers, because `autonomy.forbidden_risks` is the only gate the config can enforce
# (docs/designs/setup-plan.md, invariant 3). Canary updates and safe prune are both R2, so they
# cannot be switched apart.
Tier = Literal["observe", "restart", "stacks", "full"]
FORBIDDEN_RISKS_BY_TIER: dict[str, list[str]] = {
    "observe": ["R1", "R2", "R3", "R4"],
    "restart": ["R2", "R3", "R4"],
    "stacks": ["R3", "R4"],
    "full": ["R4"],
}

_OPERATOR = re.compile(r"^[a-z0-9_.-]{1,32}$")
_UNIT = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def _single_line(value: SecretStr) -> SecretStr:
    text = value.get_secret_value()
    if not text or any(c in text for c in "\r\n\"'\\$`"):
        raise ValueError("must be a single line without quotes, backslashes or shell characters")
    return value


Secret = Annotated[SecretStr, AfterValidator(_single_line)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Telegram(_Strict):
    token: Secret
    chat_id: Secret


class Llm(_Strict):
    provider: Literal["openai", "anthropic"]
    api_key: Secret | None = None


class Operator(_Strict):
    name: str
    passphrase: Secret
    totp_secret: Secret

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _OPERATOR.fullmatch(value):
            raise ValueError("1-32 lowercase letters, digits, _, . or -")
        return value

    @field_validator("passphrase")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 12:
            raise ValueError("a passphrase needs at least 12 characters")
        return value


class SudoUnit(_Strict):
    unit: str
    actions: list[Literal["start", "stop", "restart"]] = ["start", "stop", "restart"]

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        if not _UNIT.fullmatch(value):
            raise ValueError("not a safe unit name")
        return value


class MissionsAnswers(_Strict):
    """Set up the host for mission control (the session launcher, docs/designs/v4-slice-0-contracts.md 22.5). Off unless
    `enabled` is said outright, so an answer file that mentions `missions` without it changes nothing. The values default
    to `missions.*` in config_schema; the same ones must be in the config Core reads, because launch.conf is written from
    these and the helper checks Core's requests against it."""
    enabled: bool = False
    session_uid_min: int = _MISSIONS_DEFAULTS.session_uid_min
    session_uid_max: int = _MISSIONS_DEFAULTS.session_uid_max
    sock_root: AbsPath = _MISSIONS_DEFAULTS.sock_root
    cgroup_root: AbsPath = _MISSIONS_DEFAULTS.cgroup_root
    workspace_root: AbsPath | None = None      # unset: `mission-workspaces` in the state directory, as Core defaults it
    worker_sudo_rules: list[WorkerSudoRuleConfig] = []   # the worker tier's sudo ceiling; empty: no worker sudoers file
    worker_sudo_all_sessions: bool = False               # required to generate it: every session uid gets the ceiling

    @model_validator(mode="after")
    def _as_the_config_would_accept_it(self):
        try:
            MissionsConfig(enabled=False, session_uid_min=self.session_uid_min, session_uid_max=self.session_uid_max,
                           sock_root=self.sock_root, cgroup_root=self.cgroup_root, workspace_root=self.workspace_root)
        except ValidationError as exc:
            raise ValueError(str(exc.errors()[0]["msg"]).removeprefix("Value error, ")) from None
        if self.enabled:
            # The config is built with `enabled=False` above so that host-dependent checks (cgroup access) do not run at answer time,
            # but that also skips the pure range arithmetic Core applies to an ENABLED config. A range Core would refuse at startup
            # must not be accepted here, or the wizard installs a launcher for a setup that cannot start.
            problem = uid_range_problem(self.session_uid_min, self.session_uid_max)
            if problem:
                raise ValueError(problem)
        return self


class SetupAnswers(_Strict):
    story: Literal["fresh", "adopt", "repair", "uninstall"] = "fresh"
    # The account the service runs as. Root is refused except on a host with no sudo, and only when
    # `accept_root_service` is set (invariant 1).
    run_as: Name
    run_group: Name | None = None  # defaults to run_as
    accept_root_service: bool = False
    install_dir: AbsPath
    stacks_root: AbsPath
    # Stacks PE must leave completely alone (config `forbidden_stacks`).
    ignored_stacks: list[Name] = []
    tier: Tier = "observe"
    # Only used at tier "full" on a systemd host: the units Bender may start/stop/restart via sudo.
    sudo_units: list[SudoUnit] = []
    dashboard_port: int = Field(default=8420, ge=1, le=65535)
    telegram: Telegram | None = None
    llm: Llm | None = None
    operator: Operator | None = None
    start_services: bool = False
    # Absent (the default) or `enabled: false`: nothing about mission control is planned.
    missions: MissionsAnswers | None = None
