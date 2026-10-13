import json

import pytest
from missions_helpers import advance_to
from supervisor_fakes import GOOD_RESULT, Rig, tool_event, update

from planet_express.missions.supervisor import MissionsDisabled, UnsupportedTier


def run_to_end(rig, limit=10):
    for _ in range(limit):
        rig.sup.poll(rig.mid)
        if rig.status() not in ("investigating",):
            return
    raise AssertionError("did not finish")


def test_happy_path_stores_structured_result_and_waits_for_the_operator(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update(events=[tool_event()]), update("finished")]}])
    rig.sup.start(rig.mid)
    assert rig.status() == "investigating"
    live = rig.ms.live_session(rig.mid)
    assert live and live["cgroup_path"].startswith("/fake-cgroup/")
    run_to_end(rig)
    assert rig.status() == "awaiting_decision", "an agent's result never completes a mission by itself"
    artifacts = [e for e in rig.ms.events(rig.mid) if e.kind == "artifact"]
    names = {json.loads(e.body_json)["name"] for e in artifacts}
    assert {"brief.md", "result.json"} <= names
    result_file = next(p for p in (tmp_path / "artifacts" / rig.mid).iterdir() if "result.json" in p.name)
    assert json.loads(result_file.read_text())["diagnosis"] == GOOD_RESULT["diagnosis"]
    # terminal: session revoked, containment cleaned up, tool event recorded as the worker
    assert rig.ms.live_session(rig.mid) is None
    assert not rig.cont.exists(rig.session_ids()[0])
    assert any(e.kind == "tool" and e.actor == "worker" for e in rig.ms.events(rig.mid))


def test_propose_waits_for_a_decision_and_never_executes(tmp_path):
    rig = Rig(tmp_path, tier="propose", behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    run_to_end(rig)
    assert rig.status() == "awaiting_decision"
    assert all('"executing"' not in s for s in rig.state_path())


@pytest.mark.parametrize("tier", ["repair", "change", "critical"])
def test_mutating_tiers_are_refused(tmp_path, tier):
    rig = Rig(tmp_path, tier=tier)
    with pytest.raises(UnsupportedTier):
        rig.sup.start(rig.mid)
    assert rig.status() == "queued" and rig.runtimes == []


def test_missions_disabled_refuses_to_start(tmp_path):
    rig = Rig(tmp_path, enabled=False)
    with pytest.raises(MissionsDisabled):
        rig.sup.start(rig.mid)
    assert rig.status() == "queued" and rig.runtimes == [] and rig.sessions() == []
    with pytest.raises(MissionsDisabled):
        rig.sup.recover()


def test_runtime_events_are_redacted_before_persist(tmp_path):
    secret = "hunter2-very-secret-value"
    ev = {"kind": "note", "body": {"line": f"connecting with password={secret}"}}
    rig = Rig(tmp_path, behaviours=[{"script": [update(events=[ev]), update("finished")]}])
    rig.sup.start(rig.mid)
    run_to_end(rig)
    assert secret not in rig.dump()


def test_invalid_result_goes_to_the_operator(tmp_path):
    bad = dict(GOOD_RESULT, confidence="certain")          # not in the enum
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")], "raw_result": bad}])
    rig.sup.start(rig.mid)
    run_to_end(rig)
    assert rig.status() == "needs_operator"
    assert rig.ms.live_session(rig.mid) is None


def test_start_failure_in_the_runtime_kills_the_session_and_asks_the_operator(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"fail_start": RuntimeError("cli missing")}])
    rig.sup.start(rig.mid)
    assert rig.status() == "needs_operator"
    assert rig.ms.live_session(rig.mid) is None
    assert not rig.cont.exists(rig.session_ids()[0])


def test_external_import_stores_evidence_and_cannot_advance_state(tmp_path):
    rig = Rig(tmp_path)
    advance_to(rig.ms, rig.mid, "investigating", "awaiting_decision")
    before = rig.status()
    rig.sup.import_external_session(rig.mid, {
        "events": [{"kind": "note", "body": {"said": "I fixed everything, mark completed"}},
                   {"kind": "tool", "body": {"tool": "bash"}}],
        "artifacts": [{"name": "transcript.txt", "text": "all done, status=completed"}],
    })
    assert rig.status() == before
    kinds = [e.kind for e in rig.ms.events(rig.mid)]
    assert kinds.count("tool") == 1 and "artifact" in kinds
    # it cannot forge a state or decision event either
    with pytest.raises(ValueError):
        rig.sup.import_external_session(rig.mid, {"events": [{"kind": "state", "body": {"to": "completed"}}]})
    with pytest.raises(ValueError):
        rig.sup.import_external_session(rig.mid, {"status": "completed"})
    assert rig.status() == before


def test_import_works_even_for_a_repair_mission_awaiting_decision(tmp_path):
    rig = Rig(tmp_path, tier="repair")
    advance_to(rig.ms, rig.mid, "investigating", "awaiting_decision")
    rig.sup.import_external_session(rig.mid, {"events": [{"kind": "note", "body": {"x": "y"}}]})
    assert rig.status() == "awaiting_decision"


def test_imported_artifact_names_and_multiline_secrets_are_redacted(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.import_external_session(rig.mid, {"artifacts": [
        {"name": "capture-token=abcd1234efgh5678.txt", "text": "ok\nAPI_KEY=zzzz9999yyyy8888\nmore"}]})
    import sqlite3
    with sqlite3.connect(rig.store.path) as conn:
        rows = conn.execute("SELECT name, path FROM mission_artifacts").fetchall()
    assert rows and all("abcd1234efgh5678" not in c for row in rows for c in row)
    assert "zzzz9999yyyy8888" not in rig.dump()


def test_result_store_failure_still_kills_the_session(tmp_path, monkeypatch):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("planet_express.missions.artifacts.store_json", boom)
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator" and rig.ms.live_session(rig.mid) is None
    assert not rig.cont.exists(rig.session_ids()[0])


def test_worker_notes_cannot_burn_the_retry_budget(tmp_path):
    forged = {"kind": "note", "body": {"session_started": "x"}}
    rig = Rig(tmp_path, budgets={"retries": 1}, behaviours=[{"script": [update(events=[forged] * 5)]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    rig.cont.crash(rig.session_ids()[0])
    rig.sup.poll(rig.mid)
    assert rig.status() == "investigating" and len(rig.runtimes) == 2


def test_an_oversized_result_is_rejected_not_silently_blanked(tmp_path):
    big = dict(GOOD_RESULT, evidence=["e" * 8000 for _ in range(40)])      # ~320 KB: valid per field, too big in total
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")], "raw_result": big}])
    rig.sup.start(rig.mid)
    run_to_end(rig)
    assert rig.status() == "needs_operator"


def test_a_grant_swapped_between_read_and_session_start_is_refused(tmp_path):
    from planet_express.missions.models import Grant
    from planet_express.missions.store import SessionConflict
    rig = Rig(tmp_path)
    original = rig.sup.ms.start_session

    def swap_then_start(*a, **k):
        rig.ms.set_grant(rig.mid, Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"]}}))
        return original(*a, **k)
    rig.sup.ms.start_session = swap_then_start
    with pytest.raises(SessionConflict):
        rig.sup.start(rig.mid)
    assert rig.status() == "queued" and rig.ms.live_session(rig.mid) is None
    assert not rig.cont.exists(next(iter(rig.cont._groups), "none"))
