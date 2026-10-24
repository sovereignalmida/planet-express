"""planet_express/core/boot_settle.py: no systemd, no Docker; every probe is injected."""

from planet_express.core import boot_settle as bs


def reason(uptime, state=None, starting=()):
    return bs.settling_reason(uptime=lambda: uptime, unit_state=lambda: state or {}, starting=lambda: list(starting))


def active(entered_s):
    return {"ActiveState": "active", "ActiveEnterTimestampMonotonic": str(int(entered_s * 1_000_000))}


def test_a_host_up_for_a_long_time_never_waits():
    assert reason(5 * 3600, {"ActiveState": "activating"}, ["x"]) is None


def test_the_first_two_minutes_after_boot_always_wait():
    assert "rebooted" in reason(30)


def test_stacks_still_starting_wait():
    assert "still starting" in reason(200, {"ActiveState": "activating"})


def test_the_stacks_just_coming_up_wait_for_the_settle_delay_then_check_containers():
    assert "only just come up" in reason(400, active(300))
    assert "still starting" in reason(600, active(300), ["plex", "sonarr"])
    assert reason(600, active(300)) is None


def test_a_stacks_unit_that_never_ran_is_waited_for_a_bounded_time():
    assert "not been started" in reason(200, {"ActiveState": "inactive", "ActiveEnterTimestampMonotonic": "0"})
    assert reason(400, {"ActiveState": "inactive", "ActiveEnterTimestampMonotonic": "0"}) is None


def test_a_failed_bring_up_is_reported_not_waited_out():
    assert reason(200, {"ActiveState": "failed"}, ["plex"]) is None


def test_an_unreadable_unit_state_fails_open_after_the_quiet_period():
    assert reason(400, {}) is None


def test_wait_polls_until_the_reason_clears_and_says_whether_it_waited():
    answers = iter(["booting", "booting", None])
    slept = []
    assert bs.wait_until_settled(reason=lambda: next(answers), sleep=slept.append, poll=7) is True
    assert slept == [7, 7]
    assert bs.wait_until_settled(reason=lambda: None, sleep=slept.append) is False


def test_the_conftest_isolation_really_stops_a_just_booted_host_from_waiting(monkeypatch):
    # tests/conftest.py replaces the module's uptime probe; the default lookups must see that replacement
    monkeypatch.setattr(bs, "uptime_seconds", lambda: 10.0)
    assert "rebooted" in bs.settling_reason()
    monkeypatch.setattr(bs, "uptime_seconds", lambda: float("inf"))
    assert bs.settling_reason() is None and bs.wait_until_settled() is False
