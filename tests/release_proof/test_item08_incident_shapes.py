"""Release proof item 8:

    Fixture evaluations of three historical incident shapes: relevant memory retrieved, fact and unknown separated, valid
    structured output, no mutation under Observe/Propose.

The shapes are synthetic and anonymised (tests/fixtures/v4/incidents/*.json); the memory corpus is tests/fixtures/v4/memory.
The agent is a scripted fake that returns the fixture's result, so what is evaluated is Core: retrieval into the brief,
the strict output model, the stored result, and the absence of any mutation path for these tiers."""
import json
import sqlite3
from pathlib import Path

import pytest
from memory_support import NOW, OPERATOR
from supervisor_fakes import Rig, update

from planet_express.memory import importer, retrieval, store as memory_store
from planet_express.missions.brief import EvidenceItem
from planet_express.missions.runtime import AgentResult
from release_proof.support import FIXTURES

INCIDENTS = sorted((FIXTURES / "incidents").glob("*.json"))
MEMORY = FIXTURES / "memory"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_there_are_three_incident_shapes():
    assert [p.stem for p in INCIDENTS] == ["disk", "netns", "nfs"]


def run_incident(tmp_path, incident, tier):
    holder = {}

    def memory(mission):
        with holder["rig"].store._connect() as conn:
            return list(retrieval.retrieve(conn, incident["scope_tags"], incident["query"]).cards)

    rig = Rig(tmp_path, tier=tier, memory_provider=memory,
              evidence_provider=lambda m: [EvidenceItem(e["source"], e["text"]) for e in incident["evidence"]],
              behaviours=[{"script": [update(), update("finished")], "result": incident["agent_result"]}])
    holder["rig"] = rig
    with rig.store._connect() as conn:
        report = importer.import_path(conn, MEMORY, actor="importer:test", now=NOW)
        assert not report.rejected
    with rig.store._write() as conn:
        for card in memory_store.list_cards(conn):
            memory_store.confirm(conn, card.id, OPERATOR, now=NOW)
    rig.sup.start(rig.mid)
    for _ in range(4):
        rig.sup.poll(rig.mid)
    return rig


def stored_result(rig):
    with sqlite3.connect(rig.store.path) as conn:
        rows = conn.execute("SELECT path FROM mission_artifacts WHERE mission_id=? AND name='result.json'",
                            (rig.mid,)).fetchall()
    assert len(rows) == 1
    return json.loads(Path(rows[0][0]).read_text())


@pytest.mark.parametrize("path", INCIDENTS, ids=lambda p: p.stem)
@pytest.mark.parametrize("tier", ["observe", "propose"])
def test_an_incident_shape_is_evaluated_end_to_end(tmp_path, path, tier):
    incident = load(path)
    rig = run_incident(tmp_path, incident, tier)
    brief = rig.runtimes[0].context.brief

    # relevant memory retrieved, and only as quoted data
    for card_id in incident["expect_cards"]:
        assert card_id in brief, card_id
    assert "Current host observations outrank memory cards" in brief
    for source in (e["source"] for e in incident["evidence"]):
        assert source in brief

    # valid structured output, stored as Core received it
    assert rig.status() == "awaiting_decision"
    result = AgentResult.model_validate(stored_result(rig))
    assert result.diagnosis and result.confidence in ("low", "medium", "high")

    # fact and unknown are separate lists, never overlapping, and the unknowns are not asserted as facts
    assert result.evidence and result.unknowns
    assert not set(result.evidence) & set(result.unknowns)
    assert not any(u in result.diagnosis for u in result.unknowns)

    # no mutation under Observe/Propose
    assert not any('"executing"' in b for b in rig.state_path())
    mission = rig.ms.get_mission(rig.mid)
    assert mission.tier == tier and mission.checkpoint_id is None and not mission.outcome_flags
    spawned = [s["argv"] for g in rig.cont._groups.values() for s in g.spawns] or [["/usr/bin/fake-agent"]]
    assert all(argv[0] == "/usr/bin/fake-agent" for argv in spawned)


def test_a_result_that_blurs_the_output_contract_is_rejected_not_stored(tmp_path):
    incident = load(INCIDENTS[0])
    bad = {**incident["agent_result"], "root_cause_is_certain": True}              # an unknown key
    rig = run_incident(tmp_path, {**incident, "agent_result": bad}, "observe")
    assert rig.status() == "needs_operator"
    assert "result.json" not in rig.dump()


def test_a_result_with_an_unsupported_confidence_is_rejected(tmp_path):
    incident = load(INCIDENTS[0])
    rig = run_incident(tmp_path, {**incident, "agent_result": {**incident["agent_result"], "confidence": "certain"}},
                       "propose")
    assert rig.status() == "needs_operator"


def test_unrelated_memory_is_not_retrieved_for_an_incident(tmp_path):
    incident = load(INCIDENTS[0])                      # disk
    rig = run_incident(tmp_path, incident, "observe")
    brief = rig.runtimes[0].context.brief
    assert "nfs-stale-recovery" not in brief and "netns-recreate-dependents" not in brief
