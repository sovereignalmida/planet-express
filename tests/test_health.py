"""planet_express/core/health.py (v3 Phase 1 goal 4): reconciling incidents.py's and
actions.py's severity/health vocabularies into one HealthState."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core import health
from planet_express.core.state import HealthState

# --- from_observation (incidents.py's condition/severity pair) -----------------------------


def test_healthy_observation():
    assert health.from_observation("healthy", None) == HealthState(condition="healthy")


def test_truly_unknown_observation_has_no_severity():
    assert health.from_observation("unknown", None) == HealthState(condition="unknown")


def test_unknown_with_severity_is_degraded_not_unknown():
    """The real stack_completeness wrinkle: condition="unknown" with a real severity means
    degraded confidence, not no information."""
    result = health.from_observation("unknown", "MEDIUM")
    assert result.condition == "degraded"
    assert result.severity == "medium"


def test_failing_observation_maps_to_unhealthy():
    result = health.from_observation("failing", "CRITICAL")
    assert result.condition == "unhealthy"
    assert result.severity == "critical"


def test_failing_with_no_severity_falls_back_rather_than_crashing():
    result = health.from_observation("failing", None)
    assert result.condition == "unhealthy"
    assert result.severity == "medium"


def test_unrecognized_severity_string_falls_back():
    result = health.from_observation("failing", "SOMETHING_NEW")
    assert result.severity == "medium"


def test_unrecognized_condition_becomes_unknown_with_reason():
    result = health.from_observation("sideways", "HIGH")
    assert result.condition == "unknown"
    assert result.severity is None
    assert "sideways" in result.reason


# --- from_docker_reading (actions.py's HealthReading fields) -------------------------------


def test_inspect_error_is_unknown():
    result = health.from_docker_reading(error="no such container", status="", restart_count=0, health="")
    assert result.condition == "unknown"
    assert result.severity is None
    assert "no such container" in result.reason


def test_not_running_is_unhealthy_high():
    result = health.from_docker_reading(error=None, status="exited", restart_count=0, health="none")
    assert result == HealthState(condition="unhealthy", severity="high", reason="status=exited")


def test_failing_healthcheck_is_unhealthy_high():
    result = health.from_docker_reading(error=None, status="running", restart_count=0, health="unhealthy")
    assert result.condition == "unhealthy"
    assert result.severity == "high"


def test_restart_since_baseline_is_degraded_medium():
    result = health.from_docker_reading(
        error=None, status="running", restart_count=3, health="none", baseline_restarts=1,
    )
    assert result.condition == "degraded"
    assert result.severity == "medium"
    assert "2x" in result.reason


def test_no_healthcheck_and_no_restarts_is_healthy():
    result = health.from_docker_reading(error=None, status="running", restart_count=0, health="none")
    assert result == HealthState(condition="healthy")


def test_starting_healthcheck_maps_to_unknown_not_healthy():
    """Deliberate departure from container_health() (which treats "starting" as OK, a narrower
    "did the action succeed" question) -- matches incidents.py's own ("unknown", None) read of
    the same raw state, so from_observation and from_docker_reading agree."""
    result = health.from_docker_reading(error=None, status="running", restart_count=0, health="starting")
    assert result.condition == "unknown"
    assert result.severity is None


def test_restart_churn_outranks_starting():
    """Positive evidence of a problem beats "no signal yet"."""
    result = health.from_docker_reading(
        error=None, status="running", restart_count=2, health="starting", baseline_restarts=0,
    )
    assert result.condition == "degraded"
