"""Host types and staleness (the Beszel-era locality predicate went with Beszel, multi-host release C)."""
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.hosts import (
    CURRENT,
    SKEW_TOLERANCE,
    STALE,
    STALE_AFTER,
    UNKNOWN,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    liveness,
    parse_timestamp,
    unknown,
)

def test_stale_after_is_twice_the_one_minute_bucket():
    assert STALE_AFTER == 120


def test_current_reading():
    at = parse_timestamp("2026-10-01 10:55:56.571Z")
    result = liveness(at, now=at + 30)
    assert result.state == CURRENT
    assert result.is_current
    assert result.age == pytest.approx(30)


def test_reading_at_the_threshold_is_still_current():
    assert liveness(1000.0, now=1000.0 + STALE_AFTER).state == CURRENT


def test_reading_past_the_threshold_is_stale():
    result = liveness(1000.0, now=1000.0 + STALE_AFTER + 1)
    assert result.state == STALE
    assert result.age == pytest.approx(121)
    assert "121s old" in result.reason


def test_a_reading_slightly_ahead_of_us_is_current_not_negative():
    result = liveness(1000.0, now=995.0)
    assert result.state == CURRENT
    assert result.age == 0.0


def test_no_reading_is_unknown_with_no_age():
    result = liveness(None, now=1000.0)
    assert result.state == UNKNOWN
    assert result.age is None, "age 0 would read as a reading from this instant"
    assert result.reason


def test_unparseable_timestamp_is_unknown_not_epoch_zero():
    result = liveness("not a timestamp", now=1000.0)
    assert result.state == UNKNOWN
    assert result.age is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 10 ** 400, True])
def test_timestamps_that_are_not_times_are_unknown(value):
    assert parse_timestamp(value) is None


def test_caller_supplied_reason_wins():
    assert liveness(None, now=0.0, reason="collector is down").reason == "collector is down"


def test_unknown_host_has_a_reason_and_no_age():
    result = unknown("not reported by the collector")
    assert (result.state, result.age) == (UNKNOWN, None)
    assert result.reason == "not reported by the collector"
    assert not result.is_current


def test_liveness_states_are_the_three_named_ones():
    assert {CURRENT, STALE, UNKNOWN} == {"current", "stale", "unknown"}


@pytest.mark.parametrize("value,expected", [
    ("2026-10-01 10:55:56.571Z", 1790852156.571),
    ("2026-10-01T10:55:56.571Z", 1790852156.571),
    # Naive, as the collector sometimes writes it: read as UTC, not as local time.
    ("2026-10-01 10:55:56.571", 1790852156.571),
    ("2026-10-01 10:55:56.571+00:00", 1790852156.571),
])
def test_timestamp_parsing(value, expected):
    assert parse_timestamp(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "   ", None, {}, [], "2026-13-45 99:99:99Z"])
def test_timestamps_that_are_not_strings_or_dates_are_none(value):
    assert parse_timestamp(value) is None


def test_host_details_defaults_are_none():
    details = HostDetails()
    for field, value in vars(details).items():
        assert value is None, f"{field} defaults to {value!r}, not None"


def test_host_metrics_defaults_are_none():
    metrics = HostMetrics()
    for field, value in vars(metrics).items():
        assert value is None, f"{field} defaults to {value!r}, not None"


def test_remote_container_defaults_are_none():
    container = RemoteContainer(name="CASA_ABS")
    for field, value in vars(container).items():
        if field == "name":
            continue
        assert value is None, f"{field} defaults to {value!r}, not None"


def test_unreadable_is_distinguishable_from_genuinely_zero():
    # An idle host and a host nobody could read must not render the same.
    idle = HostMetrics(cpu_pct=0.0, mem_pct=0.0, temps={})
    unread = HostMetrics()
    assert idle.cpu_pct == 0.0 and unread.cpu_pct is None
    assert idle.temps == {} and unread.temps is None
    assert idle != unread


def test_an_empty_os_name_stays_distinct_from_an_absent_one():
    # Unraid's 0.17.0 agent really does report "". That is a value the collector sent; a host
    # we could not read has None, and the renderer treats the two differently.
    assert HostDetails(os_name="").os_name == ""
    assert HostDetails().os_name is None


def test_updatable_is_three_states():
    assert RemoteContainer(name="a", updatable=True).updatable is True
    assert RemoteContainer(name="a", updatable=False).updatable is False
    assert RemoteContainer(name="a").updatable is None


def test_types_are_frozen():
    container = RemoteContainer(name="a")
    with pytest.raises(FrozenInstanceError):
        container.name = "other"


def test_liveness_defaults_carry_no_false_precision():
    assert Liveness(UNKNOWN).age is None
    assert Liveness(UNKNOWN).reason is None


# --- the module stays pure ---------------------------------------------------------------

def test_module_does_no_io():
    source = (Path(__file__).resolve().parent.parent
              / "planet_express" / "core" / "hosts.py").read_text()
    for forbidden in ("import socket", "import httpx", "import requests", "import docker",
                      "import subprocess", "urlopen", "open(", "Path("):
        assert forbidden not in source, f"hosts.py must stay pure: found {forbidden!r}"


# --- clock skew is tolerated, but bounded -------------------------------------------------
# Unbounded, the age floor makes a fast-clocked host read `current` forever: its age pins at
# zero however long ago its agent died. Found by running liveness() at extremes rather than by
# reading it -- neither review caught it.

def test_small_skew_ahead_is_tolerated_and_reads_current():
    for ahead in (0.5, 2.0, SKEW_TOLERANCE):
        lv = liveness(1_000_000.0 + ahead, now=1_000_000.0)
        assert lv.state == CURRENT, ahead
        assert lv.age == 0.0


def test_a_reading_far_in_the_future_is_unknown_not_current():
    for ahead, label in ((3600, "an hour"), (86_400, "a day"), (31_536_000, "a year")):
        lv = liveness(1_000_000.0 + ahead, now=1_000_000.0)
        assert lv.state == UNKNOWN, f"{label} ahead must not read current"
        assert lv.age is None, "an undateable reading has no age"
        assert "future" in lv.reason


def test_the_skew_boundary_is_exact():
    now = 1_000_000.0
    assert liveness(now + SKEW_TOLERANCE, now=now).state == CURRENT
    assert liveness(now + SKEW_TOLERANCE + 1, now=now).state == UNKNOWN


def test_a_caller_supplied_reason_still_wins_for_a_future_reading():
    lv = liveness(1_000_000.0 + 86_400, now=1_000_000.0, reason="collector down")
    assert lv.state == UNKNOWN and lv.reason == "collector down"


def test_unusable_timestamps_carry_the_clock_cause_and_ordinary_unknowns_do_not():
    assert liveness("not a time", now=1000.0).cause == "clock_unusable"
    assert liveness(1000.0 + 10 * SKEW_TOLERANCE, now=1000.0).cause == "clock_unusable"
    assert liveness(1000.0, now=1000.0).cause is None
    assert unknown("collector unreachable").cause is None
    assert unknown("x", "not_permitted").cause == "not_permitted"
