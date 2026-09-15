"""
planet_express/execution/policy.py — is an action allowed, automatic, or approval-gated?

Slice 1 autonomy is hardcoded: R0 (observation) runs automatically, everything above R0
needs a human approval, and R4 is always refused. The configurable R0-R4 map arrives in
slice 3. Everything unknown fails closed: unknown action, unknown risk class, or a target
that resolution refused.
"""

from dataclasses import dataclass

from planet_express.execution import actions

RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
AUTOMATIC_RISKS = frozenset({"R0"})
FORBIDDEN_RISKS = frozenset({"R4"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_approval: bool
    risk: str | None
    reason: str


def decide(action: str, target_error: str | None = None) -> PolicyDecision:
    spec = actions.REGISTRY.get(action)
    if spec is None:
        return PolicyDecision(False, False, None, f"unknown action {action!r}")
    if spec.risk not in RISK_LEVELS:
        return PolicyDecision(False, False, spec.risk, f"unknown risk class {spec.risk!r} for {action}")
    if spec.risk in FORBIDDEN_RISKS:
        return PolicyDecision(False, False, spec.risk, f"{action} is {spec.risk}: never allowed")
    if target_error is not None:
        return PolicyDecision(False, False, spec.risk, f"target refused: {target_error}")
    if spec.risk in AUTOMATIC_RISKS:
        return PolicyDecision(True, False, spec.risk, f"{spec.risk}: runs automatically")
    return PolicyDecision(True, True, spec.risk, f"{spec.risk}: needs approval")
