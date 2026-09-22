"""
planet_express/execution/policy.py — is an action allowed, automatic, or approval-gated?

Configurable forbidden and directly requestable risks, with structural autonomy limits:
R0 observations run automatically; mutations require human approval. Unknown actions,
unknown risk classes and refused targets fail closed.
"""

from dataclasses import dataclass

import config
from config_schema import AutonomyConfig
from planet_express.execution import actions, runbook as runbooks

RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
OPERATOR_ORIGINS = frozenset({"telegram", "telegram-direct", "dashboard", "dashboard-direct"})
RUNBOOK_ORIGINS = frozenset({
    "telegram", "telegram-direct", "dashboard", "dashboard-direct", "incident", "chat",
    "planner", "zoidberg", "system", "install", "amy",
})
DIRECT_RUNBOOK_ORIGINS = frozenset({"telegram-direct", "dashboard-direct"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_approval: bool
    risk: str | None
    reason: str


@dataclass(frozen=True)
class RunbookDecision:
    allowed: bool
    needs_approval: bool
    automatic: bool
    risk: str | None
    reason: str
    pairs: list[tuple[str, str]]


def decide(
    action: str, target_error: str | None = None, *, autonomy: AutonomyConfig | None = None,
) -> PolicyDecision:
    autonomy = config.AUTONOMY if autonomy is None else autonomy
    spec = actions.REGISTRY.get(action)
    if spec is None:
        return PolicyDecision(False, False, None, f"unknown action {action!r}")
    if spec.risk not in RISK_LEVELS:
        return PolicyDecision(False, False, spec.risk, f"unknown risk class {spec.risk!r} for {action}")
    if spec.risk in autonomy.forbidden_risks:
        return PolicyDecision(False, False, spec.risk, f"{action} is {spec.risk}: never allowed")
    if target_error is not None:
        return PolicyDecision(False, False, spec.risk, f"target refused: {target_error}")
    # D23: non-rollbackable mutations must never become automatic, even if the
    # automatic-risk rule below is expanded in a future edit.
    if spec.risk != "R0" and not spec.rollbackable:
        return PolicyDecision(True, True, spec.risk, f"{spec.risk}: needs approval")
    # D31: only R0 reads are automatic; rollbackable restarts still need approval.
    if spec.risk == "R0":
        return PolicyDecision(True, False, spec.risk, f"{spec.risk}: runs automatically")
    return PolicyDecision(True, True, spec.risk, f"{spec.risk}: needs approval")


def allows_direct_request(risk: str, *, autonomy: AutonomyConfig | None = None) -> bool:
    autonomy = config.AUTONOMY if autonomy is None else autonomy
    return risk in autonomy.direct_request_risks


def decide_runbook(
    runbook: runbooks.Runbook, origin: str, *, autonomy: AutonomyConfig | None = None,
) -> RunbookDecision:
    """Apply the existing risk policy to a fully validated multi-step runbook."""
    autonomy = config.AUTONOMY if autonomy is None else autonomy
    if origin not in RUNBOOK_ORIGINS:
        return RunbookDecision(False, False, False, None, f"unknown origin {origin!r}", [])
    unknown = [step.type for step in runbook.steps if step.type not in runbooks.STEP_TYPES]
    if unknown:
        return RunbookDecision(
            False, False, False, None, f"unknown step type {unknown[0]!r}", [],
        )
    step_risks = [runbooks.STEP_TYPES[step.type].risk for step in runbook.steps]
    pairs = runbooks.mutating_pairs(runbook)
    unknown_risks = [value for value in step_risks if value not in RISK_LEVELS]
    if unknown_risks:
        return RunbookDecision(
            False, False, False, unknown_risks[0],
            f"unknown risk class {unknown_risks[0]!r}", pairs,
        )
    computed_risk = runbooks.risk(runbook)
    if computed_risk in autonomy.forbidden_risks:
        return RunbookDecision(
            False, False, False, computed_risk,
            f"runbook is {computed_risk}: never allowed", pairs,
        )
    if origin in DIRECT_RUNBOOK_ORIGINS:
        if not allows_direct_request(computed_risk, autonomy=autonomy):
            return RunbookDecision(
                False, False, False, computed_risk,
                f"{computed_risk}: not directly requestable", pairs,
            )
        return RunbookDecision(
            True, False, False, computed_risk, f"{computed_risk}: direct operator request", pairs,
        )
    automatic = (
        origin == "system" and all(step.type == "prune.safe" for step in runbook.steps)
    ) or (
        origin == "zoidberg" and all(step.type == "update.canary" for step in runbook.steps)
    )
    if automatic:
        return RunbookDecision(
            True, False, True, computed_risk, f"{computed_risk}: approved automatic exception", pairs,
        )
    if computed_risk == "R0":
        return RunbookDecision(
            True, False, True, computed_risk, "R0: runs automatically", pairs,
        )
    return RunbookDecision(
        True, True, False, computed_risk, f"{computed_risk}: needs approval", pairs,
    )


DAY_SECONDS = 86400


def limit_lookback_seconds(*, autonomy: AutonomyConfig | None = None) -> float:
    """How far back attempt history must be read to enforce BOTH limits. A cooldown longer than
    the 24h cap window would otherwise expire early (Codex review, T24)."""
    autonomy = config.AUTONOMY if autonomy is None else autonomy
    return max(DAY_SECONDS, autonomy.cooldown_seconds)


def limit_refusal(
    attempts: list[float], now: float, *, autonomy: AutonomyConfig | None = None,
) -> str | None:
    """Check cooldown first, then the rolling 24-hour attempt cap."""
    autonomy = config.AUTONOMY if autonomy is None else autonomy
    if attempts and now - max(attempts) < autonomy.cooldown_seconds:
        minutes = int((now - max(attempts)) // 60)
        cooldown = autonomy.cooldown_seconds // 60
        return f"cooling down: last attempt {minutes}m ago, cooldown {cooldown}m"
    count = sum(attempt >= now - DAY_SECONDS for attempt in attempts)
    if count >= autonomy.max_attempts_per_day:
        return f"attempt cap reached: {count} in 24h (max {autonomy.max_attempts_per_day})"
    return None
