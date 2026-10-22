"""Release proof item 1:

    v3 behaviour and existing installations pass their regression suites; missions disabled by default.

The v3 suites themselves run as part of the full suite (this slice does not touch them). This module proves what a test
can: missions are off in every default, nothing starts when they are off, and no v3 test file has been dropped."""
import shutil
import subprocess
import threading

import pytest
from checkpoint_fakes import make_env
from missions_helpers import make_store
from supervisor_fakes import Rig
from tripwire_fakes import make_rig

from config_schema import MissionsConfig
from planet_express.application.mission_service import MissionService
from planet_express.integrations.rpc import RpcError, build_core_handlers
from planet_express.missions.checkpoint import CheckpointDisabled
from release_proof.support import ROOT


def test_the_schema_default_and_the_shipped_example_config_have_missions_off():
    import config_io
    assert MissionsConfig().enabled is False
    assert config_io.load_config_file(ROOT / "config.example.yaml").missions.enabled is False


@pytest.mark.parametrize("method", ["mission.list", "mission.get", "memory.cards", "memory.search"])
def test_the_mission_and_memory_rpcs_answer_disabled_by_default(tmp_path, method):
    store, _ = make_store(tmp_path)
    handlers = build_core_handlers(None, store, missions=MissionService(store))
    with pytest.raises(RpcError) as exc:
        handlers[method]({})
    assert exc.value.code == "disabled"


def test_a_disabled_supervisor_starts_nothing_and_a_disabled_checkpoint_runs_no_code(tmp_path):
    rig = Rig(tmp_path, enabled=False)
    before = threading.active_count()
    rig.sup.tick()
    assert rig.status() == "queued" and rig.runtimes == [] and threading.active_count() == before
    env = make_env(tmp_path / "ck", enabled=False)
    with pytest.raises(CheckpointDisabled):
        env.svc.create(env.mission.id, env.scope)
    assert env.docker.calls == []


def test_a_disabled_install_starts_no_monitor_thread(tmp_path):
    rig = make_rig(tmp_path, enabled=False)
    assert rig.monitor.start() is None
    assert not any(t.name == "tripwire-monitor" for t in threading.enumerate())


def test_no_v3_test_file_has_been_deleted_or_renamed_since_v3_next():
    """A v4 slice may extend a v3 test (a schema table, a policy case) but never drop one."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    probe = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", "v3-next"], capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("no v3-next ref in this checkout")
    out = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-status", "--diff-filter=DR", "v3-next", "HEAD", "--", "tests"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "", out
