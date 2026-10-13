"""Strict mission models (contract §2, §4, §10). Hashed models carry ints and strings only."""

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from .canonical import canonical_json, sha256_hex

Tier = Literal["observe", "propose", "repair", "change", "critical"]
Status = Literal[
    "draft", "queued", "investigating", "awaiting_decision", "executing", "verifying",
    "completed", "failed", "rolled_back", "cancelled", "blocked", "needs_operator",
    "recovery_required", "paused_tripwire", "rollback_pending",
]
Origin = Literal["incident", "operator", "maintenance", "system", "import"]
EventKind = Literal["state", "tool", "diff", "tripwire", "decision", "artifact", "note", "takeover"]
SessionKind = Literal["local_cgroup", "local_uid", "remote_tunnel"]

# Strict scalars: a float such as 1.0 is refused instead of coerced, so canonical JSON never sees one.
Int = StrictInt
Str = Annotated[StrictStr, Field(max_length=512)]
Text = Annotated[StrictStr, Field(max_length=8192)]
JsonText = Annotated[StrictStr, Field(max_length=262144)]
ResourceId = Annotated[StrictStr, Field(min_length=1, max_length=512)]
ShortList = Annotated[list[Str], Field(max_length=256)]

PHASE_RE = re.compile(r"^(grant|plan|broad_sudo|no_backup|critical:\d{1,6})$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GrantScope(_Strict):
    stacks: ShortList = []
    services: ShortList = []
    paths: ShortList = []
    mounts: ShortList = []
    hosts: ShortList = ["local"]


class Protected(_Strict):
    resources: ShortList = []


class Window(_Strict):
    deny: ShortList = []
    allow: ShortList | None = None


class SudoRule(_Strict):
    unit: Str
    actions: Annotated[list[Literal["start", "stop", "restart"]], Field(max_length=3)]


class Sudo(_Strict):
    mode: Literal["scoped", "broad"] = "scoped"
    rules: Annotated[list[SudoRule], Field(max_length=256)] = []


class Budgets(_Strict):
    wall_s: Annotated[Int, Field(ge=1, le=86400 * 7)] = 3600
    tool_calls: Annotated[Int, Field(ge=0, le=100000)] = 200
    output_bytes: Annotated[Int, Field(ge=0, le=2**40)] = 5_000_000
    retries: Annotated[Int, Field(ge=0, le=100)] = 2


class ResourceEffect(_Strict):
    changes: Annotated[Int, Field(ge=0, le=100000)]


class Effects(_Strict):
    max_unexpected_in_scope: Annotated[Int, Field(ge=0, le=100000)] = 3
    per_resource: dict[Str, ResourceEffect] = {}


class GrantVerification(_Strict):
    checks: ShortList = []
    agreed_at: Annotated[Int, Field(ge=0)] = 0
    plan_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class Backup(_Strict):
    covered: StrictBool = False
    recent_s: Annotated[Int, Field(ge=0)] = 86400
    acknowledged_no_backup: StrictBool = False


class Grant(_Strict):
    v: Literal[1] = 1
    tier: Tier
    scope: GrantScope = GrantScope()
    protected: Protected = Protected()
    exclusions: ShortList = []
    window: Window = Window()
    sudo: Sudo = Sudo()
    credentials: ShortList = []
    budgets: Budgets = Budgets()
    effects: Effects = Effects()
    on_verify_fail: Literal["needs_operator", "rollback"] = "needs_operator"
    docker: Literal["proxy", "raw"] = "proxy"
    verification: GrantVerification = GrantVerification()
    backup: Backup = Backup()

    @model_validator(mode="after")
    def _raw_docker_needs_broad_sudo(self) -> "Grant":
        if self.docker == "raw" and self.sudo.mode != "broad":
            raise ValueError("docker: raw requires sudo.mode = broad")
        return self

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


CheckName = Literal[
    "http", "exec_probe", "container_state", "mount_readable", "project_complete",
    "log_absent", "file_state", "query",
]
ParamScalar = StrictStr | StrictInt | StrictBool


class VerificationCheck(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    check: CheckName
    params: dict[Str, ParamScalar | Annotated[list[ParamScalar], Field(max_length=64)]] = {}


class VerificationPlan(_Strict):
    catalogue_v: Annotated[Int, Field(ge=1)] = 1
    checks: Annotated[list[VerificationCheck], Field(max_length=64)] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> "VerificationPlan":
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("verification check ids must be unique")
        return self

    def hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class Mission(_Strict):
    id: Str
    title: Annotated[StrictStr, Field(max_length=256)]
    origin: Origin
    incident_id: Str | None = None
    priority: Int = 50
    status: Status
    tier: Tier
    goal: Text
    success_criteria: Text
    grant_json: JsonText
    grant_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    standing_grant_id: Str | None = None
    runtime: Str
    session_ref: Str | None = None
    cancel_requested_at: Int | None = None
    cancel_requested_by: Str | None = None
    plan_json: JsonText | None = None
    plan_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    checkpoint_id: Str | None = None
    verification_plan_json: JsonText | None = None
    verification_result_json: JsonText | None = None
    outcome: Str | None = None
    outcome_flags: Str | None = None
    created_at: Int
    updated_at: Int
    closed_at: Int | None = None
    schema_ver: Int = 1

    def grant(self) -> Grant:
        return Grant.model_validate_json(self.grant_json)


class MissionEvent(_Strict):
    seq: Int
    mission_id: Str
    at: Int
    kind: EventKind
    actor: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    body_json: StrictStr
    redaction_applied: Int = 1


class Lease(_Strict):
    resource: ResourceId
    mission_id: Str
    fence: Int
    acquired_at: Int
    expires_at: Int
    last_heartbeat: Int
    suspect_at: Int | None = None


class StandingGrant(_Strict):
    id: Str
    scope: GrantScope
    tier: Tier
    window: Window | None = None
    created_by: Str
    created_at: Int
    expires_at: Int
    revoked_at: Int | None = None
    version: Int = 1


class Approval(_Strict):
    id: Str
    mission_id: Str
    phase: Annotated[StrictStr, Field(pattern=PHASE_RE.pattern)]
    bound_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    decision: Literal["approve", "deny"]
    operator: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    elevated: Int
    decided_at: Int
