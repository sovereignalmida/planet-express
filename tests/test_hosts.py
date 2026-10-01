"""Host types, staleness and the locality predicate.

The locality cases run off `tests/fixtures/beszel/*.json`, captured from the live hub on
2026-10-01, so the measured case in the table below is the real one rather than a tidied
version of it. The fixture is what caught that CASA UNRAID overlaps by exactly one name --
`beszel-agent`, which exists on every host -- and therefore that any-overlap matching is
wrong. A hand-written fixture would have had an overlap of zero and proved nothing.
"""
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.hosts import (
    CURRENT,
    MIN_LOCAL_NAMES,
    STALE,
    STALE_AFTER,
    UNKNOWN,
    Host,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    coverage,
    liveness,
    locality,
    parse_timestamp,
    unknown,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "beszel"

LOCAL_ID = "n7n7ppta55karj9"
UNRAID_ID = "ptf3tn2gzpg913i"
MACMINI_ID = "vw53pk01zei80wt"
SOLAR_ID = "7y4fosy0ebhtk9x"


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["items"]


@pytest.fixture(scope="module")
def by_system():
    """system id -> the container names the hub reports for it, straight from the capture."""
    rows = _load("containers")
    grouped = {sid: set() for sid in (LOCAL_ID, UNRAID_ID, MACMINI_ID, SOLAR_ID)}
    for row in rows:
        grouped.setdefault(row["system"], set()).add(row["name"])
    return grouped


@pytest.fixture(scope="module")
def local_names(by_system):
    """Stand-in for the docker socket: this host's own names, which the hub also reports."""
    return set(by_system[LOCAL_ID])


# --- the measured case -------------------------------------------------------------------

def test_fixture_matches_the_measured_separation(by_system, local_names):
    # Guards the premise of every case below. If a recapture changes these counts, the
    # thresholds were tuned against data that no longer exists.
    assert len(local_names) == 85
    assert len(local_names & by_system[UNRAID_ID]) == 1
    assert local_names & by_system[UNRAID_ID] == {"beszel-agent"}
    assert len(local_names & by_system[MACMINI_ID]) == 0
    assert by_system[SOLAR_ID] == set()


def test_measured_case_resolves_to_the_local_system(by_system, local_names):
    assert locality(local_names, by_system) == LOCAL_ID


def test_measured_case_coverage_is_exact(by_system, local_names):
    assert coverage(local_names, by_system[LOCAL_ID]) == 1.0
    assert coverage(local_names, by_system[UNRAID_ID]) == pytest.approx(1 / 85)


# --- the cases that must refuse to answer ------------------------------------------------

def test_only_overlap_is_beszel_agent(local_names):
    # beszel-agent runs on every host in the fleet, so one shared name is not evidence. This
    # is the case that rules out any-overlap matching.
    assert locality(local_names, {UNRAID_ID: {"beszel-agent"}}) is None


def test_tie_is_unknown(local_names):
    twin = set(local_names)
    assert locality(local_names, {LOCAL_ID: twin, "clone": set(twin)}) is None


def test_inside_the_three_times_margin_is_unknown(local_names):
    names = sorted(local_names)
    # 60 of 85 clears coverage, but 25 is more than a third of 60, so the two are not
    # separable and neither may be claimed as this host.
    assert locality(local_names, {LOCAL_ID: set(names[:60]), UNRAID_ID: set(names[60:85])}) is None


def test_exactly_three_times_the_runner_up_resolves(local_names):
    names = sorted(local_names)
    assert locality(local_names, {LOCAL_ID: set(names[:60]), UNRAID_ID: set(names[60:80])}) == LOCAL_ID


def test_fewer_than_five_local_names_is_not_attempted():
    few = {"a", "b", "c", "d"}
    assert len(few) < MIN_LOCAL_NAMES
    assert locality(few, {LOCAL_ID: set(few)}) is None


def test_five_local_names_is_attempted():
    five = {"a", "b", "c", "d", "e"}
    assert locality(five, {LOCAL_ID: set(five)}) == LOCAL_ID


def test_empty_remote_set_cannot_win(local_names):
    # Solar Assistant is a real host that runs no containers at all.
    assert locality(local_names, {SOLAR_ID: set()}) is None


def test_no_systems_at_all_is_unknown(local_names):
    assert locality(local_names, {}) is None


def test_empty_local_set_is_unknown():
    assert locality(set(), {LOCAL_ID: {"a", "b", "c", "d", "e"}}) is None


def test_coverage_denominator_is_always_the_local_set(local_names):
    # A host reporting thousands of containers, 40 of which are ours, scores 40/85 and loses;
    # dividing by the remote set would have scored it near zero, and dividing by the
    # intersection would have scored it 1.0. Only the local denominator gives 40/85.
    flood = set(sorted(local_names)[:40]) | {f"stranger-{n}" for n in range(5000)}
    assert coverage(local_names, flood) == pytest.approx(40 / 85)
    assert locality(local_names, {UNRAID_ID: flood}) is None


def test_volume_cannot_beat_a_real_match(local_names):
    flood = set(local_names) | {f"stranger-{n}" for n in range(5000)}
    assert locality(local_names, {UNRAID_ID: flood, LOCAL_ID: set(local_names)}) is None


def test_coverage_of_an_empty_local_set_is_unknown_not_zero():
    assert coverage(set(), {"a"}) is None


def test_duplicate_names_count_once(local_names):
    # Unraid genuinely reports two containers called beszel-agent.
    assert coverage(local_names, ["beszel-agent", "beszel-agent"]) == pytest.approx(1 / 85)


def test_unusable_names_are_dropped(local_names):
    assert locality(local_names, {LOCAL_ID: list(local_names) + ["", None, 7]}) == LOCAL_ID


def test_unusable_system_ids_are_dropped(local_names):
    assert locality(local_names, {"": set(local_names), None: set(local_names)}) is None


def test_a_string_is_not_a_name_set(local_names):
    # A bare string is iterable, and iterating it would compare single characters.
    assert coverage(local_names, "beszel-agent") == 0.0


# --- staleness ---------------------------------------------------------------------------

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


def test_systems_fixture_timestamps_all_parse():
    for row in _load("systems"):
        assert parse_timestamp(row["updated"]) is not None


# --- absent is not zero ------------------------------------------------------------------

def test_host_defaults_are_none():
    host = Host(id=LOCAL_ID)
    assert (host.name, host.link, host.status, host.updated, host.details) == (None,) * 5


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
    host = Host(id=LOCAL_ID)
    with pytest.raises(FrozenInstanceError):
        host.id = "other"


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
