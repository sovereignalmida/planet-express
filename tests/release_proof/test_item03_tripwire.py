"""Release proof item 3:

    A planted protected-domain change (ingress, DNS file, credential file, firewall, data-volume shrink) during a mission
    pauses the session within the published worst-case bound (30 s: scan interval, scan deadline, freeze, durable write)
    and offers rollback.

Time is a fake clock: the scan itself is made to consume its whole 10 s deadline and the change lands just after a scan,
so the measured latency is interval + deadline, the worst case the bound is published for."""
import json

import pytest
from tripwire_fakes import frozen, go_executing, make_rig, paused_event, status, touch

from planet_express.missions.tripwire import PAUSE_SLA_S
from release_proof.support import ROOT  # noqa: F401  (sys.path)


def plant_ingress(r):
    touch(r.fs / "etc" / "traefik" / "dynamic.yml", "http: {routers: {x: {}}}\n")


def plant_dns(r):
    touch(r.fs / "etc" / "hosts", "10.0.0.1 evil.example\n")


def plant_credentials(r):
    touch(r.fs / "etc" / "shadow", "root:changed:1\n")


def plant_firewall(r):
    r.host.firewall = "table inet filter { chain input { policy accept; } }\n"


def plant_data_shrink(r):
    for f in list(r.data.glob("*.bin"))[:12]:
        f.write_bytes(b"x")


DOMAINS = [
    ("ingress", plant_ingress, "file_changed"),
    ("dns", plant_dns, "file_changed"),
    ("credentials", plant_credentials, "file_changed"),
    ("firewall", plant_firewall, "firewall_changed"),
    ("data_volume_shrink", plant_data_shrink, "data_volume_shrunk"),
]


def worst_case_rig(tmp_path):
    rig = make_rig(tmp_path)
    go_executing(rig)
    real_scan = rig.scanner.scan

    def slow_scan():
        result = real_scan()
        rig.env.clock.t += 10.0                      # every scan takes its whole 10 s deadline
        return result
    rig.scanner.scan = slow_scan
    return rig


@pytest.mark.parametrize("name,plant,kind", DOMAINS, ids=[d[0] for d in DOMAINS])
def test_a_planted_change_pauses_within_the_published_bound_and_offers_rollback(tmp_path, name, plant, kind):
    rig = worst_case_rig(tmp_path)
    clock = rig.env.clock
    assert rig.monitor.scan_once()[rig.mission.id] == "ok"        # a scan has just finished
    changed_at = clock.t + 0.001                                    # the change lands right after it
    plant(rig)
    clock.t = changed_at + rig.monitor.interval_s                   # the next scan starts a full interval later
    rig.monitor.scan_once()

    assert status(rig) == "paused_tripwire" and frozen(rig) is True
    event = [e for e in rig.env.ms.events(rig.mission.id) if e.kind == "tripwire"][-1]
    body = json.loads(event.body_json)
    assert body["event"] == "tripwire.paused" and body["cause"] == "protected"
    assert any(c["kind"] == kind for c in body["changes"]), body["changes"]
    latency = event.at - changed_at
    assert latency <= PAUSE_SLA_S == 30, latency
    assert latency == pytest.approx(rig.monitor.interval_s + 10.0, abs=1.0)
    # rollback is offered, and only because the end state was durably captured first
    assert body["rollback"]["available"] is True and "roll_back" in body["card"]["options"]
    assert rig.env.svc.store.read_post(body["rollback"]["checkpoint"]) is not None


def test_the_pause_is_durable_and_the_card_survives_a_reread(tmp_path):
    rig = worst_case_rig(tmp_path)
    plant_dns(rig)
    rig.monitor.scan_once()
    assert paused_event(rig)["card"]["options"]
    assert rig.env.ms.get_mission(rig.mission.id).status == "paused_tripwire"


def test_rolling_back_after_a_planted_change_restores_the_checkpointed_files(tmp_path):
    rig = make_rig(tmp_path)
    go_executing(rig)
    original = rig.env.compose.read_text()
    rig.env.compose.write_text(original + "# the agent edited this\n")
    plant_dns(rig)
    rig.monitor.scan_once()
    assert status(rig) == "paused_tripwire"
    rig.monitor.roll_back(rig.mission.id, "operator:alice")
    assert rig.env.compose.read_text() == original and status(rig) == "rolled_back"


def test_a_quiet_host_never_pauses(tmp_path):
    rig = make_rig(tmp_path)
    go_executing(rig)
    for _ in range(3):
        rig.env.clock.t += 15
        rig.monitor.scan_once()
    assert status(rig) == "executing"
