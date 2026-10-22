"""G4-F2 safety fixes (from the G4 /code-review): each test failed before its fix."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build

from planet_express.missions import approvals
from planet_express.missions.approval_flow import FlowError
from planet_express.missions.models import Grant


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, start_monitor=False, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def rows(st, mid):
    with sqlite3.connect(st.store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM mission_approvals WHERE mission_id=?", (mid,))]


# ── #1 the card shows the whole plan the hash binds ───────────────────────────────────────────────────

def test_long_http_url_appears_complete_on_the_card(stack):
    st = stack()
    url = "http://localhost/" + "a" * 300 + "/TAIL-OF-THE-URL"
    mid = st.propose(checks=[{"id": "default", "check": "http", "params": {"url": url}}])["mission_id"]
    card = st.flow.card(mid)
    assert card["verification"]["checks"][0]["params"]["url"] == url
    assert card["verification"]["truncated"] is False


def test_long_exec_argv_appears_complete_on_the_card(stack):
    st = stack()
    argv = [f"arg{i}-" + "x" * 200 for i in range(6)]
    chk = {"id": "default", "check": "exec_probe", "params": {"container": "media-app", "argv": argv}}
    mid = st.propose(checks=[chk])["mission_id"]
    shown = st.flow.card(mid)["verification"]["checks"][0]["params"]["argv"]
    assert all(a in shown for a in argv)


def test_a_card_that_would_clip_cannot_be_approved(stack, monkeypatch):
    from planet_express.missions import approval_flow as af
    st = stack()
    mid = st.propose()["mission_id"]
    monkeypatch.setattr(af, "PARAM_CARD_MAX", 5)                     # anything longer than 5 chars would be clipped
    card = st.flow.card(mid)
    assert card["verification"]["truncated"] is True and card["ready"] is False
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "grant", st.ms.get_mission(mid).grant_hash)
    assert not rows(st, mid)


# ── #2 a critical mission's grant row must be elevated, in the predicate itself ───────────────────────

def test_a_non_elevated_grant_row_does_not_satisfy_a_critical_mission(stack):
    from missions_helpers import new_mission
    st = stack()
    m = new_mission(st.ms, Grant.model_validate({"tier": "critical", "scope": {"stacks": ["media"]}}))
    with sqlite3.connect(st.store.path) as conn:
        conn.execute("INSERT INTO mission_approvals (id, mission_id, phase, bound_hash, decision, operator, elevated, decided_at) "
                     "VALUES ('A-x', ?, 'grant', ?, 'approve', 'operator:me', 0, 1)", (m.id, m.grant_hash))
    verdict = approvals.execution_predicate(st.store, m.id)
    assert not verdict.ok
    assert any("elevated" in r and "grant" in r for r in verdict.reasons)
