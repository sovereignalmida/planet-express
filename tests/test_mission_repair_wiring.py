"""Daemon wiring of the Repair tier (slice 7a): the controls slices 5, 6 and 8 built are constructed together, behind
`missions.enabled`, and handed to the Supervisor and the runner."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from checkpoint_fakes import FakeDocker
from missions_helpers import make_store
from verify_fakes import snapshot

from config_schema import MissionsConfig
from planet_express.missions.assembly import (
    RepairWiring,
    build_approval_flow,
    discover_inventory,
    mission_write_gate,
)
from planet_express.missions.checkpoint import SystemdCheckpointProvider
from planet_express.missions.containment import FakeContainment
from planet_express.missions.runner import MissionRunner, build_mission_runner


def wiring(stacks, scan=None):
    docker = FakeDocker()
    provider = SystemdCheckpointProvider(SimpleNamespace(stacks_root=stacks), run=docker, run_bounded=docker.bounded,
                                         read_text=lambda path: (_ for _ in ()).throw(OSError(path)))
    return RepairWiring(provider=provider, scan=scan or (lambda: snapshot()))


def stacks_tree(tmp_path):
    root = tmp_path / "stacks"
    (root / "media").mkdir(parents=True)
    (root / "media" / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n  db:\n    image: pg\n")
    (root / "photos").mkdir()
    (root / "photos" / "compose.yaml").write_text("services:\n  app:\n    image: x\n")
    (root / "empty").mkdir()
    return str(root)


def cfg(**over):
    return MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]}, **over)


def test_everything_is_built_together_and_passed_to_the_supervisor_and_the_runner(tmp_path):
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)
    # An old config's tripwire_accept_blind is accepted and ignored: trust mode removed the tripwire, nothing reads it.
    runner = build_mission_runner(store, cfg(tripwire_accept_blind=["host:local/path:/etc/shadow"], protected_absent=["backup"]),
                                  containment=FakeContainment(), stacks_root=stacks, repair_wiring=wiring(stacks))
    sup, controls = runner.supervisor, runner.controls
    assert sup.checkpoint is controls.checkpoint and sup.verifier is controls.verifier
    assert not hasattr(controls, "repair_gate") and not hasattr(controls, "backup_status") and not hasattr(controls, "protected")
    assert sup.mutating_ready and not hasattr(controls, "tripwire") and not hasattr(runner, "tripwire")
    assert str(controls.checkpoint.store.root) == str(Path(store.path).parent / "checkpoints")  # the default root
    assert runner.startup == [controls.checkpoint.startup_scan]
    # the baseline provider is a real normaliser over the scan: a failing scan means no checkpoint
    assert controls.checkpoint.baseline_provider() is not None
    assert sup.tool_broker is None or sup.tool_broker.write_gate is not None


def test_the_checkpoint_root_comes_from_config_and_old_backup_sources_are_ignored(tmp_path):
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)
    root = tmp_path / "elsewhere" / "cp"
    c = cfg(checkpoint={"root": str(root), "backup_sources": [{"path": stacks, "marker": str(tmp_path / "marker")}]})
    runner = build_mission_runner(store, c, containment=FakeContainment(), stacks_root=stacks, repair_wiring=wiring(stacks))
    assert str(runner.controls.checkpoint.store.root) == str(root)


def test_the_runner_starts_and_stops_with_repair_wired_and_no_monitor_thread(tmp_path):
    import threading
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=stacks, repair_wiring=wiring(stacks),
                                  interval_s=0.05)
    runner.start()
    try:
        assert runner._thread is not None
        assert not [t for t in threading.enumerate() if "tripwire" in t.name.lower()]
    finally:
        runner.stop()


def test_missions_disabled_constructs_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)
    calls = []

    class Exploding:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("constructed with missions off")
    off = MissionsConfig(enabled=False, runtime={"argv": ["/usr/bin/agent"]})
    assert build_mission_runner(store, off, containment=FakeContainment(), stacks_root=stacks, repair_wiring=Exploding()) is None
    assert calls == []
    assert build_approval_flow(store, off, None, None) is None


def test_a_control_that_cannot_be_built_leaves_the_host_without_repair_but_with_observe_and_propose(tmp_path, monkeypatch):
    import planet_express.missions.assembly as assembly
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)

    def broken(*a, **k):
        raise OSError("cannot create the verifier")
    monkeypatch.setattr(assembly, "verifier_from_config", broken)
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=stacks, repair_wiring=wiring(stacks))
    assert runner is not None                       # observe/propose still run
    assert runner.controls is None and not runner.supervisor.mutating_ready
    assert runner.supervisor.checkpoint is None and runner.supervisor.verifier is None      # all or none: no partial safety net
    assert build_approval_flow(store, cfg(), runner, runner.controls) is None


def test_the_inventory_lists_stacks_and_their_services(tmp_path):
    inv = discover_inventory(stacks_tree(tmp_path))
    assert set(inv.stacks) == {"media", "photos"}                       # a directory with no compose file is not a stack
    assert set(inv.services) == {"media/web", "media/db", "photos/app"}
    assert inv.hosts == ("local",) and discover_inventory(str(tmp_path / "missing")).stacks == ()


def test_the_built_validator_refuses_what_is_not_in_the_inventory(tmp_path):
    from planet_express.missions.models import Grant, VerificationPlan
    store, _ = make_store(tmp_path)
    stacks = stacks_tree(tmp_path)
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=stacks, repair_wiring=wiring(stacks))
    plan = VerificationPlan.model_validate({"catalogue_v": 1, "checks": [{"id": "c", "check": "http", "params": {"url": "http://x/"}}]})
    base = {"tier": "repair", "verification": {"checks": ["c"], "plan_sha256": plan.hash()},
            "budgets": {"wall_s": 900, "tool_calls": 100, "output_bytes": 100000, "retries": 1}}
    assert runner.controls.validator(Grant.model_validate({**base, "scope": {"stacks": ["media"]}}), plan) == []
    bad = Grant.model_validate({**base, "scope": {"stacks": ["nope"]}})
    assert any("not in the discovered inventory" in r for r in runner.controls.validator(bad, plan))


def test_the_brokered_write_gate_opens_only_for_an_executing_approved_repair_mission(tmp_path):
    from missions_helpers import advance_to, new_mission

    from planet_express.missions.approvals import record_approval
    store, _ = make_store(tmp_path)
    from planet_express.missions.store import MissionStore
    ms = MissionStore(store)
    gate = mission_write_gate(store)
    m = new_mission(ms)
    assert gate(m.id, "arr.queue_item", {}) is False and gate("M-404", "x", {}) is False
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert gate(m.id, "arr.queue_item", {}) is False                    # approved but not executing
    advance_to(ms, m.id, "executing", actor="core")
    assert gate(m.id, "arr.queue_item", {}) is True
    advance_to(ms, m.id, "needs_operator", actor="core")
    assert gate(m.id, "arr.queue_item", {}) is False                    # a mission handed to the operator has its tools closed


def test_the_daemon_helper_builds_the_wiring_only_when_missions_are_enabled(monkeypatch, tmp_path):
    import casa_farnsworth as farnsworth
    monkeypatch.setattr(farnsworth.config, "MISSIONS_ENABLED", False)
    assert farnsworth._repair_wiring() is None
    monkeypatch.setattr(farnsworth.config, "MISSIONS_ENABLED", True)
    monkeypatch.setattr(farnsworth.config, "get_stack_orchestrator", lambda: SimpleNamespace(stacks_root=tmp_path))
    monkeypatch.setattr(farnsworth.config, "HOST_CONTROL_PROVIDER", "mos")
    from planet_express.missions.checkpoint import MosCheckpointProvider
    w = farnsworth._repair_wiring()
    assert isinstance(w.provider, MosCheckpointProvider) and w.scan is farnsworth.leela.run_full
    monkeypatch.setattr(farnsworth.config, "HOST_CONTROL_PROVIDER", "systemd")
    assert isinstance(farnsworth._repair_wiring().provider, SystemdCheckpointProvider)


def test_a_runner_without_the_repair_wiring_still_runs_observe_and_propose(tmp_path):
    store, _ = make_store(tmp_path)
    runner = build_mission_runner(store, cfg(), containment=FakeContainment())
    assert isinstance(runner, MissionRunner) and runner.controls is None and not runner.supervisor.mutating_ready
