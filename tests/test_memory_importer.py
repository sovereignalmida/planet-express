"""planet_express/memory/importer.py: both formats, candidate-only, never overwrite confirmed, secrets,
idempotence, CLI. Standalone sqlite; nothing else from PE is needed."""

import json
import sqlite3
import subprocess
import sys

import pytest

from planet_express.memory import importer, store
from tests.memory_support import (
    FIXTURES,
    NOW,
    OPERATOR,
    ROOT,
    confirmed,
    make_card,
    make_conn,
)


@pytest.fixture
def conn():
    return make_conn()


def run(conn, path, **kw):
    return importer.import_path(conn, path, actor="importer:test", now=NOW, **kw)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


MD = """---
id: {id}
type: {type}
scope: ["stack:media"]
source: {source}
---
{body}
"""


def test_fixture_set_imports_cleanly_as_candidates(conn):
    report = run(conn, FIXTURES)
    assert report.rejected == []
    cards = store.list_cards(conn)
    assert len(cards) >= 12
    assert {c.status for c in cards} == {"candidate"}
    assert report.created == len(cards)


def test_importer_never_creates_a_confirmed_card_whatever_it_declares(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="operator-stated", body="Plain."))
    run(conn, tmp_path)
    assert store.get_card(conn, "a").status == "candidate"


def test_status_key_in_input_is_rejected_precisely(conn, tmp_path):
    path = write(tmp_path, "a.md", "---\nid: a\ntype: fact\nsource: inferred\nstatus: confirmed\n---\nBody.\n")
    report = run(conn, tmp_path)
    assert store.get_card(conn, "a") is None
    assert len(report.rejected) == 1
    assert str(path) in report.rejected[0].location and "status" in report.rejected[0].message


def test_observed_source_is_capped_at_operator_stated(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="observed", body="Plain."))
    run(conn, tmp_path)
    assert store.get_card(conn, "a").source == "operator-stated"
    assert "observed" in store.history(conn, "a")[0].detail  # the declared value is not lost


def test_lower_trust_sources_are_kept_as_declared(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="inferred", body="Plain."))
    write(tmp_path, "b.md", MD.format(id="b", type="fact", source="researched", body="Plain."))
    run(conn, tmp_path)
    assert (store.get_card(conn, "a").source, store.get_card(conn, "b").source) == ("inferred", "researched")


def test_markdown_front_matter_fields_and_steps_are_parsed(conn):
    run(conn, FIXTURES / "nfs")
    card = store.get_card(conn, "nfs-stale-recovery")
    assert card.type == "procedure" and card.scope == ["mount:/mnt/media", "stack:media"]
    assert next(s.check for s in card.steps) == "the backup status shows idle."
    assert len(card.steps) == 4
    assert card.statement.startswith("Stop consumers, remount")
    assert store.get_card(conn, "nfs-keep-until-migration").review_after.isoformat() == "2026-01-01"
    assert store.get_card(conn, "nfs-stale-handle-symptoms").evidence == ["incident-example-3"]


def test_jsonl_is_parsed(conn):
    run(conn, FIXTURES / "disk" / "disk-cards.jsonl")
    assert store.get_card(conn, "disk-never-prune-volumes").type == "constraint"
    assert store.get_card(conn, "disk-rollback-hold-decision").review_after.isoformat() == "2027-03-01"


@pytest.mark.parametrize("fmt", ["md", "jsonl"])
def test_cards_round_trip_through_both_formats(conn, tmp_path, fmt):
    original = make_card(
        "roundtrip", title="Round trip", type="procedure", scope=["mount:/mnt/x", "stack:y"],
        statement="Intro paragraph.\n\nSecond paragraph.", source="researched", evidence=["doc:a", "incident-1"],
        review_after="2027-02-03", supersedes=["older-card"], conflicts_with=["rival-card"],
        steps=[{"text": "First.", "check": "First done."}, {"text": "Second.", "check": "Second done."}])
    if fmt == "md":
        write(tmp_path, "c.md", importer.render_markdown(original))
    else:
        write(tmp_path, "c.jsonl", importer.render_jsonl(original) + "\n")
    report = run(conn, tmp_path)
    assert report.rejected == []
    got = store.get_card(conn, "roundtrip")
    for field in ("title", "type", "scope", "statement", "source", "evidence", "review_after", "supersedes",
                  "conflicts_with", "steps"):
        assert getattr(got, field) == getattr(original, field), field
    assert got.status == "candidate"


def test_plain_markdown_notes_alone_work(conn, tmp_path):
    """No front matter, no PE config beyond the sqlite file."""
    write(tmp_path, "Sunday Quiet Time.md", "# Sunday quiet time\n\nDo not restart stacks on Sunday evening.\n")
    report = run(conn, tmp_path)
    assert report.rejected == [] and report.created == 1
    card = store.get_card(conn, "sunday-quiet-time")
    assert card.title == "Sunday quiet time" and card.type == "fact" and card.status == "candidate"
    assert card.statement == "Do not restart stacks on Sunday evening."
    run(conn, FIXTURES / "notes")
    assert store.get_card(conn, "plain-note") is not None


def test_malformed_cards_are_rejected_with_precise_errors(conn, tmp_path):
    cases = {
        "badtype.md": ("---\nid: bt\ntype: rumour\nsource: inferred\n---\nBody.\n", "type"),
        "badid.md": ("---\nid: Bad Id\ntype: fact\nsource: inferred\n---\nBody.\n", "id"),
        "extra.md": ("---\nid: ex\ntype: fact\nsource: inferred\nmood: happy\n---\nBody.\n", "mood"),
        "badyaml.md": ("---\nid: [unclosed\n---\nBody.\n", "front matter"),
        "unterminated.md": ("---\nid: un\ntype: fact\nsource: inferred\nBody.\n", "front matter"),
        "nosteps.md": ("---\nid: ns\ntype: procedure\nsource: inferred\n---\nJust prose.\n", "steps"),
        "nocheck.md": ("---\nid: nc\ntype: procedure\nsource: inferred\n---\n1. Do it.\n", "check"),
        "empty.md": ("---\nid: em\ntype: fact\nsource: inferred\n---\n", "statement"),
        "toolong.md": ("---\nid: tl\ntype: fact\nsource: inferred\n---\n" + "x" * 5000 + "\n", "statement"),
        "baddate.md": ("---\nid: bd\ntype: fact\nsource: inferred\nreview_after: soon\n---\nBody.\n", "review_after"),
    }
    for name, (text, _) in cases.items():
        write(tmp_path, name, text)
    write(tmp_path, "lines.jsonl", '{"id": "ok-line", "type": "fact", "source": "inferred", "statement": "Fine."}\n'
                                   "not json\n"
                                   '{"id": "bad-line", "type": "fact", "source": "inferred"}\n')
    report = run(conn, tmp_path)
    by_name = {e.location.rsplit("/", 1)[-1].split(":")[0]: e for e in report.rejected}
    for name, (_, needle) in cases.items():
        assert needle in by_name[name].message, (name, by_name[name].message)
    assert {e.location.rsplit("/", 1)[-1] for e in report.rejected if "lines.jsonl" in e.location} == {
        "lines.jsonl:2", "lines.jsonl:3"}
    assert store.get_card(conn, "ok-line") is not None  # a bad line does not poison its neighbours
    assert len(store.list_cards(conn)) == 1


def test_duplicate_id_within_one_import_is_rejected(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="dup", type="fact", source="inferred", body="First."))
    write(tmp_path, "b.md", MD.format(id="dup", type="fact", source="inferred", body="Second."))
    report = run(conn, tmp_path)
    assert len(report.rejected) == 1 and "duplicate" in report.rejected[0].message.lower()


def test_reimport_is_idempotent(conn):
    first = run(conn, FIXTURES)
    snapshot = [(c.model_dump_json(), len(store.history(conn, c.id))) for c in store.list_cards(conn)]
    second = run(conn, FIXTURES)
    assert (second.created, second.updated, second.conflicts) == (0, 0, 0)
    assert second.unchanged == first.created
    assert snapshot == [(c.model_dump_json(), len(store.history(conn, c.id))) for c in store.list_cards(conn)]


def test_reimport_updates_a_changed_candidate(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="inferred", body="Version one."))
    run(conn, tmp_path)
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="inferred", body="Version two."))
    assert run(conn, tmp_path).updated == 1
    assert store.get_card(conn, "a").statement == "Version two."


def test_import_never_overwrites_a_confirmed_card(conn, tmp_path):
    confirmed(conn, "a", statement="Operator approved text.")
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="operator-stated", body="Different text."))
    report = run(conn, tmp_path)
    assert report.conflicts == 1 and report.created == 0
    original = store.get_card(conn, "a")
    assert original.status == "confirmed" and original.statement == "Operator approved text."
    conflict = [c for c in store.list_cards(conn) if c.id != "a"]
    assert len(conflict) == 1
    assert conflict[0].status == "candidate" and conflict[0].conflicts_with == ["a"]
    assert conflict[0].statement == "Different text."
    assert "a" in store.get_card(conn, "a").conflicts_with or conflict[0].id in original.conflicts_with
    # and re-importing the same file does not pile up more conflict cards
    assert run(conn, tmp_path).conflicts == 0
    assert len(store.list_cards(conn)) == 2


def test_identical_reimport_over_confirmed_card_is_unchanged(conn, tmp_path):
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="operator-stated", body="Same text."))
    run(conn, tmp_path)
    store.confirm(conn, "a", OPERATOR, now=NOW)
    report = run(conn, tmp_path)
    assert report.unchanged == 1 and report.conflicts == 0
    assert len(store.list_cards(conn)) == 1


def test_import_over_deprecated_card_also_creates_a_conflict_candidate(conn, tmp_path):
    confirmed(conn, "a", statement="Old.")
    store.deprecate(conn, "a", OPERATOR, now=NOW)
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="inferred", body="New."))
    assert run(conn, tmp_path).conflicts == 1
    assert store.get_card(conn, "a").status == "deprecated"


@pytest.mark.parametrize("secret", [
    "DB_PASSWORD=correct-horse-battery",
    "api_token: abcdef0123456789abcdef",
    "Authorization: Bearer abcdef0123456789",
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_card_with_planted_secret_is_rejected_and_not_stored(conn, tmp_path, secret):
    path = write(tmp_path, "bad.md", MD.format(id="leaky", type="fact", source="inferred",
                                               body=f"Harmless first line.\n{secret}\n"))
    write(tmp_path, "good.md", MD.format(id="fine", type="fact", source="inferred", body="Nothing secret."))
    report = run(conn, tmp_path)
    assert store.get_card(conn, "leaky") is None and store.get_card(conn, "fine") is not None
    assert len(report.rejected) == 1
    error = report.rejected[0]
    assert str(path) in error.location and "secret" in error.message.lower()
    assert secret not in error.message and secret not in error.location
    assert secret not in "".join(str(r) for r in conn.iterdump())


def test_dry_run_changes_nothing(conn, tmp_path):
    report = run(conn, FIXTURES, dry_run=True)
    assert report.created >= 12 and report.dry_run
    assert store.list_cards(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] == 0


def test_incident_and_gap_directories_are_not_card_formats(conn, tmp_path):
    write(tmp_path, "incidents/one.jsonl", '{"symptoms": "x", "root_cause": "y"}\n')
    write(tmp_path, "gaps/one.jsonl", '{"task": "x"}\n')
    report = run(conn, tmp_path)
    assert report.rejected == [] and report.created == 0 and report.skipped == 2


def test_cli_imports_into_a_fresh_database(tmp_path):
    db = tmp_path / "memory.db"
    proc = subprocess.run(
        [sys.executable, "-m", "planet_express.memory.importer", str(FIXTURES), "--db", str(db)],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "created" in proc.stdout
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM memory_cards WHERE status = 'candidate'").fetchone()[0]
    assert count >= 12


def test_cli_dry_run_does_not_create_the_database(tmp_path):
    db = tmp_path / "memory.db"
    proc = subprocess.run(
        [sys.executable, "-m", "planet_express.memory.importer", str(FIXTURES), "--db", str(db), "--dry-run"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert not db.exists() and "dry run" in proc.stdout.lower()


def test_cli_exit_code_is_nonzero_when_something_is_rejected(tmp_path):
    write(tmp_path / "notes", "bad.md", "---\nid: Bad Id\ntype: fact\nsource: inferred\n---\nBody.\n")
    proc = subprocess.run(
        [sys.executable, "-m", "planet_express.memory.importer", str(tmp_path / "notes"), "--db",
         str(tmp_path / "m.db")], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 1 and "bad.md" in proc.stderr


def test_cli_works_without_any_pe_config(tmp_path):
    """A user with only Markdown notes has no /etc/planetexpress/config.yaml."""
    import os
    notes = tmp_path / "notes"
    write(notes, "n.md", "# Note\n\nSomething worth remembering.\n")
    env = {k: v for k, v in os.environ.items() if k != "CASA_CONFIG"}
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "planet_express.memory.importer", str(notes), "--db", str(tmp_path / "m.db")],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.dumps(proc.stdout)  # printed a summary
    assert "site-specific" in proc.stderr  # screening ran shape-only, and the operator is told


def test_render_markdown_is_loadable_yaml_front_matter():
    text = importer.render_markdown(make_card("r1", statement="Body text."))
    assert text.startswith("---\n") and "id: r1" in text and text.rstrip().endswith("Body text.")


def test_secret_in_confidence_note_is_rejected_and_nowhere_in_the_db(conn, tmp_path):
    secret = "PASSWORD=correct-horse-battery-staple"
    write(tmp_path, "a.md", f"---\nid: a\ntype: fact\nsource: inferred\nconfidence_note: \"{secret}\"\n---\nBody.\n")
    report = run(conn, tmp_path)
    assert store.get_card(conn, "a") is None
    assert len(report.rejected) == 1 and "confidence_note" in report.rejected[0].message
    assert "correct-horse" not in report.rejected[0].message
    assert "correct-horse" not in "".join(conn.iterdump())


def test_secret_in_evidence_is_rejected_and_nowhere_in_the_db(conn, tmp_path):
    write(tmp_path, "a.jsonl", json.dumps({
        "id": "a", "type": "fact", "source": "inferred", "statement": "Body.",
        "evidence": ["api_token: abcdef0123456789abcdef"]}) + "\n")
    report = run(conn, tmp_path)
    assert len(report.rejected) == 1
    assert "abcdef0123456789" not in "".join(conn.iterdump())


def test_conflict_candidate_that_would_exceed_the_bound_is_rejected_not_stored(conn, tmp_path):
    others = [f"other-{i}" for i in range(16)]
    confirmed(conn, "a", statement="Approved.", conflicts_with=others)
    write(tmp_path, "a.jsonl", json.dumps({
        "id": "a", "type": "fact", "source": "inferred", "statement": "Different.",
        "conflicts_with": others}) + "\n")
    report = run(conn, tmp_path)
    assert len(report.rejected) == 1 and "conflicts_with" in report.rejected[0].message
    assert report.conflicts == 0
    assert [c.id for c in store.list_cards(conn)] == ["a"]  # still readable, nothing invalid written


def test_reimport_over_a_resolved_conflict_candidate_does_not_abort_the_batch(conn, tmp_path):
    confirmed(conn, "a", statement="Approved.")
    write(tmp_path, "a.md", MD.format(id="a", type="fact", source="inferred", body="Different."))
    assert run(conn, tmp_path).conflicts == 1
    cid = next(c.id for c in store.list_cards(conn) if c.id != "a")
    store.confirm(conn, cid, OPERATOR, now=NOW)  # the operator accepted the conflict candidate
    write(tmp_path, "good.md", MD.format(id="good", type="fact", source="inferred", body="Fresh."))
    report = run(conn, tmp_path)
    assert report.rejected == [] and report.skipped == 1 and report.created == 1
    assert store.get_card(conn, "good") is not None


def test_cli_does_not_traceback_on_a_resolved_conflict_candidate(tmp_path):
    from planet_express.memory import schema
    notes, db = tmp_path / "notes", tmp_path / "m.db"
    write(notes, "a.md", MD.format(id="a", type="fact", source="inferred", body="Different."))
    setup = sqlite3.connect(db)
    schema.init_schema(setup)
    store.upsert_candidate(setup, make_card("a", statement="Approved.", source="inferred"), OPERATOR, now=NOW)
    store.confirm(setup, "a", OPERATOR, now=NOW)
    setup.commit()
    assert importer.main([str(notes), "--db", str(db)]) == 0   # creates the conflict candidate
    cid = next(c.id for c in store.list_cards(setup) if c.id != "a")
    store.confirm(setup, cid, OPERATOR, now=NOW)
    setup.commit()
    assert importer.main([str(notes), "--db", str(db)]) == 0
    setup.close()


def test_jsonl_with_unicode_line_separators_is_one_record(conn, tmp_path):
    card = make_card("sep", statement="one\u2028two\u2029three\x85four")
    line = importer.render_jsonl(card)
    assert line.isascii() and "\n" not in line
    write(tmp_path, "c.jsonl", line + "\r\n")
    assert run(conn, tmp_path).rejected == []
    assert store.get_card(conn, "sep").statement == card.statement


def test_raw_unicode_separators_inside_a_json_string_do_not_split_the_record(conn, tmp_path):
    raw = '{"id": "raw", "type": "fact", "source": "inferred", "statement": "a\u2028b\u0085c"}'
    write(tmp_path, "c.jsonl", raw + "\n")
    report = run(conn, tmp_path)
    assert report.rejected == [] and store.get_card(conn, "raw").statement == "a\u2028b\u0085c"
