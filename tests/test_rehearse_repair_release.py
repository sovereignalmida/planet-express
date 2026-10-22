"""The repair rehearsal ends each scenario cleanly: nothing is left holding leases into the next one, a prepare that cannot reach
its baseline fails, and failure text appears only on failed steps. Fakes only; nothing touches Docker."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("rehearse_repair_release", ROOT / "scripts/rehearse_repair.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearse_repair_release"] = module
    spec.loader.exec_module(module)
    return module


class FakeMissions:
    def __init__(self, statuses, stubborn=()):
        self.status, self.stubborn, self.cancelled, self.ticks = dict(statuses), set(stubborn), [], 0

    def status_of(self, mid):
        return self.status[mid]

    def cancel(self, mid, by):
        self.cancelled.append((mid, by))
        if mid not in self.stubborn:
            self.status[mid] = "cancelled"

    def tick(self):
        self.ticks += 1


def release(mod, fake, mids, label="namespace"):
    rec = mod.Recorder()
    ok = mod.release_missions(rec, label, mids, status_of=fake.status_of, cancel=fake.cancel, tick=fake.tick,
                              sleep=lambda s: None, timeout_s=0.05)
    return ok, rec


def test_a_non_terminal_mission_is_cancelled_through_the_real_cancel_path_and_recorded(mod):
    fake = FakeMissions({"m1": "needs_operator", "m2": "completed"})
    ok, rec = release(mod, fake, ["m1", "m2"])
    assert ok and fake.cancelled == [("m1", mod.OPERATOR)]
    step = rec.steps[-1]
    assert step["step"] == "namespace:released" and step["ok"] and step["final"] == {"m1": "cancelled", "m2": "completed"}


def test_terminal_missions_are_left_alone(mod):
    fake = FakeMissions({"m1": "completed", "m2": "rolled_back"})
    ok, rec = release(mod, fake, ["m1", "m2"], "canary")
    assert ok and fake.cancelled == [] and rec.steps[-1]["step"] == "canary:released"


def test_a_mission_that_will_not_end_is_a_failed_release_with_its_status(mod):
    fake = FakeMissions({"m1": "rollback_pending"}, stubborn={"m1"})
    ok, rec = release(mod, fake, ["m1"])
    assert not ok and not rec.steps[-1]["ok"] and rec.steps[-1]["final"] == {"m1": "rollback_pending"}
    assert "rollback_pending" in rec.steps[-1]["detail"] and fake.ticks > 0


def test_a_failed_cancel_is_recorded_not_raised(mod):
    def boom(mid, by):
        raise RuntimeError("kill failed")
    rec = mod.Recorder()
    ok = mod.release_missions(rec, "namespace", ["m1"], status_of=lambda m: "executing", cancel=boom,
                              tick=lambda: None, sleep=lambda s: None, timeout_s=0.05)
    assert not ok and "kill failed" in rec.steps[-1]["detail"]


def test_no_missions_is_a_clean_release(mod):
    ok, rec = release(mod, FakeMissions({}), [])
    assert ok and rec.steps[-1]["ok"]


def test_a_prepare_that_reaches_baseline_says_nothing_is_wrong(mod, tmp_path):
    rec = mod.Recorder()

    def run(argv, timeout=120):
        if argv[:2] == ["docker", "ps"]:
            return 0, "i1 gateway\ni2 app1\ni3 app2\ni4 web\n"
        return 0, ""
    assert mod.prepare_scenario(rec, "namespace", tmp_path, "busybox:1.36", run=run) is True
    assert rec.steps[-1]["ok"] and "detail" not in rec.steps[-1]


def test_a_prepare_that_cannot_reach_baseline_fails_and_names_why(mod, tmp_path):
    rec = mod.Recorder()

    def run(argv, timeout=120):
        if argv[:2] == ["docker", "ps"]:
            return 0, "i1 gateway\ni4 web\n"
        return 0, ""
    assert mod.prepare_scenario(rec, "canary", tmp_path, "busybox:1.36", run=run) is False
    step = rec.steps[-1]
    assert not step["ok"] and "app1" in step["detail"] and "app2" in step["detail"]
    assert rec.failures and not rec.document(scenario="x")["ok"]

    rec = mod.Recorder()
    assert mod.prepare_scenario(rec, "canary", tmp_path, "busybox:1.36", run=lambda a, timeout=120: (1, "boom")) is False
    assert "compose up failed" in rec.steps[-1]["detail"]


def test_the_through_proxy_failure_names_the_first_refused_calls(mod):
    summary = {"writes": 0, "created": 0, "further_refusals_not_recorded": False,
               "refused": [{"reason": "not served", "route": None, "method": "GET", "path": "/v1.47/version"},
                           {"reason": "x", "route": "r", "method": "POST", "path": "/p"},
                           {"reason": "y", "route": "r", "method": "POST", "path": "/q"},
                           {"reason": "z", "route": "r", "method": "POST", "path": "/s"}]}
    text = mod.through_proxy_detail(summary)
    assert "GET /v1.47/version: not served" in text and "POST /p: x" in text and "POST /q: y" in text
    assert "/s" not in text and "no container create" in text
    assert "no refused" in mod.through_proxy_detail({"writes": 0, "created": 0, "refused": []})
