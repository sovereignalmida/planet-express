"""The verification plan and its result (contract 10, 2).

The plan model itself is `models.VerificationPlan` (strict, canonical, hashed). This module adds what the grant needs:
every check's params are validated against the check's own strict model (an unknown kind or an extra param REFUSES, it
is never dropped), and `binding_problems` proves a plan is the one the grant's `verification.plan_sha256` names.
`VerificationResult` is what the verifier stores on the mission and what `state.py` demands before `completed`.
"""

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

from ..canonical import CanonicalError, canonical_json, loads_strict, sha256_hex
from ..models import Grant, VerificationPlan

CATALOGUE_V = 1
MAX_RESULT_CHECKS = 64


class PlanInvalid(ValueError):
    """The plan is not a valid, supported verification plan."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CheckResult(_Strict):
    id: Annotated[StrictStr, Field(max_length=128)]
    check: Annotated[StrictStr, Field(max_length=32)]
    ok: StrictBool
    attempts: StrictInt = 1
    detail: Annotated[StrictStr, Field(max_length=200)] = ""


class FindingRef(_Strict):
    kind: Annotated[StrictStr, Field(max_length=64)]
    resource_id: Annotated[StrictStr, Field(max_length=512)]
    severity: Annotated[StrictStr, Field(max_length=16)] = ""


class ScanResult(_Strict):
    ok: StrictBool
    error: Annotated[StrictStr, Field(max_length=200)] = ""
    baseline_findings: StrictInt = 0
    fresh_findings: StrictInt = 0
    new_in_scope: Annotated[list[FindingRef], Field(max_length=20)] = []
    new_in_scope_count: StrictInt = 0
    new_out_of_scope: StrictInt = 0


class VerificationResult(_Strict):
    """Bound to the grant hash and checkpoint it was produced for: a result for anything else is worth nothing."""
    v: Literal[1] = 1
    status: Literal["passed", "failed"]
    grant_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    checkpoint_id: Annotated[StrictStr, Field(max_length=512)] | None = None
    plan_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    at: StrictInt
    reason: Annotated[StrictStr, Field(max_length=300)] = ""
    checks: Annotated[list[CheckResult], Field(max_length=MAX_RESULT_CHECKS)] = []
    scan: ScanResult | None = None

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))


def parse_result(text: str | None) -> VerificationResult | None:
    """The stored result, or None when there is none or it does not parse (never an exception: a gate fails closed)."""
    if not text:
        return None
    try:
        return VerificationResult.model_validate(loads_strict(text))
    except (CanonicalError, ValidationError, ValueError):
        return None


def validate_plan(plan: VerificationPlan, *, catalogue_v: int = CATALOGUE_V) -> VerificationPlan:
    """Every check's params parse against its strict model. Raises PlanInvalid naming the check, never the value."""
    from .checks import PARAM_MODELS  # lazy: keeps this module importable from state.py
    if plan.catalogue_v != catalogue_v:
        raise PlanInvalid(f"catalogue_v {plan.catalogue_v} is not supported (this build runs {catalogue_v})")
    if not plan.checks:
        raise PlanInvalid("a verification plan needs at least one check")
    for check in plan.checks:
        model = PARAM_MODELS.get(check.check)
        if model is None:
            raise PlanInvalid(f"check {check.id!r}: unknown kind {check.check!r}")
        try:
            model.model_validate(check.params)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e.get("loc", ())) or "?" for e in exc.errors()})
            raise PlanInvalid(f"check {check.id!r}: bad params ({', '.join(fields)})") from None
        except ValueError:
            raise PlanInvalid(f"check {check.id!r}: bad params") from None
    return plan


def parse_plan(text: str | dict[str, Any]) -> VerificationPlan:
    """Strict parse (no duplicate keys, floats or NaN) then `validate_plan`."""
    try:
        raw = loads_strict(text) if isinstance(text, str) else text
        plan = VerificationPlan.model_validate(raw)
    except (CanonicalError, ValidationError, ValueError) as exc:
        raise PlanInvalid(f"verification plan is invalid: {type(exc).__name__}") from None
    return validate_plan(plan)


def plan_hash(plan: VerificationPlan) -> str:
    return sha256_hex(plan.model_dump(mode="json"))


def binding_problems(grant: Grant, plan: VerificationPlan | None) -> list[str]:
    """Why `plan` is not the plan this grant approves; empty means it is. Mutating tiers need a plan, always."""
    if grant.tier in ("observe", "propose"):
        return []
    if not grant.verification.plan_sha256:
        return ["the grant does not carry verification.plan_sha256"]
    if plan is None:
        return ["the verification plan is missing"]
    out: list[str] = []
    try:
        validate_plan(plan)
    except PlanInvalid as exc:
        out.append(str(exc))
    if plan_hash(plan) != grant.verification.plan_sha256:
        out.append("the verification plan does not match the grant's plan_sha256")
    if set(grant.verification.checks) != {c.id for c in plan.checks}:
        out.append("grant.verification.checks does not list exactly the plan's check ids")
    return out
