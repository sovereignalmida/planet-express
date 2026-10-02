"""Reconciling PE's three-plus existing health/severity representations into one `HealthState`
(v3 Phase 1 goal 4, `docs/designs/phase-1-state-model.md`).

Before this, "is X okay" was answered three incompatible ways depending which code asked:
`actions.py`'s `HealthReading`/`container_health()` (raw docker state: `healthy|unhealthy|
starting|none`, returned as a bare `(bool, str)`), `incidents.py`'s per-observation
`condition`/`severity` pair (`healthy|failing|unknown` crossed with uppercase
`CRITICAL|HIGH|MEDIUM`), and Hermes's own prompt-level `CRITICAL|HIGH|MEDIUM|LOW` scheme, further
flattened to two booleans (`has_critical`, `has_high`) by the time `state_models.Findings`
persists it. This module does not touch any of those three -- it adds one place that reads their
output and returns the same `HealthState` either way, so a Phase 4+ consumer stops caring which
one produced a given reading.

`Liveness` (`core/hosts.py` -- is this *reading* fresh, not is the *resource* healthy) is
deliberately not folded in here, per the design doc: it is an orthogonal axis and stays a
separate field wherever both are needed.

One real wrinkle, not papered over: `incidents.py` sometimes pairs `condition="unknown"` with a
real severity (`stack_completeness`, when some services couldn't be checked but the ones that
could look bad) -- that is not "no information," it is "degraded confidence," so it maps to
`HealthState("degraded", ...)` here, not `HealthState("unknown", ...)`. True "no information"
(`condition="unknown", severity=None`) maps straight through to `HealthState("unknown")`.
"""

from planet_express.core.state import HealthState, Severity

_SEVERITY_MAP: dict[str, Severity] = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}
# A failing/degraded reading must carry *a* severity for HealthState's own invariant to hold;
# an unrecognized or missing string on an otherwise-failing observation defaults here rather
# than raising, because the condition itself is still real and worth keeping.
_FALLBACK_SEVERITY: Severity = "medium"


def _normalize_severity(raw: str | None) -> Severity:
    if raw is None:
        return _FALLBACK_SEVERITY
    return _SEVERITY_MAP.get(raw.strip().lower(), _FALLBACK_SEVERITY)


def from_observation(condition: str, severity: str | None) -> HealthState:
    """`incidents.py`'s `(condition, severity)` pair -> `HealthState`. Covers the vocabulary
    actually emitted there: `healthy`, `failing`, `unknown` crossed with `None` or an uppercase
    severity string (see module docstring for the `unknown`+severity wrinkle)."""
    if condition == "healthy":
        return HealthState(condition="healthy")
    if condition == "unknown" and severity is None:
        return HealthState(condition="unknown")
    if condition == "unknown":
        return HealthState(condition="degraded", severity=_normalize_severity(severity))
    if condition in ("failing", "unhealthy", "degraded"):
        return HealthState(condition="unhealthy", severity=_normalize_severity(severity))
    return HealthState(condition="unknown", reason=f"unrecognized condition: {condition!r}")


def from_docker_reading(
    *,
    error: str | None,
    status: str,
    restart_count: int,
    health: str,
    baseline_restarts: int = 0,
) -> HealthState:
    """The fields of `actions.py`'s `HealthReading` -> `HealthState`, mirroring
    `container_health()`'s exact branch order and verdicts (not imported directly: `core` does
    not depend on `execution`, so this takes the same primitives `read_health()` already
    returns rather than the dataclass itself)."""
    if error is not None:
        return HealthState(condition="unknown", reason=f"inspect failed: {error}")
    if status != "running":
        return HealthState(condition="unhealthy", severity="high", reason=f"status={status}")
    if health == "unhealthy":
        return HealthState(condition="unhealthy", severity="high", reason="healthcheck failing")
    restarts = restart_count - baseline_restarts
    if restarts >= 1:
        return HealthState(
            condition="degraded", severity="medium",
            reason=f"restarted {restarts}x during watch window",
        )
    return HealthState(condition="healthy")
