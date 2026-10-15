"""Config, factories and runner wiring for slice 6: missions disabled means nothing is built and no thread starts."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import make_store
from tripwire_fakes import Host, doc, go_executing, make_rig, status, touch

from config_schema import MissionsConfig
from planet_express.missions.containment import FakeContainment
from planet_express.missions.runner import MissionRunner
from planet_express.missions.store import MissionStore
from planet_express.missions.tools import broker_from_config
from planet_express.missions.tripwire.build import build_protected_set, build_tripwire


def cfg(**kw):
    return MissionsConfig(enabled=False, **kw)


def test_config_defaults_and_bounds():
    c = MissionsConfig()
    assert c.tripwire_interval_s == 15 and c.tripwire_investigating_interval_s == 60
    assert c.protected == [] and c.protected_overrides == [] and c.tool_sites == {}
    for bad in (dict(tripwire_interval_s=0), dict(tripwire_interval_s=16),
                dict(tripwire_investigating_interval_s=5),
                dict(tool_sites={"x": {"a": "ftp://h"}}), dict(tool_sites={"x": {"a": "http://u:p@h/"}}),
                dict(tool_secret_env={"Arr": "ARR"}), dict(tool_secret_env={"arr": "PE_TOKEN"}),
                dict(protected=[""]), dict(live_trees=["\0"]),
                dict(cross_host=[{"name": "nas", "path": "relative"}]),
                dict(protected_overrides=[{"resource": "path:/etc/hosts", "reason": "r", "operator": "chris", "at": 1}])):
        with pytest.raises(ValueError):
            MissionsConfig(**bad)


def test_the_protected_set_from_config_keeps_the_defaults_and_applies_overrides(tmp_path):
    c = MissionsConfig(protected=["service:media/traefik"], live_trees=["/home/x/repo"],
                       cross_host=[{"name": "nas", "path": "/etc/exports"}],
                       protected_overrides=[{"resource": "path:/etc/hosts", "reason": "managed by dhcp",
                                             "operator": "operator:chris", "at": 3}])
    seen = []
    ps = build_protected_set(c, stacks_root="/srv/stacks", record=seen.append)
    assert "host:local/service:media/traefik" in ps.resources
    assert "host:local/path:/etc/hosts" not in ps.resources and "host:local/path:/etc/shadow" in ps.resources
    assert "host:local/system:nas" in ps.resources and ps.live_trees == ("/home/x/repo",)
    assert seen[0]["operator"] == "operator:chris"


def test_disabled_missions_build_nothing_and_start_no_thread(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    ps = build_protected_set(MissionsConfig(), stacks_root="/s")
    before = threading.active_count()
    assert build_tripwire(MissionsConfig(enabled=False), missions=ms, containment=FakeContainment(), checkpoint=None,
                          provider=Host(), stacks_root="/s", state_dir=str(tmp_path / "t"), protected=ps) is None
    assert broker_from_config(MissionsConfig(enabled=False), missions=ms, stacks_root="/s") is None
    assert threading.active_count() == before


def test_enabled_missions_build_a_monitor_with_the_configured_interval(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    c = SimpleNamespace(enabled=True, tripwire_interval_s=7, tripwire_investigating_interval_s=70, package_paths=[],
                        live_trees=[], cross_host=[], protected=[], protected_overrides=[], tripwire_accept_blind=[])
    ps = build_protected_set(c, stacks_root="/s")
    mon = build_tripwire(c, missions=ms, containment=FakeContainment(), checkpoint=None, provider=Host(),
                         stacks_root="/s", state_dir=str(tmp_path / "t"), protected=ps)
    assert mon.interval_s == 7 and mon.investigating_interval_s == 70 and mon.enabled
    assert mon.start() is not None
    mon.stop()


def test_broker_from_config_resolves_the_secret_from_core_environment_only(tmp_path):
    store, _ = make_store(tmp_path)
    c = SimpleNamespace(enabled=True, tool_sites={"arr.queue_item": {"sonarr": "http://s.lan:8989"}},
                        tool_secret_env={"arr-api": "ARR_API_KEY"})
    broker = broker_from_config(c, missions=MissionStore(store), stacks_root="/s",
                                environ={"ARR_API_KEY": "k-123456789012345", "OTHER": "x"})
    assert broker.secrets("arr-api") == "k-123456789012345"
    assert broker.secrets("dns-admin") is None and broker.secrets("../etc") is None
    assert broker.sites["arr.queue_item"].urls == {"sonarr": "http://s.lan:8989"}


class Sup:
    def __init__(self):
        self.calls = []

    def recover(self):
        self.calls.append("recover")
        return []

    def tick(self):
        self.calls.append("tick")

    def shutdown(self):
        self.calls.append("shutdown")


class Trip:
    def __init__(self, fail_recover=False):
        self.calls = []
        self.fail_recover = fail_recover

    def recover(self):
        self.calls.append("recover")
        if self.fail_recover:
            raise RuntimeError("cannot re-freeze")
        return ["M-1"]

    def start(self):
        self.calls.append("start")

    def ensure_running(self):
        self.calls.append("ensure")

    def stop(self):
        self.calls.append("stop")


def test_the_runner_recovers_then_starts_the_monitor_checks_it_and_stops_it_first():
    sup, trip = Sup(), Trip()
    runner = MissionRunner(sup, interval_s=0.01, tripwire=trip)
    runner.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.1)
    finally:
        runner.stop()
    assert trip.calls[:2] == ["recover", "start"] and "ensure" in trip.calls and trip.calls[-1] == "stop"
    assert sup.calls[0] == "recover"


def test_a_monitor_that_cannot_recover_stops_the_runner_from_starting():
    runner = MissionRunner(Sup(), interval_s=0.01, tripwire=Trip(fail_recover=True))
    with pytest.raises(RuntimeError):
        runner.start()
    assert runner._thread is None


def test_the_runner_without_a_monitor_is_unchanged():
    sup = Sup()
    runner = MissionRunner(sup, interval_s=0.01)
    runner.start()
    runner.stop()
    assert sup.calls[0] == "recover" and sup.calls[-1] == "shutdown"


def test_the_monitor_and_the_broker_close_together_on_a_pause(tmp_path):
    from planet_express.missions.tools import ToolBroker, ToolSite, default_registry
    rig = make_rig(tmp_path)
    go_executing(rig)
    broker = ToolBroker(default_registry(), missions=rig.env.ms, secrets=lambda r: "k", sites={"x": ToolSite()},
                        stacks_root=str(rig.env.stacks))
    rig.monitor.revoke_endpoints = broker.revoke
    touch(rig.fs / "etc" / "hosts", "x\n")
    rig.monitor.scan_once()
    assert status(rig) == "paused_tripwire" and rig.mission.id in broker._revoked
    assert doc("x")["Name"] == "/x"
