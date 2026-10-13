"""planet_express/memory/models.py: strict card model, size bounds, actor format."""

import pytest
from pydantic import ValidationError

from planet_express.memory.models import Card, parse_actor
from tests.memory_support import make_card


def test_valid_card_defaults_to_candidate():
    card = make_card()
    assert card.status == "candidate"
    assert card.supersedes == [] and card.conflicts_with == []


@pytest.mark.parametrize("bad_id", ["", "Has Space", "UPPER", "-lead", "a" * 129, "slash/inside"])
def test_id_must_be_a_slug(bad_id):
    with pytest.raises(ValidationError):
        make_card(bad_id)


@pytest.mark.parametrize("field,value", [
    ("type", "fragility"), ("source", "guess"), ("status", "approved"),
])
def test_enums_are_closed(field, value):
    with pytest.raises(ValidationError):
        make_card(**{field: value})


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError) as err:
        Card(**{**make_card().model_dump(), "confidence": 0.9})
    assert "confidence" in str(err.value)


def test_statement_is_bounded_to_4_kib():
    make_card(statement="x" * 4096)
    with pytest.raises(ValidationError):
        make_card(statement="x" * 4097)


def test_statement_bound_is_in_bytes_not_characters():
    with pytest.raises(ValidationError):
        make_card(statement="é" * 2049)  # 4098 bytes


def test_empty_statement_rejected():
    with pytest.raises(ValidationError):
        make_card(statement="   ")


def test_procedure_needs_steps_with_checks_and_at_most_20():
    with pytest.raises(ValidationError):
        make_card(type="procedure", steps=[])
    with pytest.raises(ValidationError):
        make_card(type="procedure", steps=[{"text": "Do it.", "check": ""}])
    ok = [{"text": f"Step {i}.", "check": f"Check {i}."} for i in range(20)]
    assert len(make_card(type="procedure", steps=ok).steps) == 20
    with pytest.raises(ValidationError):
        make_card(type="procedure", steps=ok + [{"text": "One more.", "check": "Too many."}])


def test_only_procedures_may_have_steps():
    with pytest.raises(ValidationError):
        make_card(type="fact", steps=[{"text": "Do it.", "check": "Done."}])


def test_scope_tags_are_namespaced_and_bounded():
    assert make_card(scope=["mount:/x", "stack:media"]).scope == ["mount:/x", "stack:media"]
    with pytest.raises(ValidationError):
        make_card(scope=["nonamespace"])
    with pytest.raises(ValidationError):
        make_card(scope=[f"tag:{i}" for i in range(17)])


def test_naive_datetimes_are_treated_as_utc():
    from datetime import datetime
    card = make_card(created_at=datetime(2026, 1, 1, 0, 0))  # noqa: DTZ001 -- naive on purpose
    assert card.created_at.utcoffset().total_seconds() == 0


def test_card_cannot_supersede_itself():
    with pytest.raises(ValidationError):
        make_card(supersedes=["card-one"])


@pytest.mark.parametrize("actor", ["operator:alice", "runtime:s-1", "system:scan", "importer:cli"])
def test_actor_kinds(actor):
    assert parse_actor(actor)[0] == actor.split(":")[0]


@pytest.mark.parametrize("actor", ["", "alice", "root:alice", "operator:", "operator:a b", "runtime:" + "x" * 80])
def test_bad_actors_are_rejected(actor):
    with pytest.raises(ValueError):
        parse_actor(actor)
