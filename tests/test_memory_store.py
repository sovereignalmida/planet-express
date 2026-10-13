"""planet_express/memory/store.py: status machine, actor rules, history, supersession, review."""

from datetime import timedelta

import pytest

from planet_express.memory import store
from tests.memory_support import (
    NOW,
    OPERATOR,
    RUNTIME,
    SYSTEM,
    confirmed,
    make_card,
    make_conn,
    propose,
)


@pytest.fixture
def conn():
    return make_conn()


def actions(conn, card_id):
    return [(h.action, h.actor, h.from_status, h.to_status) for h in store.history(conn, card_id)]


def test_upsert_creates_candidate_and_records_actor(conn):
    propose(conn, "c1", actor=RUNTIME, source="inferred")
    card = store.get_card(conn, "c1")
    assert card.status == "candidate"
    assert actions(conn, "c1") == [("create", RUNTIME, None, "candidate")]


def test_upsert_forces_candidate_even_if_card_claims_confirmed(conn):
    store.upsert_candidate(conn, make_card("c1", status="confirmed"), OPERATOR, now=NOW)
    assert store.get_card(conn, "c1").status == "candidate"


def test_upsert_updates_a_candidate_and_logs_it(conn):
    propose(conn, "c1")
    store.upsert_candidate(conn, make_card("c1", statement="Changed."), OPERATOR, now=NOW)
    assert store.get_card(conn, "c1").statement == "Changed."
    assert [a[0] for a in actions(conn, "c1")] == ["create", "update"]


def test_upsert_identical_candidate_is_a_noop(conn):
    propose(conn, "c1")
    assert store.upsert_candidate(conn, make_card("c1"), OPERATOR, now=NOW) == "unchanged"
    assert len(actions(conn, "c1")) == 1


def test_upsert_refuses_to_overwrite_confirmed_or_deprecated(conn):
    confirmed(conn, "c1")
    with pytest.raises(store.CardExistsError):
        store.upsert_candidate(conn, make_card("c1", statement="Evil.", source="inferred"), RUNTIME, now=NOW)
    store.deprecate(conn, "c1", OPERATOR, now=NOW)
    with pytest.raises(store.CardExistsError):
        store.upsert_candidate(conn, make_card("c1", statement="Evil.", source="inferred"), RUNTIME, now=NOW)
    assert store.get_card(conn, "c1").statement != "Evil."


@pytest.mark.parametrize("actor", [RUNTIME, SYSTEM])
def test_runtime_and_system_cannot_claim_operator_stated(conn, actor):
    with pytest.raises(store.ActorNotPermitted):
        store.upsert_candidate(conn, make_card("c1", source="operator-stated"), actor, now=NOW)


@pytest.mark.parametrize("card_type", ["procedure", "constraint"])
@pytest.mark.parametrize("actor", [RUNTIME, SYSTEM, "importer:cli"])
def test_non_operator_cannot_confirm_procedure_or_constraint(conn, card_type, actor):
    propose(conn, "c1", type=card_type)
    with pytest.raises(store.ActorNotPermitted):
        store.confirm(conn, "c1", actor, now=NOW)
    assert store.get_card(conn, "c1").status == "candidate"


def test_runtime_can_never_confirm_anything(conn):
    for card_type in ("fact", "decision"):
        propose(conn, f"c-{card_type}", type=card_type, actor=RUNTIME, source="inferred")
        with pytest.raises(store.ActorNotPermitted):
            store.confirm(conn, f"c-{card_type}", RUNTIME, now=NOW)


@pytest.mark.parametrize("card_type", ["fact", "procedure", "constraint", "decision"])
def test_operator_can_confirm_every_type(conn, card_type):
    propose(conn, "c1", type=card_type)
    card = store.confirm(conn, "c1", OPERATOR, now=NOW)
    assert card.status == "confirmed" and card.last_verified_at == NOW
    assert actions(conn, "c1")[-1] == ("confirm", OPERATOR, "candidate", "confirmed")


def test_confirm_requires_a_candidate(conn):
    confirmed(conn, "c1")
    with pytest.raises(store.InvalidTransition):
        store.confirm(conn, "c1", OPERATOR, now=NOW)
    with pytest.raises(store.UnknownCard):
        store.confirm(conn, "missing", OPERATOR, now=NOW)


def test_deprecated_cards_cannot_be_reconfirmed(conn):
    confirmed(conn, "c1")
    store.deprecate(conn, "c1", OPERATOR, reason="obsolete", now=NOW)
    with pytest.raises(store.InvalidTransition):
        store.confirm(conn, "c1", OPERATOR, now=NOW)
    assert store.get_card(conn, "c1").status == "deprecated"


def test_every_transition_appends_history_with_actor(conn):
    propose(conn, "c1", actor="importer:cli")
    store.confirm(conn, "c1", OPERATOR, now=NOW)
    store.deprecate(conn, "c1", "system:scan", reason="gone", now=NOW)
    assert actions(conn, "c1") == [
        ("create", "importer:cli", None, "candidate"),
        ("confirm", OPERATOR, "candidate", "confirmed"),
        ("deprecate", "system:scan", "confirmed", "deprecated"),
    ]
    assert store.history(conn, "c1")[-1].detail == "gone"


def test_history_is_append_only_at_the_database_level(conn):
    propose(conn, "c1")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE memory_history SET actor = 'operator:mallory'")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM memory_history")


def test_supersession_is_inert_until_the_superseder_is_confirmed(conn):
    confirmed(conn, "old")
    propose(conn, "new", actor=RUNTIME, source="inferred")
    store.supersede(conn, "new", "old", RUNTIME, now=NOW)
    assert store.get_card(conn, "old").status == "confirmed"  # a candidate cannot retire a confirmed card
    store.confirm(conn, "new", OPERATOR, now=NOW)
    assert store.get_card(conn, "old").status == "deprecated"
    assert store.get_card(conn, "new").supersedes == ["old"]
    assert ("supersede", OPERATOR) == actions(conn, "old")[-1][:2]


def test_supersede_when_superseder_already_confirmed_deprecates_immediately(conn):
    confirmed(conn, "old")
    confirmed(conn, "new")
    store.supersede(conn, "new", "old", OPERATOR, now=NOW)
    assert store.get_card(conn, "old").status == "deprecated"


def test_supersedes_field_on_upsert_creates_the_edge(conn):
    confirmed(conn, "old")
    propose(conn, "new", supersedes=["old"])
    store.confirm(conn, "new", OPERATOR, now=NOW)
    assert store.get_card(conn, "old").status == "deprecated"


def test_mark_conflict_is_symmetric_and_logged(conn):
    confirmed(conn, "a")
    propose(conn, "b")
    store.mark_conflict(conn, "a", "b", SYSTEM, now=NOW)
    assert store.get_card(conn, "a").conflicts_with == ["b"]
    assert store.get_card(conn, "b").conflicts_with == ["a"]
    assert actions(conn, "a")[-1][:2] == ("conflict", SYSTEM)
    assert store.get_card(conn, "a").status == "confirmed"  # a conflict flags, it does not demote


def test_mark_conflict_unknown_card(conn):
    propose(conn, "a")
    with pytest.raises(store.UnknownCard):
        store.mark_conflict(conn, "a", "nope", SYSTEM, now=NOW)


def test_review_due_lists_confirmed_cards_past_review_after(conn):
    confirmed(conn, "due", review_after=(NOW - timedelta(days=1)).date())
    confirmed(conn, "later", review_after=(NOW + timedelta(days=30)).date())
    propose(conn, "cand", review_after=(NOW - timedelta(days=1)).date())  # candidates are not folklore yet
    assert [c.id for c in store.review_due(conn, NOW)] == ["due"]


def test_review_due_falls_back_to_stale_last_verified(conn):
    confirmed(conn, "no-date")  # last_verified_at == NOW
    assert store.review_due(conn, NOW) == []
    later = NOW + timedelta(days=store.DEFAULT_REVIEW_DAYS + 1)
    assert [c.id for c in store.review_due(conn, later)] == ["no-date"]


def test_reverify_is_operator_only_and_clears_review(conn):
    confirmed(conn, "due", review_after=(NOW - timedelta(days=1)).date())
    with pytest.raises(store.ActorNotPermitted):
        store.reverify(conn, "due", RUNTIME, now=NOW)
    store.reverify(conn, "due", OPERATOR, now=NOW, review_after=(NOW + timedelta(days=90)).date())
    assert store.review_due(conn, NOW) == []
    assert actions(conn, "due")[-1][:2] == ("reverify", OPERATOR)


def test_deprecated_cards_are_not_review_due(conn):
    confirmed(conn, "c1", review_after=(NOW - timedelta(days=1)).date())
    store.deprecate(conn, "c1", OPERATOR, now=NOW)
    assert store.review_due(conn, NOW) == []


@pytest.mark.parametrize("payload", [
    "DB_PASSWORD=correct-horse-battery",
    "authorization: Bearer abcdef0123456789",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "postgres://svc:hunter22hunter@db.example.invalid/app",
])
def test_secret_shaped_content_is_refused_without_echoing_it(conn, payload):
    with pytest.raises(store.SecretShapedContent) as err:
        store.upsert_candidate(conn, make_card("c1", statement=f"Line one.\n{payload}\n"), OPERATOR, now=NOW)
    assert payload not in str(err.value)
    assert "statement" in str(err.value) and "line 2" in str(err.value)
    assert store.get_card(conn, "c1") is None


def test_secret_in_step_or_title_is_refused(conn):
    with pytest.raises(store.SecretShapedContent):
        store.upsert_candidate(conn, make_card("c1", title="PASSWORD=abcdef12345"), OPERATOR, now=NOW)
    with pytest.raises(store.SecretShapedContent):
        store.upsert_candidate(conn, make_card("c2", type="procedure", steps=[
            {"text": "Log in.", "check": "api_token: abcdef1234567890"}]), OPERATOR, now=NOW)


def test_credential_placeholders_are_not_secrets(conn):
    store.upsert_candidate(
        conn, make_card("c1", statement="Needs <credential: cloudflare-dns-token> from the vault."),
        OPERATOR, now=NOW)
    assert store.get_card(conn, "c1") is not None
