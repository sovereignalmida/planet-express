"""`compose.write` and its inverse on the engine (slice 5b-4, design §4.6): the step, its refusals,
what it records, and the rollback built from that record."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import config
from planet_express.core.store import Store
from planet_express.execution import compose_files as cf, engine, runbook as runbooks

OLD = "services:\n  web:\n    image: nginx:1.27\n"
NEW = "services:\n  web:\n    image: nginx:1.28\n"


class Svc:
    def __init__(self, tmp_path):
        self._store = Store(tmp_path / "db.sqlite", clock=lambda: 1_000_000.0)
        self._store.init()
        self._clock = lambda: 1_000_000.0
        self.argv = []

    def _run_argv(self, argv, timeout=None):
        self.argv.append(argv)
        return 0, "", ""


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    root.mkdir()
    monkeypatch.setattr(config, "STACKS_ROOT", root)
    return root


@pytest.fixture
def svc(tmp_path):
    return Svc(tmp_path)


def write_step(root, stack="media", *, content=NEW, expected=None, absent=False):
    params = {"stack": stack, "content_sha256": cf.sha256_text(content)}
    if absent:
        params["expected_absent"] = True
    else:
        params["expected_old_sha256"] = expected
    binding = {"compose_path": str(cf.compose_path_for(root, stack)), "project": stack,
               "directory_existed": (root / stack).exists()}
    return {"type": "compose.write", "params": params, "binding": binding}


def runbook(root, *steps, content=NEW):
    return runbooks.Runbook.model_validate(
        {"title": "Edit media", "steps": list(steps), "artifacts": {cf.sha256_text(content): content}})


def execution(svc):
    approval, _ = svc._store.propose(
        action="docker.restart_service", target_key="media", risk="R3",
        target={"stack": "media", "service": "web", "container": "c"},
        requested_via="telegram", requested_by="t")
    return svc._store.approve_and_create_execution(approval["id"], decided_by="t", arrived_at=1)["id"]


def run(svc, plan):
    ex = execution(svc)
    result = engine.RunbookEngine(svc).run(ex, plan, origin="telegram")
    return ex, result, svc._store.list_steps(ex)[0]


def stack_file(root, stack="media", content=OLD):
    (root / stack).mkdir(exist_ok=True)
    path = cf.compose_path_for(root, stack)
    path.write_text(content)
    return path


def test_an_edit_is_written_recorded_and_reported(host, svc):
    path = stack_file(host)
    _ex, result, step = run(svc, runbook(host, write_step(host, expected=cf.sha256_text(OLD))))
    assert result.status == "passed"
    assert path.read_text() == NEW
    assert (step["status"], step["effect"]) == ("passed", "applied")
    assert step["output"] == {"compose_path": str(path), "compose_sha256": cf.sha256_text(NEW)}
    pre = step["pre_state"]
    assert pre["previous_sha256"] == cf.sha256_text(OLD)
    assert Path(pre["backup_path"]).read_text() == OLD
    assert pre["created_file"] is False


def test_a_new_stack_is_created_and_recorded_as_created(host, svc):
    _ex, result, step = run(svc, runbook(host, write_step(host, "brand-new", absent=True)))
    assert result.status == "passed"
    assert cf.compose_path_for(host, "brand-new").read_text() == NEW
    assert step["pre_state"]["created_file"] and step["pre_state"]["created_directory"]
    assert step["pre_state"]["backup_path"] is None


def test_a_file_changed_since_approval_fails_without_writing(host, svc):
    path = stack_file(host, content="someone else was here\n")
    _ex, result, step = run(svc, runbook(host, write_step(host, expected=cf.sha256_text(OLD))))
    assert result.status == "failed"
    assert (step["status"], step["effect"]) == ("failed", "not_applied")
    assert "changed since" in step["reason"]
    assert path.read_text() == "someone else was here\n"


def test_a_binding_that_points_at_another_stack_is_refused(host, svc):
    stack_file(host)
    step = write_step(host, expected=cf.sha256_text(OLD))
    step["binding"]["compose_path"] = str(cf.compose_path_for(host, "other"))
    with pytest.raises(ValueError):       # the document itself refuses it (binding vs params)
        runbook(host, step)


def test_a_symlinked_stack_directory_is_refused(host, svc, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (host / "media").symlink_to(elsewhere)
    _ex, result, step = run(svc, runbook(host, write_step(host, absent=True)))
    assert result.status == "failed" and "symlink" in step["reason"]
    assert not (elsewhere / cf.COMPOSE_FILENAME).exists()


def test_a_missing_artifact_is_refused_before_any_write(host, svc):
    stack_file(host)
    step = write_step(host, expected=cf.sha256_text(OLD))
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [step], "artifacts": {cf.sha256_text(NEW): NEW}})
    object.__setattr__(plan, "artifacts", {})      # as if the stored document lost its artifact
    _ex, result, _step = run(svc, plan)
    assert result.status == "failed" and "missing" in result.reason
    assert cf.compose_path_for(host, "media").read_text() == OLD


# ── the inverse ─────────────────────────────────────────────────────────────────
def rollback_of(svc, ex):
    rows = svc._store.list_steps(ex)
    return engine.plan_rollback(svc, "Edit media", rows)


def test_rolling_back_an_edit_restores_the_previous_content(host, svc):
    path = stack_file(host)
    ex, _result, _step = run(svc, runbook(host, write_step(host, expected=cf.sha256_text(OLD))))
    plan = rollback_of(svc, ex)
    assert [s.type for s in plan.runbook.steps] == ["compose.restore"]
    child = execution(svc)
    assert engine.RunbookEngine(svc).run(child, plan.runbook, origin="telegram").status == "passed"
    assert path.read_text() == OLD


def test_rolling_back_a_new_stack_removes_what_it_created(host, svc):
    ex, _result, _step = run(svc, runbook(host, write_step(host, "brand-new", absent=True)))
    plan = rollback_of(svc, ex)
    child = execution(svc)
    assert engine.RunbookEngine(svc).run(child, plan.runbook, origin="telegram").status == "passed"
    assert not cf.compose_path_for(host, "brand-new").exists()
    assert not (host / "brand-new").exists()


def test_a_file_edited_by_hand_since_the_write_is_never_reverted(host, svc):
    path = stack_file(host)
    ex, _result, _step = run(svc, runbook(host, write_step(host, expected=cf.sha256_text(OLD))))
    path.write_text(NEW + "# a human has been here\n")
    plan = rollback_of(svc, ex)
    child = execution(svc)
    result = engine.RunbookEngine(svc).run(child, plan.runbook, origin="telegram")
    assert result.status == "failed" and "changed since the step wrote it" in result.reason
    assert "a human has been here" in path.read_text()


def test_an_unknown_write_is_settled_from_what_is_on_disk(host, svc):
    path = stack_file(host)
    ex, _result, _step = run(svc, runbook(host, write_step(host, expected=cf.sha256_text(OLD))))
    rows = svc._store.list_steps(ex)
    rows[0]["effect"] = "unknown"
    assert engine._now_effect(svc, rows[0]) == "applied"        # the new content is there
    path.write_text(OLD)
    assert engine._now_effect(svc, rows[0]) == "not_applied"    # the old content is back
    path.write_text("something else entirely\n")
    assert engine._now_effect(svc, rows[0]) == "unknown"


def test_a_compose_write_is_not_counted_by_the_attempt_limits(host, svc, monkeypatch):
    """T24's cooldown damps an agent repeating a mutation; a compare-and-swap write is neither
    repeatable (its expectation no longer holds) nor automatic, and counting it only blocked a
    second legitimate edit of the same stack (found in the T43 VM rehearsal)."""
    import config as config_module
    from config_schema import AutonomyConfig
    monkeypatch.setattr(config_module, "AUTONOMY",
                        AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=1))
    stack_file(host)
    first = runbook(host, write_step(host, expected=cf.sha256_text(OLD)))
    _ex, result, _step = run(svc, first)
    assert result.status == "passed"

    # a second, different edit of the same stack, minutes later, still runs
    newer = NEW + "# later\n"
    second = runbooks.Runbook.model_validate({
        "title": "Edit again",
        "steps": [write_step(host, content=newer, expected=cf.sha256_text(NEW))],
        "artifacts": {cf.sha256_text(newer): newer}})
    _ex, result, _step = run(svc, second)
    assert result.status == "passed"
    assert cf.compose_path_for(host, "media").read_text() == newer
    with svc._store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
