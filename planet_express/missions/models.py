"""Strict mission models (contract §2, §4, §10). Hashed models carry ints and strings only."""

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from .canonical import canonical_json, loads_strict, sha256_hex

Tier = Literal["observe", "propose", "repair", "change", "critical"]
Status = Literal[
    "draft", "queued", "investigating", "awaiting_decision", "executing", "verifying",
    "completed", "failed", "rolled_back", "cancelled", "blocked", "needs_operator",
    "recovery_required", "paused_tripwire", "rollback_pending",      # paused_tripwire: legacy rows only (see state.LEGACY_STATES)
]
Origin = Literal["incident", "operator", "maintenance", "system", "import"]
EventKind = Literal["state", "tool", "diff", "tripwire", "decision", "artifact", "note", "takeover"]
# Event kinds only rows written before trust mode removed the tripwire carry: still readable, never written again.
LEGACY_EVENT_KINDS = frozenset({"tripwire", "takeover"})
SessionKind = Literal["local_cgroup", "local_uid", "remote_tunnel"]

# Strict scalars: a float such as 1.0 is refused instead of coerced, so canonical JSON never sees one.
Int = StrictInt
Str = Annotated[StrictStr, Field(max_length=512)]
Text = Annotated[StrictStr, Field(max_length=8192)]
GRANT_JSON_MAX = 262144                  # a stored JSON document, the grant among them
JsonText = Annotated[StrictStr, Field(max_length=GRANT_JSON_MAX)]
ResourceId = Annotated[StrictStr, Field(min_length=1, max_length=512)]
ShortList = Annotated[list[Str], Field(max_length=256)]

# Approval phases. `broad_sudo` and `no_backup` are only on rows written before trust mode: still readable, never asked for.
PHASE_RE = re.compile(r"^(grant|plan|broad_sudo|no_backup|critical:\d{1,6})$")

# What trust mode (rc14) took out of a grant. A row stored before then still loads (`Grant.load`): these are dropped, and the
# grant it gives no longer hashes to the stored hash, so an approval made before the upgrade can never start a run.
LEGACY_GRANT_KEYS = frozenset({"protected", "exclusions", "sudo", "docker", "backup", "effects"})
LEGACY_SCOPE_KEYS = frozenset({"paths", "mounts"})
LEGACY_CEILING_KEYS = frozenset({"sudo_units", "exclusions"})
LEGACY_GRANT_TEXT = ("this was approved before PE's trust-mode update changed what an approval covers, so it will not run. "
                     "Close it and confirm the plan again.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _without(doc: Any, keys: frozenset[str]) -> Any:
    return {k: v for k, v in doc.items() if k not in keys} if isinstance(doc, dict) else doc


class GrantScope(_Strict):
    """What a Repair works on: the stacks and services its leases cover and its verification is about."""
    stacks: ShortList = []
    services: ShortList = []
    hosts: ShortList = ["local"]

    @classmethod
    def load(cls, doc: Any) -> "GrantScope":
        """A stored scope (a mission's or a standing grant's): one from before trust mode may still carry paths and mounts."""
        return cls.model_validate(_without(doc, LEGACY_SCOPE_KEYS))


class StandingCeilings(_Strict):
    """What an unattended start under a standing grant may carry beyond its scope. Everything defaults to nothing: no brokered
    tools and budgets no larger than `Budgets()`'s own defaults."""
    credentials: ShortList = []              # the `tool:<name>` credentials a mission may name
    budgets: "Budgets | None" = None         # per-field maxima; None means the model defaults

    @classmethod
    def load(cls, doc: Any) -> "StandingCeilings":
        return cls.model_validate(_without(doc, LEGACY_CEILING_KEYS))


class Window(_Strict):
    deny: ShortList = []
    allow: ShortList | None = None


class Budgets(_Strict):
    wall_s: Annotated[Int, Field(ge=1, le=86400 * 7)] = 1800
    tool_calls: Annotated[Int, Field(ge=0, le=100000)] = 200
    output_bytes: Annotated[Int, Field(ge=0, le=2**40)] = 4 * 1024 * 1024
    retries: Annotated[Int, Field(ge=0, le=100)] = 2


class GrantVerification(_Strict):
    checks: ShortList = []
    agreed_at: Annotated[Int, Field(ge=0)] = 0
    plan_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class Grant(_Strict):
    """Trust mode: a grant is the tier, what it works on (for leases and verification), its budgets and the verification plan it is
    judged by, all hash-bound. The session's access is not in it: every session has full access to the host. `credentials` are the
    brokered tools (`tool:<name>`) the mission may call."""
    v: Literal[1] = 1
    tier: Tier
    scope: GrantScope = GrantScope()
    window: Window = Window()
    credentials: ShortList = []
    budgets: Budgets = Budgets()
    on_verify_fail: Literal["needs_operator", "rollback"] = "needs_operator"
    verification: GrantVerification = GrantVerification()

    @classmethod
    def load(cls, text: str) -> "Grant":
        """A grant read back from the store. New grants are strict (`model_validate`); a stored one may be from before trust mode,
        so the fields that update removed are dropped. Whatever else is wrong is still refused."""
        doc = loads_strict(text) if isinstance(text, str) else text
        if isinstance(doc, dict):
            doc = _without(doc, LEGACY_GRANT_KEYS)
            if isinstance(doc.get("scope"), dict):
                doc["scope"] = _without(doc["scope"], LEGACY_SCOPE_KEYS)
        return cls.model_validate(doc)

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


def is_legacy_grant(text: str) -> bool:
    """A stored grant written before trust mode (it names a field that update removed)."""
    try:
        doc = loads_strict(text)
    except Exception:                       # noqa: BLE001 - unreadable is not "legacy"; the strict reader says what is wrong
        return False
    if not isinstance(doc, dict):
        return False
    scope = doc.get("scope")
    return bool(LEGACY_GRANT_KEYS & set(doc)) or (isinstance(scope, dict) and bool(LEGACY_SCOPE_KEYS & set(scope)))


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
        return Grant.load(self.grant_json)


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
