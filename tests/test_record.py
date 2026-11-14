"""4.1: the record (docs/designs/missions-memory.md §4), built over the real schema; and its Core RPC."""
import json
import sqlite3

import pytest

from missions_helpers import make_store

from planet_express.application import record as R


def _db(tmp_path):
    store, _ = make_store(tmp_path)
    conn = sqlite3.connect(store.path)
    conn.execute("PRAGMA foreign_keys = OFF")              # rows here are minimal: only what the record reads
    return store, conn


def _mission(conn, mid, title, status, tier="observe", runtime="console", origin="operator", at=100, outcome=None):
    conn.execute("INSERT INTO missions (id, title, origin, status, tier, goal, success_criteria, grant_json, grant_hash, runtime, "
                 "created_at, updated_at, outcome) VALUES (?, ?, ?, ?, ?, 'g', 's', '{}', 'h', ?, ?, ?, ?)",
                 (mid, title, origin, status, tier, runtime, at, at, outcome))


def _conversation(conn, cid, mid, title, at=100, origin="operator"):
    conn.execute("INSERT INTO console_conversations (id, mission_id, title, origin, token_budget, created_at, updated_at, "
                 "last_activity_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)", (cid, mid, title, origin, at, at, at))


def _run(conn, cid, mid, status="ended"):
    conn.execute("INSERT INTO console_executions (conversation_id, plan_id, option_index, mission_id, grant_hash, status, "
                 "created_at) VALUES (?, 1, 0, ?, 'h', ?, 1)", (cid, mid, status))


def _spend(conn, mid, cost):
    body = {"event": "model.request", "charged": 10, **({"cost_usd": cost} if cost is not None else {})}
    conn.execute("INSERT INTO mission_events (mission_id, at, kind, actor, body_json) VALUES (?, 1, 'tool', 'broker', ?)",
                 (mid, json.dumps(body)))


def _approval(conn, aid, status="approved", origin="zoidberg", at=50.0, target=None, plan=None, action="runbook", decided_by=None):
    conn.execute("INSERT INTO approvals (id, action, target_key, target_json, risk, status, requested_via, created_at, expires_at, "
                 "decided_by, decided_at, plan_json, origin) VALUES (?, ?, 'k', ?, 'low', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (aid, action, json.dumps(target or {"title": "Safe prune"}), status, origin, at, at + 600, decided_by, at,
                  json.dumps(plan) if plan else None, origin))


def _execution(conn, eid, aid, status="passed", kind="run", at=60.0, parent=None, reason=None):
    conn.execute("INSERT INTO executions (id, approval_id, kind, parent_execution_id, status, started_at, finished_at, reason) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (eid, aid, kind, parent, status, at, at + 30, reason))


CANARY = {"steps": [{"type": "update.canary", "params": {"stack": "media", "service": "plex"}}]}


def test_a_conversation_is_one_row_with_its_runs_nested_and_its_whole_cost(tmp_path):
    _, conn = _db(tmp_path)
    _mission(conn, "M-1", "Restart miniflux", "awaiting_decision", at=100)
    _conversation(conn, "c1", "M-1", "Restart <the> miniflux container", at=100)
    _mission(conn, "M-2", "Run: restart miniflux", "rolled_back", tier="repair", runtime="command", at=200)
    _run(conn, "c1", "M-2")
    _mission(conn, "M-3", "Run: never authorised", "cancelled", tier="repair", runtime="command", at=150)
    _run(conn, "c1", "M-3", status="withdrawn")
    _spend(conn, "M-1", 0.10)
    _spend(conn, "M-2", 0.05)
    _spend(conn, "M-2", None)                                        # an unpriced call is counted, never guessed at
    conn.commit()
    rows = R.build_record(conn, missions=True, history=[])
    assert len(rows) == 1                                            # neither run is a row of its own
    [row] = rows
    assert row["kind"] == "conversation" and row["nature"] == "change" and row["origin"] == "console"
    assert row["outcome"] == "rolled_back" and row["at"] == 200 and row["href"] == "/console/c1"
    assert [r["id"] for r in row["runs"]] == ["M-2"]                 # a withdrawn run never ran
    assert row["calls"] == 3 and row["cost_usd"] == pytest.approx(0.15) and row["unpriced"] == 1


def test_a_question_and_a_mission_no_conversation_owns(tmp_path):
    _, conn = _db(tmp_path)
    _mission(conn, "M-1", "Are the apps up?", "awaiting_decision", at=300)
    _conversation(conn, "c1", "M-1", "Are the apps up?", at=300, origin="operator")
    _mission(conn, "M-5", "plex is unhealthy", "completed", at=40)
    _conversation(conn, "c5", "M-5", "plex is unhealthy", at=40, origin="incident")
    _mission(conn, "M-4", "Investigate: plex", "completed", tier="propose", runtime="command", origin="incident", at=50,
             outcome="a diagnosis")
    conn.commit()
    question, old, from_incident = R.build_record(conn, missions=True, history=[])
    assert (question["nature"], question["outcome"], question["origin"], question["cost_usd"]) == ("question", "open", "console", None)
    assert from_incident["origin"] == "incident"
    assert old["kind"] == "mission" and old["origin"] == "incident" and old["href"] == "/missions/M-4"
    assert old["detail"] == "a diagnosis" and old["outcome"] == "completed"


def test_with_missions_off_only_approvals_and_history_are_read(tmp_path):
    _, conn = _db(tmp_path)
    _mission(conn, "M-1", "x", "completed")
    _approval(conn, "a1", origin="system")
    _execution(conn, "e1", "a1")
    conn.commit()
    rows = R.build_record(conn, missions=False, history=[])
    assert [(r["kind"], r["origin"], r["outcome"], r["href"]) for r in rows] == [("execution", "automatic", "completed", "/executions/e1")]


def test_approval_outcomes(tmp_path):
    _, conn = _db(tmp_path)
    _approval(conn, "a1", origin="telegram-direct", at=10, action="docker.restart_service", target={"service": "plex"})
    _execution(conn, "e1", "a1", status="passed", at=11)
    _execution(conn, "e2", "a1", kind="rollback", parent="e1", status="passed", at=20)
    _approval(conn, "a2", status="denied", origin="planner", at=30, decided_by="chris", target={"title": "Restart plex"})
    _approval(conn, "a3", status="expired", origin="amy", at=40)
    _approval(conn, "a4", origin="dashboard-direct", at=50)
    _execution(conn, "e4", "a4", status="aborted", at=51, reason="stopped by the operator")
    _approval(conn, "a5", origin="incident", at=60)                  # approved, not run yet
    _approval(conn, "a6", status="pending", at=70)                   # waiting: NEEDS YOU, not the record
    _approval(conn, "a7", origin="mission", at=80)                   # a mission's own typed step: shown with the mission
    _execution(conn, "e7", "a7", at=81)
    _approval(conn, "a8", origin="planner", at=90)                   # undone, then run again and it passed
    _execution(conn, "e8", "a8", status="passed", at=91)
    _execution(conn, "e8r", "a8", kind="rollback", parent="e8", status="passed", at=92)
    _execution(conn, "e8b", "a8", status="passed", at=200)
    conn.commit()
    rows = {r["id"]: r for r in R.build_record(conn, missions=False, history=[], now=100)}
    assert set(rows) == {"a1", "a2", "a3", "a4", "a5", "a8"}
    assert (rows["a8"]["outcome"], rows["a8"]["href"]) == ("completed", "/executions/e8b")
    later = {r["id"]: r for r in R.build_record(conn, missions=False, history=[], now=60 + 7200)}
    assert (later["a5"]["outcome"], later["a5"]["detail"]) == ("cancelled", "approved, but it never ran")
    assert (rows["a1"]["outcome"], rows["a1"]["origin"], rows["a1"]["title"]) == ("rolled_back", "telegram", "docker restart service plex")
    assert (rows["a2"]["outcome"], rows["a2"]["origin"], rows["a2"]["detail"]) == ("declined", "scan", "denied by chris")
    assert rows["a3"]["detail"] == "expired before anyone decided"
    assert (rows["a4"]["outcome"], rows["a4"]["detail"]) == ("cancelled", "stopped by the operator")
    assert (rows["a5"]["outcome"], rows["a5"]["href"]) == ("open", None)


def test_a_canary_update_carries_its_history_line_and_history_is_never_shown_twice(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    _approval(conn, "a1", at=t, target={"title": "Canary update media/plex"}, plan=CANARY)
    _execution(conn, "e1", "a1", at=t + 5)
    conn.commit()
    iso = lambda ts: __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc).isoformat()
    history = [{"ts": iso(t + 600), "stack": "media", "service": "plex", "status": "updated", "reason": "plex:latest: 1a → 2b, ok"},
               {"ts": iso(t - 86400), "stack": "media", "service": "sonarr", "status": "rollback_failed", "reason": "no"},
               {"ts": iso(t - 2 * 86400), "stack": "media", "service": "radarr", "status": "rolled_back", "reason": "unhealthy"},
               {"ts": iso(t - 3 * 86400), "stack": "media", "service": "bazarr", "status": "skipped", "reason": "pinned"}]
    rows = R.build_record(conn, missions=False, history=history)
    assert [(r["kind"], r["detail"], r["outcome"]) for r in rows] == [
        ("execution", "plex:latest: 1a → 2b, ok", "completed"),
        ("update", "no", "failed"),                                  # one no approval accounts for: still on the record
        ("update", "unhealthy", "rolled_back"), ("update", "pinned", "cancelled")]


def test_zoidberg_putting_the_old_image_back_is_a_rollback_not_a_failure(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    _approval(conn, "a1", at=t, target={"title": "Canary update media/plex"}, plan=CANARY)
    _execution(conn, "e1", "a1", status="failed", at=t + 5)
    conn.commit()
    from datetime import datetime, timezone
    history = [{"ts": datetime.fromtimestamp(t + 60, timezone.utc).isoformat(), "stack": "media", "service": "plex",
                "status": "rolled_back", "reason": "unhealthy after update; old image back"}]
    [row] = R.build_record(conn, missions=False, history=history)
    assert row["outcome"] == "rolled_back" and row["detail"] == "unhealthy after update; old image back"


def test_a_huge_zoidberg_run_is_bounded_and_keeps_every_failure(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    for i in range(300):
        _approval(conn, f"a{i}", at=t + i * 10, target={"title": f"Canary update s/svc{i}"})
        _execution(conn, f"e{i}", f"a{i}", status="failed" if i in (5, 250) else "passed", at=t + i * 10)
    conn.commit()
    [row] = R.build_record(conn, missions=False, history=[])
    assert row["title"] == "Canary updates: 300 services" and len(row["runs"]) == 120
    assert sum(1 for r in row["runs"] if r["outcome"] == "failed") == 2


def test_the_cache_rebuilds_only_when_stale_or_the_switch_changes():
    clock = [0.0]
    cache = R.RecordCache(ttl_s=15, clock=lambda: clock[0])
    built = []
    build = lambda: built.append(1) or [{"n": len(built)}]
    assert cache.rows(True, build) == [{"n": 1}] and cache.rows(True, build) == [{"n": 1}]
    clock[0] = 16
    assert cache.rows(True, build) == [{"n": 2}]
    assert cache.rows(False, build) == [{"n": 3}]


def test_select_filters_pages_and_counts_each_filter_over_the_others():
    rows = ([{"outcome": "completed", "nature": "question", "origin": "console", "n": i} for i in range(3)]
            + [{"outcome": "failed", "nature": "change", "origin": "automatic", "n": 9}])
    page = R.select(rows, outcome=None, nature="change", origin=None, limit=50, offset=0)
    assert [r["n"] for r in page["rows"]] == [9] and page["total"] == 1
    assert page["counts"]["nature"] == {"question": 3, "change": 1}  # the chip's own filter is left out of its counts
    assert page["counts"]["outcome"] == {"failed": 1}
    page = R.select(rows, outcome=None, nature=None, origin=None, limit=2, offset=2)
    assert [r["n"] for r in page["rows"]] == [2, 9] and page["total"] == 4


def test_the_record_rpc_validates_and_answers_with_missions_off(tmp_path, monkeypatch):
    import config
    from planet_express.integrations.rpc import RpcError, build_core_handlers
    store, conn = _db(tmp_path)
    _approval(conn, "a1", origin="system")
    _execution(conn, "e1", "a1")
    conn.commit()
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", tmp_path / "missing.json")
    handlers = build_core_handlers(commands=None, store=store)
    page = handlers["record.list"]({"limit": 10, "offset": 0, "origin": "automatic"})
    assert page["total"] == 1 and page["rows"][0]["href"] == "/executions/e1"
    for bad in ({}, {"limit": 0, "offset": 0}, {"limit": 101, "offset": 0}, {"limit": 10, "offset": -1},
                {"limit": 10, "offset": 0, "outcome": "bogus"}, {"limit": 10, "offset": 0, "extra": 1},
                {"limit": True, "offset": 0}):
        with pytest.raises(RpcError):
            handlers["record.list"](bad)


def test_the_worker_can_never_read_the_record():
    from planet_express.missions.worker_rpc import WORKER_METHODS
    assert "record.list" not in WORKER_METHODS


def test_a_zoidberg_run_is_one_row_not_one_per_service(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    for i, (svc, status) in enumerate((("plex", "passed"), ("sonarr", "failed"), ("radarr", "passed"))):
        _approval(conn, f"a{i}", at=t + i * 300, target={"title": f"Canary update media/{svc}"})
        _execution(conn, f"e{i}", f"a{i}", status=status, at=t + i * 300)
    _approval(conn, "a9", at=t + 86400, target={"title": "Canary update media/bazarr"})   # the next night, alone
    _execution(conn, "e9", "a9", at=t + 86400)
    _approval(conn, "p1", origin="system", at=t + 400)                                      # a safe prune is not folded in
    _execution(conn, "ep", "p1", at=t + 400)
    conn.commit()
    rows = R.build_record(conn, missions=False, history=[])
    assert [(r["kind"], r["title"]) for r in rows] == [
        ("execution", "Canary update media/bazarr"), ("execution", "Canary update media/radarr"), ("execution", "Safe prune"),
        ("updates", "Canary updates: 2 services")]          # the prune ran between two updates: it splits the run, in time order
    assert rows[3]["outcome"] == "failed" and rows[3]["detail"] == "1 failed"
    assert [(x["title"], x["href"]) for x in rows[3]["runs"]] == [("media/sonarr", "/executions/e1"), ("media/plex", "/executions/e0")]


def test_a_page_stays_inside_the_rpc_frame():
    big = [{"outcome": "failed", "nature": "change", "origin": "automatic", "title": "x",
            "runs": [{"title": "y" * 200, "outcome": "failed"} for _ in range(120)]} for _ in range(100)]
    page = R.select(big, outcome=None, nature=None, origin=None, limit=100, offset=0)
    assert len(page["rows"]) == 100 and len(json.dumps(page)) < 700_000


def test_a_zoidberg_run_still_going_is_open_not_completed(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    _approval(conn, "a1", at=t, target={"title": "Canary update media/plex"})
    _execution(conn, "e1", "a1", at=t)
    _approval(conn, "a2", at=t + 300, target={"title": "Canary update media/sonarr"})
    _execution(conn, "e2", "a2", status="running", at=t + 300)
    conn.commit()
    [row] = R.build_record(conn, missions=False, history=[])
    assert (row["outcome"], row["detail"]) == ("open", "1 still running")


def test_a_capped_run_keeps_its_failures_ahead_of_skipped_services(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    _approval(conn, "f", at=t, target={"title": "Canary update s/broken"})
    _execution(conn, "ef", "f", status="failed", at=t)
    for i in range(130):
        _approval(conn, f"a{i}", at=t + 10 + i * 10, target={"title": f"Canary update s/svc{i}"})
        _execution(conn, f"e{i}", f"a{i}", status="aborted", at=t + 10 + i * 10)
    conn.commit()
    [row] = R.build_record(conn, missions=False, history=[])
    assert row["runs"][0]["title"] == "s/broken" and row["runs"][0]["outcome"] == "failed"


def test_a_run_where_nothing_ran_is_cancelled_and_skips_are_counted(tmp_path):
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    for i in range(2):
        _approval(conn, f"a{i}", at=t + i * 10, target={"title": f"Canary update s/svc{i}"})
        _execution(conn, f"e{i}", f"a{i}", status="aborted", at=t + i * 10)
    _approval(conn, "b0", at=t + 86400, target={"title": "Canary update s/x"})
    _execution(conn, "eb0", "b0", at=t + 86400)
    _approval(conn, "b1", at=t + 86410, target={"title": "Canary update s/y"})
    _execution(conn, "eb1", "b1", status="aborted", at=t + 86410)
    conn.commit()
    mixed, aborted = R.build_record(conn, missions=False, history=[])
    assert (aborted["outcome"], aborted["detail"]) == ("cancelled", "2 did not run")
    assert (mixed["outcome"], mixed["detail"]) == ("completed", "1 did not run")


def test_a_history_line_goes_to_the_closest_update_of_its_service(tmp_path):
    """Second review: a no-change check writes no line, so a later update's rollback must not land on the earlier run."""
    from datetime import datetime, timezone
    _, conn = _db(tmp_path)
    t = 1_790_000_000.0
    _approval(conn, "a1", at=t, target={"title": "Canary update media/plex"}, plan=CANARY)
    _execution(conn, "e1", "a1", status="passed", at=t)                       # no change: no history line
    _approval(conn, "a2", at=t + 600, target={"title": "Canary update media/plex"}, plan=CANARY)
    _execution(conn, "e2", "a2", status="failed", at=t + 600)
    conn.commit()
    history = [{"ts": datetime.fromtimestamp(t + 640, timezone.utc).isoformat(), "stack": "media", "service": "plex",
                "status": "rolled_back", "reason": "old image back"}]
    rows = {r["href"]: r for r in R.build_record(conn, missions=False, history=history)[0]["runs"]}
    assert rows["/executions/e2"]["outcome"] == "rolled_back" and rows["/executions/e1"]["outcome"] == "completed"


def test_a_later_turn_that_did_not_finish_names_the_conversation_held(tmp_path):
    _, conn = _db(tmp_path)
    _mission(conn, "M-1", "Update immich", "needs_operator", at=300)
    _conversation(conn, "c1", "M-1", "Update immich", at=300)
    _mission(conn, "M-2", "Run: update immich", "completed", tier="repair", runtime="command", at=200)
    _run(conn, "c1", "M-2")
    conn.commit()
    [row] = R.build_record(conn, missions=True, history=[])
    assert row["outcome"] == "held" and row["runs"][0]["outcome"] == "completed"


def test_a_canary_rollback_is_read_from_the_engine_after_the_history_file_rotates(tmp_path):
    _, conn = _db(tmp_path)
    _approval(conn, "a1", at=50, target={"title": "Canary update media/plex"}, plan=CANARY)
    _execution(conn, "e1", "a1", status="failed", at=60)
    conn.execute("INSERT INTO execution_steps (execution_id, n, type, params_json, binding_json, status, effect, output_json, reason) "
                 "VALUES ('e1', 1, 'update.canary', '{}', '{}', 'failed', 'not_applied', '{\"old\": \"1a\"}', 'unhealthy: old image back')")
    _mission(conn, "M-9", "Run", "rollback_pending", tier="repair", runtime="command")
    conn.commit()
    rows = {r["kind"]: r for r in R.build_record(conn, missions=True, history=[])}
    assert (rows["execution"]["outcome"], rows["execution"]["detail"]) == ("rolled_back", "unhealthy: old image back")
    assert rows["mission"]["outcome"] == "held"                  # waiting for the operator to ask for the restore


def test_a_rollback_cut_off_by_a_restart_is_not_a_completed_run(tmp_path):
    _, conn = _db(tmp_path)
    _approval(conn, "a1", origin="telegram", at=10)
    _execution(conn, "e1", "a1", status="passed", at=11)
    _execution(conn, "r1", "a1", kind="rollback", parent="e1", status="interrupted", at=100, reason="core restarted")
    _approval(conn, "a2", origin="telegram", at=200)
    _execution(conn, "e2", "a2", status="passed", at=201)
    _execution(conn, "r2", "a2", kind="rollback", parent="e2", status="running", at=300)
    conn.commit()
    rows = {r["id"]: r for r in R.build_record(conn, missions=False, history=[])}
    assert (rows["a1"]["outcome"], rows["a1"]["detail"], rows["a1"]["at"]) == ("failed", "rollback interrupted: core restarted", 130)
    assert rows["a2"]["outcome"] == "open"


def test_a_follow_up_running_after_a_finished_run_is_open(tmp_path):
    _, conn = _db(tmp_path)
    _mission(conn, "M-1", "Update immich", "investigating", at=300)
    _conversation(conn, "c1", "M-1", "Update immich", at=300)
    _mission(conn, "M-2", "Run", "completed", tier="repair", runtime="command", at=200)
    _run(conn, "c1", "M-2")
    _mission(conn, "M-3", "Answered", "awaiting_decision", at=300)
    _conversation(conn, "c3", "M-3", "Answered", at=290)
    _mission(conn, "M-4", "Run", "completed", tier="repair", runtime="command", at=200)
    _run(conn, "c3", "M-4")
    conn.commit()
    rows = {r["id"]: r for r in R.build_record(conn, missions=True, history=[])}
    assert rows["c1"]["outcome"] == "open" and rows["c3"]["outcome"] == "completed"
