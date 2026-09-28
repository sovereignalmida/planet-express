"""The VPN port-forwarding sensor, and when it is allowed to wake someone.

Every number here came off the live host's gluetun log over 48 hours (2026-09-24 to -26):
ProtonVPN rotates the forwarded port every two hours at :17, a healthy renewal is a ~15
second gap, and 7 of 24 renewals had their NAT-PMP RPC refused — gluetun does not retry a
refusal, it waits for the next cycle, so the port is genuinely gone for up to two hours and
then returns on its own.

Sampling that at a 6h scan cadence fired three times on 2026-09-26, and the plan it proposed
restarted GSP to get a port the next scheduled renewal replaced 26 minutes later.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_leela as leela
from planet_express.core.incidents import observations_from_snapshot

HOUR = 3600
NOW = 1790000000.0


@pytest.fixture
def host(monkeypatch):
    """A host whose gluetun, qBittorrent and gluetun log answer whatever the test says."""
    state = {
        "gluetun_port": 47268,
        "qbit_port": 47268,
        "raises": None,
        # gluetun's log as (minutes ago, event). Events outside the window asked for are
        # dropped by the fake, exactly as `docker logs --since` would.
        "events": [(59, "obtained"), (31, "cleared")],
        "log_rc": 0,
        "containers": "CASA_GLUETON|qmcgaw/gluetun:v3.41.1\nCASA_QBIT|lscr.io/linuxserver/qbittorrent\n",
    }
    monkeypatch.setattr(leela, "_read_env_var", lambda *a, **k: "an-api-key")
    monkeypatch.setattr(leela.time, "time", lambda: NOW)

    def fake_get(url, **kwargs):
        if state["raises"]:
            raise state["raises"]
        response = Mock()
        response.raise_for_status = Mock()
        response.json = Mock(return_value={"port": state["gluetun_port"]})
        return response

    monkeypatch.setattr(leela.requests, "get", fake_get)

    def fake_run(argv, **kwargs):
        if argv[:3] == ["docker", "ps", "--format"]:
            return 0, state["containers"], ""
        if argv[:2] == ["docker", "logs"]:
            if state["log_rc"] != 0:
                return state["log_rc"], "", "Error: No such container"
            window = int(argv[argv.index("--since") + 1].rstrip("s"))
            text = {"obtained": "port forwarded is 47268",
                    "cleared": "clearing port file /tmp/gluetun/forwarded_port"}
            lines = []
            for ago, kind in sorted(state["events"], reverse=True):
                if ago * 60 > window:
                    continue
                stamp = datetime.fromtimestamp(NOW - ago * 60, tz=timezone.utc)
                lines.append(f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                             f"INFO [port forwarding] {text[kind]}")
            return 0, "\n".join(lines + ["some unrelated line"]) + "\n", ""
        return 0, f"[BitTorrent]\nSession\\Port={state['qbit_port']}\n", ""

    monkeypatch.setattr(leela, "_run", fake_run)
    return state


def check(_host_state):
    return leela.check_vpn_port_forwarding()


# ── the healthy host ────────────────────────────────────────────────────────────

def test_matching_ports_are_healthy(host):
    assert check(host) == {"reachable": True, "gluetun_port": 47268, "qbit_port": 47268}


# ── a refused renewal, which fixes itself ───────────────────────────────────────

def test_a_missing_port_is_reported_but_does_not_alert_while_it_is_still_rotating(host):
    """gluetun cleared the port 31 minutes ago, so this is the ordinary two-hourly cycle with
    a refused renewal -- it comes back by itself at the next one."""
    host["gluetun_port"] = 0
    result = check(host)
    assert "alert" not in result
    assert result["within_renewal_grace"] is True
    assert result["grace_evidence"] == "port lost"
    assert result["grace_evidence_seconds"] == 31 * 60
    assert "clears itself" in result["issue"]


@pytest.mark.parametrize("minutes_ago", [1, 30, 119, 149])
def test_a_clear_inside_the_grace_means_it_is_rotating_not_stuck(host, minutes_ago):
    host["gluetun_port"] = 0
    host["events"] = [(minutes_ago + 1, "obtained"), (minutes_ago, "cleared")]
    assert "alert" not in check(host)


def test_chained_failed_renewals_do_not_keep_restarting_the_clock(host):
    """gluetun logs a clear on EVERY failed renewal. Measured from the latest one, a forward
    that stayed dead all day would look two hours old for ever and never alert -- the exact
    silence this check exists to break. The outage starts at the first clear after the last
    port gluetun actually got."""
    host["gluetun_port"] = 0
    host["events"] = [
        (175, "obtained"),      # the last port it really had
        (170, "cleared"),       # renewal refused -- the outage starts here
        (50, "cleared"),        # the next cycle refused too
        (10, "cleared"),        # and the one after that
    ]
    result = check(host)
    assert result["alert"] == "HIGH"
    assert "stuck" in result["issue"]


def test_a_clear_before_the_last_good_port_is_not_this_outage(host):
    """An ordinary rotation is a clear followed 13 seconds later by a new port. That clear
    belongs to a recovered cycle and must not be mistaken for the current outage's start."""
    host["gluetun_port"] = 0
    host["events"] = [
        (140, "cleared"), (139, "obtained"),   # a healthy rotation, long before
        (20, "cleared"),                       # this is the one that is still unresolved
    ]
    result = check(host)
    assert "alert" not in result
    assert result["grace_evidence_seconds"] == 20 * 60


def test_the_clock_starts_at_the_clear_not_at_the_last_good_port(host):
    """A port lives for the full two-hour lease before its renewal fails. Measured from the
    last successful allocation, a 2.5h window would expire about half an hour into a gap that
    self-heals ninety minutes later -- the exact false alert this change removes."""
    host["gluetun_port"] = 0
    # Obtained 2.6h ago (outside the window), but only gone for 36 minutes.
    host["events"] = [(156, "obtained"), (36, "cleared")]
    result = check(host)
    assert "alert" not in result
    assert result["grace_evidence_seconds"] == 36 * 60


def test_the_three_real_samples_of_2026_09_26_would_all_have_stayed_quiet(host):
    """The scheduler sampled at 06:16, 12:16 and 18:17 UTC and found the port cleared every
    time. gluetun had obtained one 59, 59 and 60 minutes before those moments respectively --
    it was rotating, not stuck. A rule of "unhealthy last scan and unhealthy now" would have
    claimed six continuous hours without a port, which never happened."""
    host["gluetun_port"] = 0
    # 05:57 -> 06:16, 11:25 -> 12:16, 17:45 -> 18:17: minutes since the port was cleared,
    # each preceded by the healthy port gluetun held for the hour before it.
    for minutes in (19, 51, 31):
        host["events"] = [(minutes + 59, "obtained"), (minutes, "cleared")]
        assert "alert" not in check(host)


def test_no_port_obtained_within_a_renewal_cycle_is_the_real_thing(host):
    """The 2026-08-22 failure this check was written for: ~12h undetected."""
    host["gluetun_port"] = 0
    host["events"] = []
    result = check(host)
    assert result["alert"] == "HIGH"
    assert "stuck" in result["issue"] and "firewalled" in result["issue"]
    assert "within_renewal_grace" not in result


def test_the_log_is_read_far_further_back_than_the_grace(host):
    """The two are separate on purpose. The evidence for when an outage started has to be
    inside the window, and a chain of failed renewals pushes that moment further back than
    the grace it is compared against -- reading only the grace's worth of log made a
    multi-hour outage look an hour old."""
    seen = {}
    original = leela._run

    def capture(argv, **kwargs):
        if argv[:2] == ["docker", "logs"]:
            seen["since"] = argv[argv.index("--since") + 1]
        return original(argv, **kwargs)

    host["gluetun_port"] = 0
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(leela, "_run", capture)
        check(host)
    assert seen["since"] == f"{int(leela.GLUETUN_LOG_LOOKBACK_SECONDS)}s"
    assert leela.GLUETUN_LOG_LOOKBACK_SECONDS > 4 * leela.VPN_RENEWAL_GRACE_SECONDS


# ── a stuck GSP ─────────────────────────────────────────────────────────────────

def test_a_mismatch_right_after_a_rotation_is_gsps_sync_minute(host):
    host["gluetun_port"], host["qbit_port"] = 47268, 53614
    host["events"] = [(1, "obtained")]
    result = check(host)
    assert "alert" not in result and result["within_renewal_grace"] is True
    assert result["qbit_port"] == 53614


def test_a_mismatch_long_after_the_rotation_is_gsp_stuck(host):
    """GSP syncs within about a minute, so the mismatch window is minutes, not hours -- a
    rotation 40 minutes ago is not an excuse for qBittorrent still being on the old port."""
    host["gluetun_port"], host["qbit_port"] = 47268, 53614
    host["events"] = [(40, "obtained")]
    result = check(host)
    assert result["alert"] == "HIGH"
    assert "GSP sync is stuck" in result["issue"]


# ── the checks that cannot see ──────────────────────────────────────────────────

def test_a_fault_whose_log_cannot_be_read_is_treated_as_real(host):
    """An unverifiable fault reporting nothing is the silence this check exists to break."""
    host["gluetun_port"] = 0
    host["log_rc"] = 1
    result = check(host)
    assert result["alert"] == "HIGH"
    assert "could not check gluetun's log" in result["issue"]


def test_gluetun_is_found_by_image_not_by_name(host):
    """This host's container is CASA_GLUETON -- a typo that has outlived several rebuilds."""
    host["gluetun_port"] = 0
    host["containers"] = "SOMETHING_ELSE_ENTIRELY|qmcgaw/gluetun:v3.41.1\n"
    assert "alert" not in check(host)

    host["containers"] = "CASA_QBIT|lscr.io/linuxserver/qbittorrent\n"
    result = check(host)
    assert result["alert"] == "HIGH"
    assert "no running container for image qmcgaw/gluetun" in result["issue"]


def test_an_unreachable_control_server_still_alerts(host):
    host["raises"] = leela.requests.RequestException("connection refused")
    assert check(host)["alert"] == "HIGH"


def test_a_missing_api_key_still_alerts(host, monkeypatch):
    monkeypatch.setattr(leela, "_read_env_var", lambda *a, **k: None)
    assert check(host)["alert"] == "HIGH"


# ── what the incident layer makes of it ─────────────────────────────────────────

def _observation(vpn):
    return next(o for o in observations_from_snapshot({"vpn_port_forwarding": vpn})
                if o.kind == "vpn_port_forwarding")


def test_a_fault_inside_the_grace_is_never_reported_as_healthy():
    """The failure mode this project keeps finding: a tile healthier than the host. There is
    no forwarded port; "not actionable yet" is not "fine"."""
    observation = _observation({
        "reachable": True, "gluetun_port": 0, "within_renewal_grace": True,
        "grace_evidence": "port cleared", "grace_evidence_seconds": 600,
        "issue": "gluetun reports no forwarded port",
    })
    assert observation.condition == "unknown"
    assert observation.severity == "MEDIUM"
    assert observation.summary == "gluetun reports no forwarded port"
    assert observation.details["grace_evidence"] == "port cleared"
    assert observation.details["grace_evidence_seconds"] == 600


def test_a_confirmed_fault_is_failing_and_high():
    observation = _observation({
        "reachable": True, "gluetun_port": 0, "alert": "HIGH",
        "issue": "port forwarding is stuck",
    })
    assert observation.condition == "failing" and observation.severity == "HIGH"


def test_a_genuinely_healthy_check_is_healthy():
    observation = _observation({"reachable": True, "gluetun_port": 47268, "qbit_port": 47268})
    assert observation.condition == "healthy" and observation.severity is None


def test_hermes_is_only_told_about_a_confirmed_fault():
    """Hermes forwards this block only when `alert` is set, and the planner proposes from
    Hermes's findings -- so the grace is also what stops the no-op plan being proposed."""
    import casa_hermes as hermes

    within = hermes._slim_snapshot({"vpn_port_forwarding": {
        "reachable": True, "gluetun_port": 0, "within_renewal_grace": True}})
    assert within["vpn_port_forwarding"] == {}

    confirmed = hermes._slim_snapshot({"vpn_port_forwarding": {
        "reachable": True, "gluetun_port": 0, "alert": "HIGH"}})
    assert confirmed["vpn_port_forwarding"]["alert"] == "HIGH"


def test_no_port_obtained_in_the_whole_lookback_is_decisive(host):
    """Twelve hours without gluetun getting a port, and none now, is a dead forward whatever
    the clears say — including when there are no clears at all to date it from."""
    host["gluetun_port"] = 0
    host["events"] = [(30, "cleared"), (200, "cleared")]     # clears, but never a port
    assert check(host)["alert"] == "HIGH"


def test_a_port_that_vanished_with_no_clear_logged_is_treated_as_real(host):
    """gluetun holds a port, then it is gone with nothing in the log to say when. There is no
    evidence it is mid-rotation, and a check that stays quiet on no evidence is the silence
    this one exists to break."""
    host["gluetun_port"] = 0
    host["events"] = [(90, "obtained")]
    assert check(host)["alert"] == "HIGH"


def test_an_outage_exactly_at_the_grace_alerts(host):
    host["gluetun_port"] = 0
    grace_min = leela.VPN_RENEWAL_GRACE_SECONDS / 60
    host["events"] = [(grace_min + 5, "obtained"), (grace_min, "cleared")]
    assert check(host)["alert"] == "HIGH"
    host["events"] = [(grace_min + 5, "obtained"), (grace_min - 5, "cleared")]
    assert "alert" not in check(host)


def test_a_port_that_arrives_mid_check_is_not_an_alert(host):
    """The port is read, then the log. Between the two the ordinary two-hourly renewal can
    land: the newest obtain then sits after every clear, the outage looks undateable, and a
    healthy recovery would alert. The port is re-read before alerting for exactly that."""
    host["gluetun_port"] = 0
    host["events"] = [(90, "obtained")]          # undateable: no clear after the last port

    calls = {"n": 0}
    original = leela.requests.get

    def recovering(url, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:                        # the re-read sees the new port
            host["gluetun_port"] = 51234
        return original(url, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(leela.requests, "get", recovering)
        result = leela.check_vpn_port_forwarding()

    assert "alert" not in result
    assert result["grace_evidence"] == "recovered during the check"
    assert "cleared while this check was running" in result["issue"]


def test_a_fault_that_is_still_there_on_the_re_read_still_alerts(host):
    host["gluetun_port"] = 0
    host["events"] = [(90, "obtained")]
    assert check(host)["alert"] == "HIGH"


def test_a_control_server_that_dies_mid_check_still_alerts(host):
    """The re-read is a chance to withdraw an alert, never a way to lose one."""
    host["gluetun_port"] = 0
    host["events"] = [(90, "obtained")]
    calls = {"n": 0}

    def then_unreachable(url, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise leela.requests.RequestException("gone")
        response = Mock()
        response.raise_for_status = Mock()
        response.json = Mock(return_value={"port": 0})
        return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(leela.requests, "get", then_unreachable)
        assert leela.check_vpn_port_forwarding()["alert"] == "HIGH"
