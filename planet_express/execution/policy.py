"""
planet_express/execution/policy.py — is an action allowed, automatic, or approval-gated?

Configurable forbidden and directly requestable risks, with structural autonomy limits:
R0 observations run automatically; mutations require human approval. Unknown actions,
unknown risk classes and refused targets fail closed.
"""

from dataclasses import dataclass

import config
from config_schema import AutonomyConfig
from planet_express.execution import actions

RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
OPERATOR_ORIGINS = frozenset({"telegram", "dashboard", "dashboard-direct"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_approval: bool
    risk: str | None
    reason: str


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
