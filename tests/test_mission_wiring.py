"""Config, factories and runner wiring for slice 6: missions disabled means nothing is built and no thread starts."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import make_store

from config_schema import MissionsConfig
from planet_express.missions.containment import FakeContainment
from planet_express.missions.protected import build_protected_set
from planet_express.missions.runner import MissionRunner
from planet_express.missions.store import MissionStore
from planet_express.missions.tools import broker_from_config


def cfg(**kw):
    return MissionsConfig(enabled=False, **kw)


def test_config_defaults_and_bounds():
    c = MissionsConfig()
    assert c.protected == [] and c.protected_overrides == [] and c.tool_sites == {}
    for bad in (dict(tool_sites={"x": {"a": "ftp://h"}}), dict(tool_sites={"x": {"a": "http://u:p@h/"}}),
                dict(tool_secret_env={"Arr": "ARR"}), dict(tool_secret_env={"arr": "PE_TOKEN"}),
                dict(protected=[""]), dict(live_trees=["\0"]),
                dict(cross_host=[{"name": "nas", "path": "relative"}]),
                dict(protected_overrides=[{"resource": "path:/etc/hosts", "reason": "r", "operator": "chris", "at": 1}])):
        with pytest.raises(ValueError):
            MissionsConfig(**bad)


def test_an_old_config_with_the_retired_tripwire_settings_still_loads():
    """Trust mode removed the tripwire. The live host's config.yaml still has a `tripwire_accept_blind` block (and may name the
    intervals): it must load, the keys accepted and ignored, never refused, whatever they hold."""
    import yaml
    old = yaml.safe_load("""
enabled: true
tripwire_interval_s: 15
tripwire_investigating_interval_s: 60
tripwire_accept_blind:
  - host:local/path:/etc/shadow
  - host:local/path:/etc/gshadow
protected: ["service:media/traefik"]
""")
    c = MissionsConfig(**old)
    assert c.enabled and c.protected == ["service:media/traefik"]
    assert not any(hasattr(c, k) for k in ("tripwire_interval_s", "tripwire_investigating_interval_s", "tripwire_accept_blind"))
    assert MissionsConfig(tripwire_interval_s=999, tripwire_accept_blind="not even a list").enabled is False   # ignored, not judged
    with pytest.raises(ValueError):
        MissionsConfig(tripwire_typo=1)                                        # only the retired names are let through


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
    assert ps.resources
    assert broker_from_config(MissionsConfig(enabled=False), missions=ms, stacks_root="/s") is None
    assert threading.active_count() == before


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


def test_the_runner_recovers_then_ticks_then_shuts_down():
    sup = Sup()
    runner = MissionRunner(sup, interval_s=0.01)
    runner.start()
    runner.stop()
    assert sup.calls[0] == "recover" and sup.calls[-1] == "shutdown"


@pytest.fixture
def cgroup2_mount(tmp_path, monkeypatch):
    import config_schema
    path = tmp_path / "mountinfo"
    path.write_text("25 1 0:22 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n")
    monkeypatch.setattr(config_schema, "MOUNTINFO_PATH", str(path))


def test_command_runtime_applies_the_env_policy_before_spawning(tmp_path):
    from planet_express.missions.adapters.command import CommandRuntime
    from planet_express.missions.containment import ContainedSession
    from planet_express.missions.grants import make_env_policy
    from planet_express.missions.models import Grant
    from planet_express.missions.runtime import RunContext
    from planet_express.missions.tools import default_registry

    prot = build_protected_set(MissionsConfig(), stacks_root=str(tmp_path))
    policy = make_env_policy(protected=prot, stacks_root=str(tmp_path), known_tools=default_registry().names(),
                             secret_values=lambda: ["super-secret-value-123456"])
    seen = []
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = RunContext(brief="b", session=ContainedSession("S-1", "fake", None, 60001), token_fd=7, env={}, cwd=str(ws),
                     spawn=lambda argv, env, cwd, fds: seen.append(dict(env)) or 4242)
    rt = CommandRuntime(FakeContainment(), argv=["/usr/bin/agent"], worker_socket="/run/w.sock",
                        env_allow=["INNOCENT", "LANG"],
                        environ={"INNOCENT": "xx-super-secret-value-123456", "LANG": "C"}, env_policy=policy)
    mission = SimpleNamespace(id="M-1")
    grant = Grant.model_validate({"tier": "propose", "scope": {"stacks": ["media"]}, "credentials": ["tool:arr.queue_item"]})
    rt.start(mission, ctx, grant)
    env = seen[0]
    assert "INNOCENT" not in env and env["LANG"] == "C"                 # secret by value: dropped whatever its name
    assert env["PE_TOOLS"] == "arr.queue_item" and env["PE_SESSION_TOKEN_FD"] == "7"
    assert env["PE_WORKER_SOCKET"] == "/run/w.sock" and env["PE_MISSION_ID"] == "M-1"
    assert "super-secret-value-123456" not in "".join(env.values())


def test_a_failing_env_policy_stops_the_start(tmp_path):
    from planet_express.missions.adapters.command import CommandRuntime
    from planet_express.missions.containment import ContainedSession
    from planet_express.missions.runtime import RunContext
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = RunContext(brief="b", session=ContainedSession("S-1", "fake", None, 60001), token_fd=7, env={}, cwd=str(ws),
                     spawn=lambda *a: pytest.fail("a session was spawned"))

    def policy(*a):
        raise RuntimeError("cannot build the environment")
    rt = CommandRuntime(FakeContainment(), argv=["/usr/bin/agent"], worker_socket="/run/w.sock", env_policy=policy)
    with pytest.raises(RuntimeError):
        rt.start(SimpleNamespace(id="M-1"), ctx, None)


def test_build_mission_runner_wires_the_broker_and_env_policy_and_passes_the_rest_through(tmp_path, cgroup2_mount):
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    c = MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]},
                       tool_sites={"arr.queue_item": {"sonarr": "http://s.lan:8989"}},
                       tool_secret_env={"arr-api": "ARR_API_KEY"})
    gate = lambda: None
    runner = build_mission_runner(store, c, containment=FakeContainment(), stacks_root=str(tmp_path), repair_gate=gate)
    sup = runner.supervisor
    assert sup.tool_broker is not None and sup.tool_broker.sites["arr.queue_item"].urls == {"sonarr": "http://s.lan:8989"}
    assert sup.repair_gate is gate and not hasattr(runner, "tripwire")
    assert sup.runtime_factory(None).env_policy is not None
    bare = build_mission_runner(store, c, containment=FakeContainment())
    assert bare.supervisor.tool_broker is None and bare.supervisor.runtime_factory(None).env_policy is None


OVERRIDE = {"resource": "path:/etc/hosts", "reason": "managed by dhcp, password=hunter2", "operator": "operator:chris", "at": 3}


def test_overrides_without_a_record_sink_are_refused():
    with pytest.raises(ValueError, match="record"):
        build_protected_set(MissionsConfig(protected_overrides=[OVERRIDE]), stacks_root="/s")


def test_an_override_through_the_runner_leaves_a_durable_redacted_record(tmp_path, cgroup2_mount):
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    c = MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]}, protected_overrides=[OVERRIDE])
    build_mission_runner(store, c, containment=FakeContainment(), stacks_root=str(tmp_path))
    rows = [e for e in store.list_events() if e["kind"] == "mission.protected_override"]
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["resource"] == "host:local/path:/etc/hosts" and payload["operator"] == "operator:chris"
    assert "dhcp" in payload["reason"] and "hunter2" not in payload["reason"]


def test_no_overrides_means_no_record_and_no_refusal(tmp_path, cgroup2_mount):
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    c = MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]})
    assert build_mission_runner(store, c, containment=FakeContainment(), stacks_root=str(tmp_path)) is not None
    assert not [e for e in store.list_events() if e["kind"] == "mission.protected_override"]
