"""4.2 (docs/designs/missions-memory.md, release 3): NEEDS YOU is one rule in Core (`needs.list`), and a run a Console conversation
started is decided in that conversation, not on its mission page or over Telegram."""
import pytest
from missions_helpers import make_store, new_mission
from missions_helpers import repair_grant as _repair

from planet_express.application.mission_service import MissionError, MissionService
from planet_express.integrations.rpc import build_core_handlers
from planet_express.missions.models import Grant
from planet_express.missions.store import MissionStore

OBSERVE = Grant.model_validate({"tier": "observe", "scope": {}})


class Console:
    """Stands in for `ConsoleApi`: what its rail lists, or an error."""

    def __init__(self, conversations=(), error=None):
        self.conversations, self.error = list(conversations), error

    def list(self):
        if self.error:
            raise self.error
        return {"conversations": self.conversations, "status": {}}


def conv(cid, group, title="Update immich", text="Needs your call"):
    return {"id": cid, "title": title, "status": {"group": group, "text": text}}


@pytest.fixture
def env(tmp_path):
    store, _ = make_store(tmp_path)
    svc = MissionService(store, enabled=lambda: True)
    return svc, store, MissionStore(store)


def mission(store, ms, status, *, tier="repair", runtime="command", conversation=None, run_of=None):
    """A mission in `status`, optionally a conversation's own (runtime console) or a run of one."""
    m = new_mission(ms, OBSERVE if tier == "observe" else _repair())
    import sqlite3
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA foreign_keys = OFF")                  # the rows here are minimal: only what NEEDS YOU reads
    with conn:
        conn.execute("UPDATE missions SET status=?, runtime=? WHERE id=?", (status, runtime, m.id))
        if conversation:
            conn.execute("INSERT INTO console_conversations (id, mission_id, title, origin, token_budget, created_at, updated_at, "
                         "last_activity_at) VALUES (?, ?, 't', 'operator', 1, 1, 1, 1)", (conversation, m.id))
        if run_of:
            conn.execute("INSERT INTO console_executions (conversation_id, plan_id, option_index, mission_id, grant_hash, status, "
                         "created_at) VALUES (?, 1, 0, ?, 'h', 'authorised', 1)", (run_of, m.id))
    conn.close()
    return m.id


def ids(out):
    return [m["id"] if m["id"] != "Console" else m["href"] for m in out["missions"]]


def test_an_answered_conversation_is_not_a_decision_and_a_run_counts_once_through_its_conversation(env):
    svc, store, ms = env
    answered = mission(store, ms, "awaiting_decision", tier="observe", runtime="console", conversation="c-answered")
    plan = mission(store, ms, "awaiting_decision", tier="observe", runtime="console", conversation="c-plan")
    run = mission(store, ms, "awaiting_decision", run_of="c-plan")
    plain = mission(store, ms, "awaiting_decision")
    svc.console_api = Console([conv("c-answered", "Recent"), conv("c-plan", "Needs you"), conv("c-busy", "Working")])
    out = svc.needs_missions()
    assert ids(out) == [plain, "/console/c-plan"]
    assert answered not in ids(out) and plan not in ids(out) and run not in ids(out)


@pytest.mark.parametrize("console", [None, Console(error=RuntimeError("down")), Console([])])
def test_a_held_run_the_console_cannot_show_stays_listed(env, console):
    """Console off, unreadable, or the conversation archived: a held run never drops out of sight."""
    svc, store, ms = env
    held = mission(store, ms, "needs_operator", run_of="c-gone")
    mission(store, ms, "awaiting_decision", tier="observe", runtime="console", conversation="c-gone")
    svc.console_api = console
    assert ids(svc.needs_missions()) == [held]


def test_a_held_run_stays_listed_while_its_conversation_is_working(env):
    svc, store, ms = env
    held = mission(store, ms, "needs_operator", run_of="c-busy")
    mission(store, ms, "investigating", tier="observe", runtime="console", conversation="c-busy")
    svc.console_api = Console([conv("c-busy", "Working", text="Looking into it")])
    assert ids(svc.needs_missions()) == [held]


def test_needs_list_counts_plans_windows_and_missions_and_unknown_is_never_zero(env, monkeypatch):
    from planet_express.integrations import rpc
    monkeypatch.setattr(rpc, "NEEDS_TTL_S", -1.0)                  # every call rebuilds (the 3 s cache is tested below)
    svc, store, ms = env
    mission(store, ms, "needs_operator")
    handlers = build_core_handlers(None, store, missions=svc)
    out = handlers["needs.list"]({})
    assert (out["plans"], out["held_windows"], len(out["missions"]), out["count"]) == (0, 0, 1, 1)
    monkeypatch.setattr(svc, "needs_missions", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    out = handlers["needs.list"]({})
    assert out["missions_readable"] is False and out["count"] == 0          # missions unreadable leave out only themselves
    monkeypatch.setattr(store, "list_pending", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert handlers["needs.list"]({})["count"] is None                       # plans unknown: the count is unknown


def test_needs_list_is_cached_for_a_few_seconds(env, monkeypatch):
    svc, store, ms = env
    calls = []
    monkeypatch.setattr(svc, "needs_missions", lambda: calls.append(1) or {"missions": [], "truncated": False})
    handlers = build_core_handlers(None, store, missions=svc)
    handlers["needs.list"]({})
    handlers["needs.list"]({})
    assert len(calls) == 1


def test_with_missions_off_needs_list_reads_no_mission(env):
    svc, store, ms = env
    svc._enabled = lambda: False
    out = build_core_handlers(None, store, missions=svc)["needs.list"]({})
    assert out["missions"] == [] and out["count"] == 0


# ── one place to act ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("call", [
    lambda svc, mid: svc.approve_mission(mid, operator="alice", phase="grant", grant_hash="0" * 64),
    lambda svc, mid: svc.deny_mission(mid, operator="alice", grant_hash="0" * 64),
    lambda svc, mid: svc.rollback_mission(mid, operator="alice"),
    lambda svc, mid: svc.recheck_mission(mid, operator="alice"),
    lambda svc, mid: svc.keep_mission(mid, operator="alice"),
])
def test_a_run_a_conversation_started_is_decided_in_the_conversation(env, call):
    svc, store, ms = env
    own(store, ms, "c-plan")
    run = mission(store, ms, "awaiting_decision", run_of="c-plan")
    svc.console_api = Console()
    with pytest.raises(MissionError) as exc:
        call(svc, run)
    assert exc.value.code == "in_conversation" and "/console/c-plan" in str(exc.value)


def own(store, ms, cid, status="awaiting_decision", state="open"):
    mid = mission(store, ms, status, tier="observe", runtime="console", conversation=cid)
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE console_conversations SET state=? WHERE id=?", (state, cid))
    return mid


def test_only_the_latest_run_of_an_open_conversation_is_left_to_the_console(env):
    """Review, 4.2: the Console shows and acts on a conversation's latest run only, and only while it is open. An earlier run (its
    Undo) and a run a closed conversation left behind are decided on their mission page, so no run is ever out of reach."""
    svc, store, ms = env
    svc.console_api = Console()
    own(store, ms, "c-open")
    earlier = mission(store, ms, "completed", run_of="c-open")
    latest = mission(store, ms, "awaiting_decision", run_of="c-open")
    own(store, ms, "c-archived", state="archived")
    orphan = mission(store, ms, "recovery_required", run_of="c-archived")
    own(store, ms, "c-done", status="completed")
    finished = mission(store, ms, "needs_operator", run_of="c-done")
    assert svc.console_decides(latest) == "c-open"
    assert svc.console_decides(earlier) is None and svc.console_decides(orphan) is None and svc.console_decides(finished) is None
    assert svc.get_mission(latest)["decided_in_console"] == "c-open" and svc.get_mission(earlier)["decided_in_console"] is None


def test_with_the_console_off_a_run_is_decided_on_its_page_as_before(env):
    svc, store, ms = env
    run = mission(store, ms, "awaiting_decision", run_of="c-plan")
    svc.console_api = None
    with pytest.raises(MissionError) as exc:                  # past the guard: the flow itself answers (none is attached here)
        svc.deny_mission(run, operator="alice", grant_hash="0" * 64)
    assert exc.value.code != "in_conversation"


def test_a_mission_no_conversation_owns_is_still_decided_anywhere(env):
    svc, store, ms = env
    plain = mission(store, ms, "awaiting_decision")
    svc.console_api = Console()
    with pytest.raises(MissionError) as exc:
        svc.deny_mission(plain, operator="alice", grant_hash="0" * 64)
    assert exc.value.code != "in_conversation"


def test_telegram_approve_deny_and_rollback_point_to_the_conversation(env):
    svc, store, ms = env
    own(store, ms, "c-plan1234")
    run = mission(store, ms, "awaiting_decision", run_of="c-plan1234")
    svc.console_api = Console()
    for text in (f"/approve {run} 00000000", f"/deny {run} 00000000", f"/rollback {run}"):
        reply = svc.telegram(text, "alice")
        assert "decide it there" in reply and "/plan c-plan12" in reply and "/run c-plan12 rollback|recheck|keep" in reply, text
