from supervisor_fakes import Rig, update


def test_crash_mid_run_restarts_within_the_retry_budget_then_asks_the_operator(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 1})
    rig.sup.start(rig.mid)
    first = rig.session_ids()[0]
    rig.cont.crash(first)
    rig.sup.poll(rig.mid)                              # crash seen -> restart (1 retry allowed)
    assert rig.status() == "investigating" and len(rig.runtimes) == 2
    second = next(s for s in rig.session_ids() if s != first)
    assert rig.ms.live_session(rig.mid)["id"] == second
    rig.cont.crash(second)
    rig.sup.poll(rig.mid)                              # budget spent
    assert rig.status() == "needs_operator" and len(rig.runtimes) == 2
    assert rig.ms.live_session(rig.mid) is None


def test_no_retries_means_the_first_crash_goes_to_the_operator(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 0})
    rig.sup.start(rig.mid)
    rig.cont.crash(rig.session_ids()[0])
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator" and len(rig.runtimes) == 1


def test_runtime_reporting_failed_is_a_crash(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 0}, behaviours=[{"script": [update("failed")]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"


def test_recover_after_supervisor_crash_kills_the_old_session_and_restarts(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 1})
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    pids = rig.cont.spawns(old)
    assert pids
    rig.sup = rig.new_supervisor()                    # the daemon restarted: no in-memory state
    acted = rig.sup.recover()
    assert acted == [rig.mid]
    assert rig.cont.kill_calls(old) >= 1 and not rig.cont.exists(old)
    assert rig.status() == "investigating" and len(rig.runtimes) == 2
    live = rig.ms.live_session(rig.mid)
    assert live and live["id"] != old
    # a second restart spends the last retry; the third time there is none left
    rig.sup = rig.new_supervisor()
    rig.sup.recover()
    assert rig.status() == "needs_operator"
    assert rig.ms.live_session(rig.mid) is None


def test_recover_does_not_restart_until_the_old_session_is_provably_empty(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 2})
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    rig.cont.ignore_kill(old, 3)
    rig.sup = rig.new_supervisor()
    rig.sup.recover()
    assert len(rig.runtimes) == 1 and rig.status() == "investigating"
    for _ in range(4):
        rig.sup.poll(rig.mid)
    assert len(rig.runtimes) == 2 and rig.status() == "investigating"


def test_recover_ignores_terminal_and_queued_missions(tmp_path):
    rig = Rig(tmp_path)
    assert rig.sup.recover() == []
    assert rig.status() == "queued" and rig.runtimes == []


def test_cancel_after_a_supervisor_restart_still_kills_before_the_terminal_transition(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    rig.cont.ignore_kill(old, 2)
    rig.sup = rig.new_supervisor()                    # no in-memory run
    rig.sup.cancel(rig.mid, "operator:me")
    assert rig.cont.kill_calls(old) >= 1 and rig.status() == "investigating"
    for _ in range(3):
        rig.sup.poll(rig.mid)
    assert rig.status() == "cancelled" and not rig.cont.exists(old)


def test_recover_reaps_a_live_session_of_a_mission_that_moved_on_while_down(tmp_path):
    from missions_helpers import advance_to
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    advance_to(rig.ms, rig.mid, "blocked")             # someone moved it on without the supervisor
    rig.sup = rig.new_supervisor()
    assert rig.sup.recover() == [rig.mid]
    assert not rig.cont.exists(old) and rig.ms.live_session(rig.mid) is None
    assert rig.status() == "blocked" and len(rig.runtimes) == 1       # no restart, state untouched


def test_pipe_setup_failure_is_a_start_failure_not_a_stranded_run(tmp_path, monkeypatch):
    rig = Rig(tmp_path)

    def no_pipe():
        raise OSError("too many open files")
    monkeypatch.setattr("planet_express.missions.supervisor.os.pipe", no_pipe)
    rig.sup.start(rig.mid)
    assert rig.status() == "needs_operator" and rig.ms.live_session(rig.mid) is None
    assert rig.sup.poll(rig.mid) == "needs_operator"


def test_a_stale_run_never_revokes_or_settles_over_a_replacement_session(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    rig.ms.revoke_session(old)                                   # the old session was revoked elsewhere...
    sid = rig.add_replacement()                                  # ...and replaced
    rig.cont.crash(old)
    rig.sup.poll(rig.mid)                                        # the stale run for `old` wakes up and ends
    assert rig.ms.live_session(rig.mid)["id"] == sid
    assert rig.status() == "investigating"


def test_cancel_reaches_a_replacement_session_after_the_stale_run_ends(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    rig.ms.revoke_session(rig.session_ids()[0])
    rig.add_replacement()
    rig.sup.cancel(rig.mid, "operator:me")
    rig.sup.poll(rig.mid)
    assert rig.status() == "cancelled" and rig.cont.kill_calls("S-replacement") >= 1


def test_recover_cleans_up_an_empty_leftover_cgroup(tmp_path):
    from missions_helpers import advance_to
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    rig.ms.revoke_session(old)
    rig.cont.crash(old)
    advance_to(rig.ms, rig.mid, "blocked")
    rig.sup = rig.new_supervisor()
    assert rig.sup.recover() == []
    assert not rig.cont.exists(old)


def test_a_crashed_command_session_keeps_its_last_output_lines_and_exit_status(tmp_path):
    """rc14.4: why a Repair (command) session died is kept as session-tail.txt, redacted, with the exit status."""
    import json
    import sqlite3

    from planet_express.missions.adapters.command import CommandRuntime
    rig = Rig(tmp_path, budgets={"retries": 0})
    runtime = CommandRuntime(rig.cont, argv=["/usr/bin/agent"], worker_socket="/run/w.sock")
    rig.sup.runtime_factory = lambda mission: runtime
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.cont.write_output(sid, b'{"type":"system"}\nTraceback (most recent call last):\nRuntimeError: boom PASSWORD=hunter2hunter2\n')
    rig.cont.crash(sid, exit_status=2)
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"
    with sqlite3.connect(rig.store.path) as db:
        paths = [r[0] for r in db.execute("SELECT path FROM mission_artifacts WHERE mission_id=? AND name='session-tail.txt'", (rig.mid,))]
    text = open(paths[0]).read()
    assert "exit status: 2" in text and "RuntimeError: boom" in text and "hunter2hunter2" not in text and '"system"' not in text
    note = [json.loads(e.body_json) for e in rig.ms.events(rig.mid) if e.kind == "note" and "session_tail" in e.body_json]
    assert note[0]["lines"] == 2 and note[0]["ended"] == "crash"


def test_a_cancelled_command_session_keeps_no_tail(tmp_path):
    import sqlite3

    from planet_express.missions.adapters.command import CommandRuntime
    rig = Rig(tmp_path, budgets={"retries": 0})
    runtime = CommandRuntime(rig.cont, argv=["/usr/bin/agent"], worker_socket="/run/w.sock")
    rig.sup.runtime_factory = lambda mission: runtime
    rig.sup.start(rig.mid)
    rig.cont.write_output(rig.session_ids()[0], b"working\n")
    rig.sup.cancel(rig.mid, "operator:alice")
    assert rig.status() == "cancelled"
    with sqlite3.connect(rig.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM mission_artifacts WHERE name='session-tail.txt'").fetchone()[0] == 0
