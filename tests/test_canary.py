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
    SERVICE,
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
    """The compose file is edited between the resolve and the deploy. A pull can take minutes.
    Read from the keyed config now, so this is about this service's image and nothing else."""
    images = iter([REFERENCE, "nginx:1.28-alpine"])
    original = svc._run_argv

    def run_argv(argv, timeout=None):
        if "config --format json" in " ".join(argv):
            svc.argv.append(argv)
            import json as _json
            return 0, _json.dumps({"services": {SERVICE: {"image": next(images)}}}), ""
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


def test_a_rollback_is_never_refused_by_a_cooldown(svc, monkeypatch):
    """You always roll back something that just happened, so a per-target cooldown would refuse
    exactly the runs that most need to work (found in the T44 VM rehearsal)."""
    import config as config_module
    from config_schema import AutonomyConfig
    from tests.canary_fakes import canary_step
    monkeypatch.setattr(config_module, "AUTONOMY",
                        AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=1))
    first = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    assert engine.RunbookEngine(svc).run(first, plan, origin="zoidberg").status == "passed"

    # the same target again, as a planner would: refused
    svc.pulled = "3" * 64
    blocked = execution(svc)
    assert engine.RunbookEngine(svc).run(blocked, plan, origin="planner").status == "failed"

    # ...but an operator's undo runs
    undo = execution(svc)
    assert engine.RunbookEngine(svc).run(undo, plan, origin="rollback").status == "passed"


# ── 2026-09-27: a gluetun canary took Traefik down for four hours ───────────────
#
# `docker compose config --images <service>` returns every image in the RELATED SET, in a
# non-deterministic order. Compose v2.39.1, three consecutive runs against the live network
# stack:
#
#     run1: traefik:v3.6.25 qmcgaw/gluetun:v3.41.1
#     run2: qmcgaw/gluetun:v3.41.1 traefik:v3.6.25
#     run3: traefik:v3.6.25 qmcgaw/gluetun:v3.41.1
#
# The canary took line [0]. At 05:45 that was traefik's. It pulled traefik, watched
# CASA_GLUETON, found it still on gluetun's image -- as it always would be -- called that a
# failure, and its automatic rollback ran `docker tag <gluetun's image> traefik:v3.6.25`.
# Traefik then exited with "command is unknown: --configFile=/etc/traefik/traefik.yml" on
# every start, with restart: no, until someone noticed four hours later.
#
# The reference now comes from the container being updated, and compose has to CONFIGURE that
# same image for that same service -- read from the keyed JSON config, not from flattened
# lines. Two independent single-valued sources that must agree.

GLUETUN = "qmcgaw/gluetun:v3.41.1"
TRAEFIK = "traefik:v3.6.25"


def _network_stack(svc):
    """The shape that caused it: one file, two services, the canary bound to the first."""
    svc.container_reference = GLUETUN
    svc.compose_config = {SERVICE: GLUETUN, "traefik": TRAEFIK}


def test_a_related_services_image_is_never_chosen_for_this_one(svc):
    """The whole outage in one assertion."""
    _network_stack(svc)

    _ex, result, step = run(svc)

    assert result.status == "passed"
    assert step["output"]["image_reference"] == GLUETUN
    assert f"docker tag {NEW} {GLUETUN}" in joined(svc)
    assert not any(TRAEFIK in line for line in joined(svc)), "another service's tag was touched"


def test_a_reference_some_related_service_uses_is_not_good_enough(svc):
    """Membership in compose's related set proves only that SOMETHING uses the reference. If
    the container is stale after a config change and its old reference is still some sibling's
    image, that has to be refused, not accepted."""
    svc.container_reference = TRAEFIK                       # stale: this is the sibling's image
    svc.compose_config = {SERVICE: GLUETUN, "traefik": TRAEFIK}

    _ex, _result, step = run(svc)

    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "compose configures" in step["reason"]
    assert not any("pull" in line for line in joined(svc))
    assert not any("docker tag" in line for line in joined(svc))


def test_a_container_running_something_compose_does_not_configure_is_refused(svc):
    svc.container_reference = "nginx:1.27-alpine"
    svc.compose_config = {SERVICE: GLUETUN, "traefik": TRAEFIK}

    _ex, _result, step = run(svc)

    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "compose configures" in step["reason"]


def test_a_service_compose_configures_no_image_for_is_refused(svc):
    svc.compose_config = {"traefik": TRAEFIK}
    _ex, _result, step = run(svc)
    assert step["status"] == "failed"
    assert "configures no image" in step["reason"]


def test_a_rollback_refuses_to_tag_a_reference_the_container_does_not_run(svc, monkeypatch):
    """Belt and braces over the fix above. The tag is the one irreversible thing a canary does
    to state another service shares, so it is checked against the bound container immediately
    before it runs -- the cost of being wrong here is somebody else's outage."""
    _network_stack(svc)
    original = svc._run_argv
    asked = []

    def drifting(argv, timeout=None):
        # The first ask resolves the step's reference; by the second -- the check immediately
        # before the tag -- the container has been recreated on something else.
        if argv[:3] == ["docker", "inspect", "--format"] and "{{.Config.Image}}" in " ".join(argv):
            asked.append(argv)
            if len(asked) > 1:
                svc.container_reference = TRAEFIK
        return original(argv, timeout=timeout)

    monkeypatch.setattr(svc, "_run_argv", drifting)
    _ex, _result, step = run(svc)

    assert step["status"] == "failed"
    assert "refusing to tag" in step["reason"]
    assert not any("docker tag" in line for line in joined(svc))


def test_a_rollback_still_runs_when_the_container_is_gone(svc, monkeypatch):
    """A failed recreate can remove the old container before making its replacement, and that
    is exactly when the inverse has to run. Refusing there would leave the service absent and
    the shared reference pointing at the image that just failed."""
    _network_stack(svc)
    svc.deploy_breaks = True               # the deploy lands the wrong image, forcing a rollback
    original = svc._run_argv
    asked = []

    def vanishing(argv, timeout=None):
        # Asked three times: resolving the reference, the forward guard, then the inverse's.
        if argv[:3] == ["docker", "inspect", "--format"] and "{{.Config.Image}}" in " ".join(argv):
            asked.append(argv)
            if len(asked) > 2:             # the failed recreate removed it
                return 1, "", "No such object: CASA_GLUETON"
        return original(argv, timeout=timeout)

    monkeypatch.setattr(svc, "_run_argv", vanishing)
    _ex, _result, step = run(svc)

    assert f"docker tag {OLD} {GLUETUN}" in joined(svc), "the inverse did not restore the image"
    assert "refusing to tag" not in (step["reason"] or "")


def test_going_forward_an_unreadable_container_stops_the_tag(svc, monkeypatch):
    """Only the inverse gets to proceed on an unreadable container. Going forward there is no
    urgency, and an inspect that timed out while the container was being recreated on
    something else is exactly how the wrong image gets tagged."""
    _network_stack(svc)
    original = svc._run_argv
    asked = []

    def unreadable(argv, timeout=None):
        if argv[:3] == ["docker", "inspect", "--format"] and "{{.Config.Image}}" in " ".join(argv):
            asked.append(argv)
            if len(asked) > 1:             # the forward guard cannot see it
                return 1, "", "context deadline exceeded"
        return original(argv, timeout=timeout)

    monkeypatch.setattr(svc, "_run_argv", unreadable)
    _ex, _result, step = run(svc)

    assert step["status"] == "failed"
    assert "cannot read what" in step["reason"]
    # The forward tag never happened; the pull had already moved the reference, so the
    # inverse still puts it back. That is the one tag allowed here.
    assert f"docker tag {NEW} {GLUETUN}" not in joined(svc)
    assert f"docker tag {OLD} {GLUETUN}" in joined(svc)


# -- the weekly digest -------------------------------------------------------------------------------------------
def test_the_digest_says_what_was_updated_what_was_current_and_what_needs_a_person():
    import casa_zoidberg as zoidberg
    results = [
        {"stack": "media", "service": "radarr", "status": "updated", "reason": "x"},
        {"stack": "media", "service": "sonarr", "status": "updated", "reason": "x"},
        {"stack": "services", "service": "planka", "status": "no_change"},
        {"stack": "services", "service": "miniflux", "status": "no_change"},
        {"stack": "media", "service": "unpackerr", "status": "skipped",
         "reason": "golift/unpackerr is not a canary-eligible image reference"},
        {"stack": "media", "service": "redis", "status": "skipped",
         "reason": "docker.io/redis:6.2-alpine@sha256:abc is not a canary-eligible image reference"},
        {"stack": "services", "service": "porta", "status": "skipped", "reason": "no image reference (build-only service)"},
        {"stack": "vikunja", "service": "sync", "status": "pull_failed", "reason": "denied"},
        {"stack": "media", "service": "bad", "status": "rolled_back", "reason": "unhealthy"},
    ]
    text = zoidberg.summarize_pass(results, manual=["network/traefik", "network/adguard"])
    assert "9 services checked" in text and "Updated (2):</b> media/radarr, media/sonarr" in text
    assert "Already on the latest:</b> 2" in text and "Rolled back (1)" in text and "media/bad" in text
    assert "Could not be updated automatically (4)" in text
    assert "media/unpackerr: no tag in the compose file" in text and "pinned by digest" in text
    assert "built locally" in text and "the pull failed" in text
    assert "Kept manual on purpose:</b> network/traefik, network/adguard" in text


def test_a_quiet_pass_still_says_so_and_a_huge_one_stays_under_the_telegram_limit():
    import casa_zoidberg as zoidberg
    quiet = zoidberg.summarize_pass([{"stack": "a", "service": "b", "status": "no_change"}])
    assert "nothing was newer" in quiet and "Could not be updated" not in quiet
    many = [{"stack": "s", "service": f"svc{i}", "status": "skipped",
             "reason": "x is not a canary-eligible image reference"} for i in range(400)]
    assert len(zoidberg.summarize_pass(many)) <= 3500


def test_service_names_are_escaped_for_telegram_html():
    import casa_zoidberg as zoidberg
    text = zoidberg.summarize_pass([{"stack": "a<b", "service": "c&d", "status": "updated", "reason": ""}])
    assert "a&lt;b/c&amp;d" in text


def test_the_pass_sends_the_digest_and_a_failing_send_never_fails_the_pass(monkeypatch):
    import casa_zoidberg as zoidberg
    sent = []

    class Tg:
        def send(self, text, **kw):
            sent.append(text)
            raise OSError("telegram down")
    # a pass with results sends a digest, and survives the send failing
    monkeypatch.setattr(zoidberg, "stack_services", lambda stack_dir: ["svc"])
    monkeypatch.setattr(zoidberg, "eligible_stacks", lambda: [Path("/stacks/media")])
    monkeypatch.setattr(zoidberg, "canary_update_service", lambda *a, **k: {"stack": "media", "service": "svc", "status": "no_change"})
    monkeypatch.setattr(zoidberg.time, "sleep", lambda s: None)
    monkeypatch.setattr(zoidberg, "kept_manual", lambda stacks: [])
    results = zoidberg.run_update_pass(tg=Tg(), commands=object())
    assert results[0]["status"] == "no_change" and sent and "Weekly update pass" in sent[0]


def test_hermes_no_longer_turns_image_age_into_findings():
    import casa_hermes
    assert "Image age is NOT a finding" in casa_hermes.SYSTEM_PROMPT
    slim = casa_hermes._slim_snapshot({"image_candidates": [{"repo": "redis", "tag": "latest", "stale_days": 42}]})
    assert "image_candidates" not in slim


def test_a_failed_rollback_is_urgent_never_reported_as_rolled_back():
    import casa_zoidberg as zoidberg
    text = zoidberg.summarize_pass([
        {"stack": "a", "service": "x", "status": "rollback_failed", "reason": "boom"},
        {"stack": "a", "service": "y", "status": "interrupted", "reason": "?"},
        {"stack": "a", "service": "z", "status": "rolled_back", "reason": "unhealthy"}])
    assert "Needs your attention now (2)" in text and "a/x: the rollback failed" in text and "a/y: the update did not finish" in text
    assert "Rolled back (1):</b> a/z" in text


def test_a_pass_that_checked_nothing_still_sends_its_digest(monkeypatch):
    import casa_zoidberg as zoidberg
    sent = []

    class Tg:
        def send(self, text, **kw):
            sent.append(text)
    monkeypatch.setattr(zoidberg, "eligible_stacks", list)
    monkeypatch.setattr(zoidberg, "kept_manual", lambda stacks: ["network/traefik"])
    assert zoidberg.run_update_pass(tg=Tg(), commands=object()) == []
    assert sent and "0 services checked" in sent[0] and "network/traefik" in sent[0]
