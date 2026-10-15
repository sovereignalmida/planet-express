"""A Repair-tier start refuses unless every side door is closed; Observe and Propose are unaffected."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor_fakes import GOOD_RESULT, Rig, update

from planet_express.missions.launcher.preflight import Check, PreflightReport
from planet_express.missions.supervisor import RepairRefused, UnsupportedTier


def report(ok: bool, name="docker-tcp-exposure", detail="unauthenticated Docker TCP: tcp://0.0.0.0:2375") -> PreflightReport:
    r = PreflightReport("side-doors")
    r.checks.append(Check(name, ok, "" if ok else detail))
    return r


def rig_with_gate(tmp_path, tier, gate):
    rig = Rig(tmp_path, tier=tier, behaviours=[{"script": [update("finished")]}])
    rig.sup.repair_gate = gate
    return rig


def test_repair_start_is_refused_while_a_side_door_is_open_and_names_it(tmp_path):
    rig = rig_with_gate(tmp_path, "repair", lambda: report(False))
    with pytest.raises(RepairRefused, match="2375"):
        rig.sup.start(rig.mid)
    assert rig.status() == "queued" and rig.runtimes == [] and rig.sessions() == []


def test_a_gate_that_crashes_refuses(tmp_path):
    def boom():
        raise RuntimeError("probe blew up")
    rig = rig_with_gate(tmp_path, "repair", boom)
    with pytest.raises(RepairRefused):
        rig.sup.start(rig.mid)


def test_no_gate_configured_refuses_a_repair_start_too(tmp_path):
    rig = Rig(tmp_path, tier="repair")
    with pytest.raises(RepairRefused):
        rig.sup.start(rig.mid)
    assert issubclass(RepairRefused, UnsupportedTier)        # the S3 behaviour for mutating tiers is unchanged


@pytest.mark.parametrize("tier", ["change", "critical"])
def test_every_mutating_tier_goes_through_the_gate(tmp_path, tier):
    rig = rig_with_gate(tmp_path, tier, lambda: report(False))
    with pytest.raises(RepairRefused):
        rig.sup.start(rig.mid)


def test_with_every_door_closed_the_supervisor_still_does_not_run_repair_itself(tmp_path):
    rig = rig_with_gate(tmp_path, "repair", lambda: report(True))
    with pytest.raises(UnsupportedTier) as exc:
        rig.sup.start(rig.mid)
    assert not isinstance(exc.value, RepairRefused)         # slice 7 runs repairs; the gate itself passed


@pytest.mark.parametrize("tier", ["observe", "propose"])
def test_observe_and_propose_never_consult_the_gate(tmp_path, tier):
    called = []
    rig = rig_with_gate(tmp_path, tier, lambda: called.append(1) or report(False))
    rig.sup.start(rig.mid)
    assert called == [] and rig.runtimes and GOOD_RESULT


def test_the_gate_is_asked_again_each_time_not_cached_as_open(tmp_path):
    answers = [report(True), report(False)]
    rig = rig_with_gate(tmp_path, "repair", lambda: answers.pop(0))
    with pytest.raises(UnsupportedTier) as first:
        rig.sup.start(rig.mid)
    assert not isinstance(first.value, RepairRefused)
    with pytest.raises(RepairRefused):
        rig.sup.start(rig.mid)
