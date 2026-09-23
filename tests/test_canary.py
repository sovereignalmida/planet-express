"""`update.canary` on the engine (slice 5b-3, design §4.5): the two phases, the rollback window,
the automatic inverse and crash reconciliation."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import engine, runbook as runbooks
from tests.canary_fakes import (
    CONTAINER,
    NEW,
    OLD,
    REFERENCE,
    CanarySvc,
    canary_step,
    execution,
)


@pytest.fixture
def svc(tmp_path):
    return CanarySvc(tmp_path)


def run(svc, origin="zoidberg"):
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "Canary update media/sonarr", "steps": [canary_step(svc)], "artifacts": {}})
    result = engine.RunbookEngine(svc).run(ex, plan, origin=origin)
    return ex, result, svc._store.list_steps(ex)[0]


def open_candidates(svc):
    with svc._store._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM rollback_candidates WHERE closed_at IS NULL")]


def joined(svc):
    return [" ".join(argv) for argv in svc.argv]


def test_a_new_image_is_deployed_pinned_verified_and_the_window_closes(svc):
    _ex, result, step = run(svc)
    assert result.status == "passed"
    assert (step["status"], step["effect"]) == ("passed", "applied")
    assert step["output"] == {"image_reference": REFERENCE, "old_image_id": OLD, "new_image_id": NEW}
    assert f"docker tag {NEW} {REFERENCE}" in joined(svc)
    assert any("up -d --pull never" in line for line in joined(svc))  # never a second pull
    assert svc.running == NEW
    assert open_candidates(svc) == []


def test_an_unchanged_image_passes_without_recreating_anything(svc):
    svc.pulled = OLD
    _ex, result, step = run(svc)
    assert result.status == "passed"
    assert (step["status"], step["effect"]) == ("passed", "not_applied")
    assert step["output"] == {"image_reference": REFERENCE, "old_image_id": OLD, "new_image_id": OLD}
    assert not any("up -d" in line for line in joined(svc))
    assert open_candidates(svc) == []


def test_the_rollback_window_holds_the_running_image_while_the_update_is_in_flight(svc):
    svc.watch_result = (False, "restarting (3 restarts)")
    ex, _result, _step = run(svc)
    # it closed at the end, but it carried the image the inverse needed while it was open
    with svc._store._connect() as conn:
        row = dict(conn.execute("SELECT * FROM rollback_candidates WHERE execution_id=?",
                                (ex,)).fetchone())
    assert (row["old_image_id"], row["image_reference"]) == (OLD, REFERENCE)
    assert row["closed_at"] is not None


def test_a_failing_canary_watch_is_rolled_back_to_the_previous_image(svc):
    svc.watch_result = (False, "restarting (3 restarts)")   # the restored image watches clean
    _ex, result, step = run(svc)
    assert result.status == "failed"
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "canary watch failed" in step["reason"] and "rolled back" in step["reason"]
    assert step["output"]["old_image_id"] == OLD
    assert f"docker tag {OLD} {REFERENCE}" in joined(svc)
    assert joined(svc).index(f"docker tag {OLD} {REFERENCE}") > joined(svc).index(
        f"docker tag {NEW} {REFERENCE}")                      # new image first, then the undo
    assert svc.running == OLD
    assert open_candidates(svc) == []


def test_a_rollback_whose_restored_image_is_unstable_is_not_a_successful_rollback(svc):
    """An old image that crash-loops on restore is not a rollback: saying it was would close the
    window protecting that image (Codex, T42)."""
    svc.watch_result = (False, "restarting (3 restarts)")
    svc.rollback_watch_result = (False, "restarting (2 restarts)")
    _ex, _result, step = run(svc)
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    assert "the restored image is not stable" in step["reason"]
    held = open_candidates(svc)
    assert len(held) == 1 and held[0]["expires_at"] > svc._clock() + 365 * 86400


def test_a_deploy_that_lands_the_wrong_image_is_rolled_back(svc):
    svc.deploy_breaks = True
    _ex, result, step = run(svc)
    assert result.status == "failed" and "expected" in step["reason"]
    assert step["effect"] == "not_applied" and "rolled back" in step["reason"]
    assert svc.running == OLD


def test_a_rollback_that_also_fails_stays_unknown_and_keeps_its_window_open(svc):
    svc.watch_result = (False, "restarting (3 restarts)")
    svc.fail = {f"docker tag {OLD}": 1}
    _ex, _result, step = run(svc)
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    assert "rollback also failed" in step["reason"]
    held = open_candidates(svc)
    # prune stays blocked until a human sorts it out — past the ordinary grace period
    assert len(held) == 1 and held[0]["expires_at"] > svc._clock() + 365 * 86400


def test_a_pull_failure_changes_nothing_and_closes_the_window(svc):
    svc.fail = {"pull": 1}
    _ex, _result, step = run(svc)
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert step["reason"].startswith("pull failed")
    assert not any("up -d" in line for line in joined(svc))
    assert open_candidates(svc) == []


def test_a_digest_pinned_or_build_only_service_is_refused_before_anything_runs(svc):
    svc.reference = "ghcr.io/org/app@sha256:" + "a" * 64
    _ex, _result, step = run(svc)
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "not a canary-eligible image reference" in step["reason"]
    assert open_candidates(svc) == [] and not any("pull" in line for line in joined(svc))


def test_a_reference_that_moves_mid_update_refuses_phase_two(svc, monkeypatch):
    references = iter([REFERENCE, "nginx:1.28-alpine"])
    original = svc._run_argv

    def run_argv(argv, timeout=None):
        if "config --images" in " ".join(argv):
            svc.argv.append(argv)
            return 0, next(references) + "\n", ""
        return original(argv, timeout)

    monkeypatch.setattr(svc, "_run_argv", run_argv)
    _ex, _result, step = run(svc)
    assert step["reason"] == "the image reference changed during the update"
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert not any("up -d" in line for line in joined(svc))


def test_an_interrupted_update_that_never_deployed_is_not_applied(svc):
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "pull_dispatched", "container": CONTAINER,
                                          "running_image_id": OLD, "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=2_000_000.0)
    svc._store.mark_step_dispatched(ex, 1)
    engine.startup_reconcile(svc._store, svc)
    step = svc._store.list_steps(ex)[0]
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "still running" in step["reason"]
    assert open_candidates(svc) == []


def test_an_interrupted_update_that_already_deployed_stays_unknown(svc):
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "pulled", "container": CONTAINER,
                                          "running_image_id": OLD, "pulled_image_id": NEW,
                                          "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=2_000_000.0)
    svc._store.mark_step_dispatched(ex, 1)
    svc.running = NEW
    engine.startup_reconcile(svc._store, svc)
    step = svc._store.list_steps(ex)[0]
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    assert "never watched" in step["reason"]
    assert len(open_candidates(svc)) == 1


def test_a_crash_after_dispatch_leaves_the_window_pinned_open(svc, monkeypatch):
    """Nothing about an interrupted update is known, so its image must survive every prune until a
    human settles it — not just for the grace period (Codex, T42)."""
    from planet_express.core.store import INDEFINITE_EXPIRY

    def explode(container, seconds, **kwargs):
        raise RuntimeError("docker went away mid-watch")

    monkeypatch.setattr(svc, "_watch", explode)
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    result = engine.RunbookEngine(svc).run(ex, plan, origin="zoidberg")
    assert result.status == "failed"
    held = open_candidates(svc)
    assert len(held) == 1 and held[0]["expires_at"] >= INDEFINITE_EXPIRY
    assert svc._store.any_open_rollback_candidate(svc._clock() + 30 * 86400)


def test_an_interrupted_deployed_update_pins_its_window_too(svc):
    from planet_express.core.store import INDEFINITE_EXPIRY

    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "pulled", "container": CONTAINER,
                                          "running_image_id": OLD, "pulled_image_id": NEW,
                                          "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=svc._clock() + 900)
    svc._store.mark_step_dispatched(ex, 1)
    svc.running = NEW
    engine.startup_reconcile(svc._store, svc)
    held = open_candidates(svc)
    assert len(held) == 1 and held[0]["expires_at"] >= INDEFINITE_EXPIRY


def test_a_container_recreated_during_the_pull_stops_phase_two(svc, monkeypatch):
    """A pull can take minutes; the container can be recreated in that time without the compose
    file or the reference changing (Codex, T42)."""
    original = svc._binder.service
    calls = []

    def recreate_after_the_pull(target, *, timeout):
        binding = original(target, timeout=timeout)
        calls.append(binding)
        # 1: the plan's own binding, 2: the check before the pull, 3+: phase two's re-check
        if len(calls) > 2:
            binding = binding | {"container_id": "f" * 64}
        return binding

    monkeypatch.setattr(svc._binder, "service", recreate_after_the_pull)
    _ex, _result, step = run(svc)
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "recreated" in step["reason"] and "during the update" in step["reason"]
    assert not any("up -d" in " ".join(argv) for argv in svc.argv)


def test_a_long_but_valid_reference_is_eligible_and_storable():
    """The eligibility check and the output model must agree, or a deploy that succeeded is
    recorded as a crash when its outputs fail validation (Codex, T42)."""
    from planet_express.execution import actions as actions_module
    reference = "registry.example.com/" + "a" * 400 + ":2026-09-23"
    assert len(reference) < runbooks.IMAGE_REFERENCE_MAX
    assert actions_module.is_canary_reference(reference)
    runbooks.validate_outputs("update.canary", {"image_reference": reference,
                                                "old_image_id": OLD, "new_image_id": NEW})
    assert not actions_module.is_canary_reference("r" * 600 + ":tag")


def test_reconciliation_can_reopen_a_window_closed_just_before_a_crash(svc):
    """Closing the window and recording the step are two writes; a crash between them must not
    leave the old image unprotected under an unfinished step (Codex, T42)."""
    from planet_express.core.store import INDEFINITE_EXPIRY

    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "deploying", "container": CONTAINER,
                                          "running_image_id": OLD, "pulled_image_id": NEW,
                                          "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=svc._clock() + 900)
    svc._store.mark_step_dispatched(ex, 1)
    assert svc._store.close_rollback_candidate(ex, 1)      # the watch passed, then the crash
    svc.running = NEW

    engine.startup_reconcile(svc._store, svc)

    step = svc._store.list_steps(ex)[0]
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    held = open_candidates(svc)
    assert len(held) == 1 and held[0]["expires_at"] >= INDEFINITE_EXPIRY


def test_an_interrupted_rollback_is_never_called_not_applied(svc):
    """The old image being back does not mean the rollback was verified — that watch never ran."""
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "rolling_back", "container": CONTAINER,
                                          "running_image_id": OLD, "pulled_image_id": NEW,
                                          "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=svc._clock() + 900)
    svc._store.mark_step_dispatched(ex, 1)
    svc.running = OLD                                       # the inverse landed, unwatched

    engine.startup_reconcile(svc._store, svc)

    step = svc._store.list_steps(ex)[0]
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    assert "never watched" in step["reason"]
    assert len(open_candidates(svc)) == 1


def test_settling_an_engine_crash_keeps_the_canary_window_open(svc):
    """`settle_crashed_execution` is the in-process twin of startup reconciliation, and it has to
    protect the image the same way (Codex, T42 round 7)."""
    from planet_express.core.store import INDEFINITE_EXPIRY

    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    svc._store.create_steps(ex, plan.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "update.canary", "media/sonarr")],
                                        window_start=0, cooldown_start=0, max_per_day=9, now=1)
    svc._store.set_step_pre_state(ex, 1, {"phase": "deploying", "container": CONTAINER,
                                          "running_image_id": OLD, "image_reference": REFERENCE})
    svc._store.open_rollback_candidate(ex, 1, stack="media", service="sonarr",
                                       image_reference=REFERENCE, old_image_id=OLD,
                                       expires_at=svc._clock() + 900)
    svc._store.mark_step_dispatched(ex, 1)
    assert svc._store.close_rollback_candidate(ex, 1)      # closed just before the crash

    engine.settle_crashed_execution(svc._store, ex)

    step = svc._store.list_steps(ex)[0]
    assert (step["status"], step["effect"]) == ("failed", "unknown")
    held = open_candidates(svc)
    assert len(held) == 1 and held[0]["expires_at"] >= INDEFINITE_EXPIRY
