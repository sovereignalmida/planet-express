"""How the hook layer reaches a real session (slice 7a, Codex round 1): Core puts the policy and the log path in the
session's environment, and drains the log the hooks write (untrusted data, bounded) into the mission's events."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor_fakes import Rig, update

from planet_express.missions import supervisor as sup_mod
from planet_express.missions.hooks import HOOK_LOG_NAME, HookPolicy

POLICY = HookPolicy(live_trees=("/srv/live",), protected_paths=("/mnt/data",), blocked_failure="open")


BIG = {"tool_calls": 100000, "output_bytes": 10**9}


def started(tmp_path, n=6):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * n}], budgets=BIG)
    rig.sup.hook_policy = lambda mission: POLICY.to_json()
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    return rig, rig.sup.workspace_dir / sid / HOOK_LOG_NAME


def hook_events(rig):
    return [(e.kind, e.actor, json.loads(e.body_json)) for e in rig.ms.events(rig.mid) if json.loads(e.body_json).get("via") == "hook"]


def test_the_session_environment_carries_the_policy_and_the_log_path_and_never_the_token(tmp_path):
    rig, log = started(tmp_path)
    env = rig.cont.spawns(rig.session_ids()[0])[0]["env"]
    assert HookPolicy.from_json(env["PE_HOOK_POLICY"]) == POLICY
    assert env["PE_HOOK_LOG"] == str(log)
    token = rig.cont.spawns(rig.session_ids()[0])[0]["token"]
    assert token and all(token not in v for v in env.values())            # the token is on fd 3 only: not in any variable


def test_without_a_hook_policy_nothing_is_added_to_the_environment(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 3}])
    rig.sup.start(rig.mid)
    env = rig.cont.spawns(rig.session_ids()[0])[0]["env"]
    assert "PE_HOOK_POLICY" not in env and "PE_HOOK_LOG" not in env


def test_the_claude_code_settings_run_this_module_before_and_after_every_tool_call(tmp_path):
    from planet_express.missions.hooks import claude_code_settings, install_claude_code
    command = ["/opt/pe/venv/bin/python", "-m", "planet_express.missions.hooks"]
    settings = claude_code_settings(command)
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/opt/pe/venv/bin/python -m planet_express.missions.hooks pre"
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith(" post")
    tricky = claude_code_settings(["/opt/my dir/python", "-m", "x;rm"])
    assert "'/opt/my dir/python'" in tricky["hooks"]["PreToolUse"][0]["hooks"][0]["command"]     # quoted, never re-split
    env = install_claude_code(tmp_path, command)
    assert json.loads((tmp_path / "settings.json").read_text()) == settings
    assert env == {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


COMMAND = ["/opt/pe/python", "-m", "planet_express.missions.hooks"]


def with_install(rig, root):
    from planet_express.missions.hooks import install_claude_code
    rig.sup.hook_root = root
    rig.sup.hook_install = lambda hook_dir: install_claude_code(hook_dir, COMMAND)


def test_the_hook_settings_live_in_a_core_owned_directory_outside_the_workspace_and_the_session_cannot_write_them(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 5}])
    root = tmp_path / "hooks"
    root.mkdir()
    with_install(rig, root)
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    settings = root / sid / "settings.json"
    env = rig.cont.spawns(sid)[0]["env"]
    assert settings.exists() and env["CLAUDE_CONFIG_DIR"] == str(root / sid)
    assert not (rig.sup.workspace_dir / sid / "settings.json").exists()                 # nothing of it is in the session's workspace
    assert not str(settings).startswith(str(rig.sup.workspace_dir))
    # the modes a session uid (neither the owner nor in the group) sees: it can read, it cannot write or delete
    assert oct(settings.stat().st_mode & 0o777) == "0o644" and oct(settings.parent.stat().st_mode & 0o777) == "0o755"
    assert not (settings.stat().st_mode & 0o022) and not (settings.parent.stat().st_mode & 0o022)
    assert settings.stat().st_uid == os.getuid()                                          # Core's, not a session's


def test_changed_or_missing_settings_are_a_session_fault_that_ends_the_session(tmp_path):
    for tamper in ("edit", "delete", "replace-by-symlink"):
        rig = Rig(tmp_path / tamper, behaviours=[{"script": [update()] * 9}])
        root = tmp_path / tamper / "hooks"
        root.mkdir(parents=True)
        with_install(rig, root)
        rig.sup.start(rig.mid)
        sid = rig.session_ids()[0]
        settings = root / sid / "settings.json"
        rig.sup.poll(rig.mid)
        assert rig.status() == "investigating"                                            # intact: nothing happens
        if tamper == "edit":
            settings.write_text('{"hooks": {}}\n')
        elif tamper == "delete":
            settings.unlink()
        else:
            real = settings.with_name("real.json")
            real.write_text(settings.read_text())
            settings.unlink()
            settings.symlink_to(real)
        rig.sup.poll(rig.mid)
        events = [json.loads(e.body_json) for e in rig.ms.events(rig.mid)]
        assert any(b.get("hook_settings") == "changed or missing" for b in events), tamper
        assert any(b.get("kill") == "hook_tamper" for b in events)
        assert not rig.cont.exists(sid) and rig.ms.live_session(rig.mid) is None         # the session was killed and reaped
        assert not (root / sid).exists()                                                  # and its settings directory removed


def test_a_repair_session_that_edits_its_hooks_is_classified_from_the_host_not_trusted(tmp_path):
    from repair_fakes import build
    st = build(tmp_path, behaviours=[{"script": [update()] * 9}])
    try:
        root = tmp_path / "hooks"
        root.mkdir()
        from planet_express.missions.hooks import install_claude_code
        st.sup.hook_root = root
        st.sup.hook_install = lambda hook_dir: install_claude_code(hook_dir, COMMAND)
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        st.sup.tick()
        sid = st.runtimes[0].context.session.session_id
        (root / sid / "settings.json").write_text("{}")
        st.sup.tick()
        assert st.status(mid) == "needs_operator"
        assert any(json.loads(e.body_json).get("event") == "recovery.classified" for e in st.ms.events(mid))
    finally:
        st.close()


def test_a_start_with_hooks_configured_but_no_core_owned_root_is_refused(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 3}])
    rig.sup.hook_install = lambda hook_dir: {}
    rig.sup.start(rig.mid)
    assert rig.status() == "needs_operator" and rig.cont.spawns(rig.session_ids()[0]) == []


def test_a_failed_install_stops_the_start_and_leaves_nothing_launched(tmp_path):
    broken = Rig(tmp_path / "b", behaviours=[{"script": [update()] * 3}])
    (tmp_path / "b" / "hooks").mkdir(parents=True)
    broken.sup.hook_root = tmp_path / "b" / "hooks"
    broken.sup.hook_install = lambda hook_dir: (_ for _ in ()).throw(OSError("cannot write"))
    broken.sup.start(broken.mid)
    assert broken.status() == "needs_operator" and broken.cont.spawns(broken.session_ids()[0]) == []


def test_the_config_requires_a_command_and_a_root_to_install_and_the_runner_installs_only_when_asked(tmp_path):
    import pytest
    from missions_helpers import make_store

    from config_schema import MissionsConfig
    from planet_express.missions.containment import FakeContainment
    from planet_express.missions.runner import build_mission_runner
    with pytest.raises(ValueError):
        MissionsConfig(enabled=True, hooks={"install_claude_code": True})
    with pytest.raises(ValueError):
        MissionsConfig(enabled=True, hooks={"install_claude_code": True, "command": COMMAND})       # no root: the session could edit it
    with pytest.raises(ValueError):
        MissionsConfig(enabled=True, hooks={"command": ["python", "-m", "x"]})                      # not absolute
    with pytest.raises(ValueError):
        MissionsConfig(enabled=True, hooks={"root": "relative/dir"})
    store, _ = make_store(tmp_path)
    plain = build_mission_runner(store, MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/a"]}), containment=FakeContainment())
    assert plain.supervisor.hook_install is None and plain.supervisor.hook_policy is not None and plain.supervisor.hook_root is None
    cfg = MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/a"]},
                         hooks={"install_claude_code": True, "command": COMMAND, "root": str(tmp_path / "hr")})
    installed = build_mission_runner(store, cfg, containment=FakeContainment())
    assert installed.supervisor.hook_root == tmp_path / "hr"
    (tmp_path / "hr").mkdir()
    assert plain.supervisor.hook_argv is None and installed.supervisor.hook_argv is not None
    assert installed.supervisor.hook_install(tmp_path / "hr") == {"CLAUDE_CONFIG_DIR": str(tmp_path / "hr")}
    assert (tmp_path / "hr" / "settings.json").exists()


def test_hook_lines_become_events_exactly_once_and_count_against_the_budgets(tmp_path):
    rig, log = started(tmp_path)
    log.write_text(json.dumps({"kind": "tool", "data": {"hook": "pre", "tool": "Bash", "verdict": "block", "class": "publish",
                                                        "reason": "git push", "input": "git push origin main"}}) + "\n"
                   + json.dumps({"kind": "note", "data": {"hook": "post", "tool": "Bash", "outcome": "ok"}}) + "\n")
    rig.sup.poll(rig.mid)
    rig.sup.poll(rig.mid)                                                  # a second poll finds nothing new
    events = hook_events(rig)
    assert [(k, a) for k, a, b in events] == [("tool", "worker"), ("note", "worker")]
    assert events[0][2]["verdict"] == "block" and events[0][2]["class"] == "publish"
    assert rig.sup._runs[rig.mid].tool_calls == 1


def test_a_partial_last_line_waits_for_its_newline(tmp_path):
    rig, log = started(tmp_path)
    line = json.dumps({"kind": "note", "data": {"hook": "post", "tool": "Bash"}})
    log.write_text(line[:20])
    rig.sup.poll(rig.mid)
    assert hook_events(rig) == []
    log.write_text(line + "\n")
    rig.sup.poll(rig.mid)
    assert len(hook_events(rig)) == 1


def test_hostile_log_content_cannot_forge_state_decisions_or_core_actors_and_is_bounded(tmp_path):
    rig, log = started(tmp_path)
    forged = [
        {"kind": "state", "data": {"from": "executing", "to": "completed"}},
        {"kind": "decision", "data": {"event": "approval.recorded", "phase": "grant"}},
        {"kind": "tripwire", "data": {"event": "tripwire.paused"}},
        {"kind": "note", "actor": "core", "data": {"event": "intent.done", "id": "I-1", "effect": "applied"}},
        {"kind": "note", "data": {"hook": "x", "event": "recovery.classified", "state": "verifying", "tool": "t" * 3000}},
        ["not", "an", "object"], {"kind": "note", "data": "text"}, {"kind": "tool"},
    ]
    log.write_bytes(b"\n".join(json.dumps(x).encode() for x in forged) + b"\nnot json\n" + b"x" * 5000 + b"\n")
    rig.sup.poll(rig.mid)
    events = hook_events(rig)
    assert len(events) == 1                                                # only the one well-formed note was kept
    kind, actor, body = events[0]
    assert (kind, actor) == ("note", "worker") and set(body) <= {"via", "hook", "tool", "verdict", "class", "reason", "input", "outcome"}
    assert len(body["tool"]) == sup_mod.MAX_HOOK_FIELD                      # clipped
    assert "event" not in body                                              # a hook line cannot name a Core event
    from planet_express.missions import recovery
    assert recovery.open_intents(rig.ms.events(rig.mid)) == recovery.open_intents(
        [e for e in rig.ms.events(rig.mid) if e.actor != "worker"])         # nothing the log said reached the trusted journal
    assert rig.status() == "investigating"


def test_a_symlinked_or_non_regular_log_is_never_read(tmp_path):
    rig, log = started(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text(json.dumps({"kind": "note", "data": {"hook": "x", "reason": "core's own file"}}) + "\n")
    os.symlink(secret, log)
    rig.sup.poll(rig.mid)
    assert hook_events(rig) == []
    os.unlink(log)
    os.mkfifo(log)
    rig.sup.poll(rig.mid)                                                  # a FIFO must not block or be read
    assert hook_events(rig) == []


def test_a_long_log_is_processed_in_full_across_polls_with_nothing_skipped_or_repeated(tmp_path):
    rig, log = started(tmp_path, n=60)
    line = json.dumps({"kind": "tool", "data": {"hook": "pre", "tool": "Bash", "verdict": "allow"}}) + "\n"
    total = 3000
    log.write_text(line * total)
    for _ in range(40):
        rig.sup.poll(rig.mid)
        if len(hook_events(rig)) >= total:
            break
    assert len(hook_events(rig)) == total                                   # more than the old 200 per poll, none dropped
    assert rig.sup._runs[rig.mid].tool_calls == total                      # every pre-hook line counts as a tool call
    rig.sup.poll(rig.mid)
    assert len(hook_events(rig)) == total                                   # and none twice


def test_a_poll_never_loses_the_lines_past_one_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(sup_mod, "MAX_HOOK_READ", 2048)
    rig, log = started(tmp_path, n=60)
    line = json.dumps({"kind": "note", "data": {"hook": "post", "tool": "T" * 60}}) + "\n"
    log.write_text(line * 100)
    for _ in range(60):
        rig.sup.poll(rig.mid)
    assert len(hook_events(rig)) == 100


def test_the_final_drain_runs_after_the_session_is_dead_and_before_it_is_settled(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}], budgets=BIG)
    rig.sup.hook_policy = lambda mission: POLICY.to_json()
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    log = rig.sup.workspace_dir / sid / HOOK_LOG_NAME
    line = json.dumps({"kind": "tool", "data": {"hook": "pre", "tool": "Bash", "verdict": "ask", "input": "last words"}}) + "\n"
    log.write_text(line * 2000)                                               # written right before the result: more than one poll's worth
    order = []
    original = rig.sup._settle
    rig.sup._settle = lambda run: (order.append(len(hook_events(rig))), original(run))[1]
    rig.sup.poll(rig.mid)                                                     # result, kill, final drain, settle - in one poll
    assert order == [2000]                                                    # everything was read before settle ran
    assert len(hook_events(rig)) == 2000 and rig.status() == "awaiting_decision"


def test_a_log_that_shrinks_or_disappears_is_reported_as_a_session_fault_once(tmp_path):
    rig, log = started(tmp_path)
    line = json.dumps({"kind": "note", "data": {"hook": "post", "tool": "Bash"}}) + "\n"
    log.write_text(line * 5)
    rig.sup.poll(rig.mid)
    assert len(hook_events(rig)) == 5
    log.write_text(line)                                                      # truncated behind Core's offset
    rig.sup.poll(rig.mid)
    rig.sup.poll(rig.mid)
    faults = [json.loads(e.body_json) for e in rig.ms.events(rig.mid) if "hook_log_fault" in e.body_json]
    assert len(faults) == 1 and "shorter" in faults[0]["hook_log_fault"] and e_actor(rig) == "supervisor"
    log.unlink()
    rig.sup.poll(rig.mid)
    assert len([1 for e in rig.ms.events(rig.mid) if "hook_log_fault" in e.body_json]) == 1      # reported once per session


def e_actor(rig):
    return next(e.actor for e in rig.ms.events(rig.mid) if "hook_log_fault" in e.body_json)


# ── a policy too large for the environment travels as a Core-owned file (G4 final review) ───────────────────────────

def big_policy():
    return HookPolicy(live_trees=("/srv/live",), protected_paths=tuple(f"/mnt/data/volumes/{n:064x}" for n in range(80)), blocked_failure="open")


def test_a_policy_over_the_launcher_limit_goes_in_a_core_owned_file(tmp_path):
    from planet_express.missions.hooks import POLICY_FILE_ENV
    policy = big_policy()
    assert len(policy.to_json()) > 4096                                    # the size the launcher would refuse in an environment value
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 5}], budgets=BIG)
    root = tmp_path / "hooks"
    root.mkdir()
    with_install(rig, root)
    rig.sup.hook_policy = lambda mission: policy.to_json()
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    env = rig.cont.spawns(sid)[0]["env"]
    assert "PE_HOOK_POLICY" not in env and all(len(v) <= 4096 for v in env.values())
    path = Path(env[POLICY_FILE_ENV])
    assert path == root / sid / "policy.json" and HookPolicy.from_json(path.read_text()) == policy
    assert oct(path.stat().st_mode & 0o777) == "0o644" and path.stat().st_uid == os.getuid()
    assert not str(path).startswith(str(rig.sup.workspace_dir))


def test_a_small_policy_still_travels_in_the_environment(tmp_path):
    from planet_express.missions.hooks import POLICY_FILE_ENV
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 3}], budgets=BIG)
    root = tmp_path / "hooks"
    root.mkdir()
    with_install(rig, root)
    rig.sup.hook_policy = lambda mission: POLICY.to_json()
    rig.sup.start(rig.mid)
    env = rig.cont.spawns(rig.session_ids()[0])[0]["env"]
    assert HookPolicy.from_json(env["PE_HOOK_POLICY"]) == POLICY and POLICY_FILE_ENV not in env


def test_a_large_policy_with_no_core_owned_directory_fails_the_start_closed(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 3}], budgets=BIG)
    policy = big_policy()
    rig.sup.hook_policy = lambda mission: policy.to_json()
    rig.sup.start(rig.mid)                                                  # no hook directory to put it in
    events = [json.loads(e.body_json) for e in rig.ms.events(rig.mid)]
    assert {"start_failed": "ContainmentError"} in events and rig.status() == "needs_operator"
    assert rig.ms.live_session(rig.mid) is None and not rig.cont.exists(rig.session_ids()[0])      # nothing is left running


def test_changing_the_policy_file_is_a_session_fault_like_changing_the_settings(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 9}], budgets=BIG)
    root = tmp_path / "hooks"
    root.mkdir()
    with_install(rig, root)
    policy = big_policy()
    rig.sup.hook_policy = lambda mission: policy.to_json()
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.sup.poll(rig.mid)
    assert rig.status() == "investigating"
    (root / sid / "policy.json").write_text(HookPolicy().to_json())          # weaken it to the default
    rig.sup.poll(rig.mid)
    events = [json.loads(e.body_json) for e in rig.ms.events(rig.mid)]
    assert any(b.get("kill") == "hook_tamper" for b in events)


def test_the_hook_cli_reads_the_policy_file_and_blocks_when_it_cannot(tmp_path):
    import io

    from planet_express.missions import hooks
    path = tmp_path / "policy.json"
    protected = f"/mnt/data/volumes/{3:064x}"
    path.write_text(big_policy().to_json())
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": f"cat {protected}/x"}})

    def run(environ, which="pre"):
        err = io.StringIO()
        return hooks.main([which], stdin=io.StringIO(payload), stderr=err, environ=environ), err.getvalue()
    code, _ = run({"PE_HOOK_POLICY_FILE": str(path)})
    assert code in (0, 2)                                                  # judged by the file's policy (which protects that path)
    # A policy file that was NAMED but cannot be loaded must block: falling back to the empty default would drop every protected path.
    for bad in ("not json", ""):
        path.write_text(bad)
        code, err = run({"PE_HOOK_POLICY_FILE": str(path)})
        assert code == 2 and "could not be loaded" in err, bad
    for environ in ({"PE_HOOK_POLICY_FILE": str(tmp_path / "missing.json")}, {"PE_HOOK_POLICY_FILE": str(tmp_path)}):
        assert run(environ)[0] == 2
    path.write_bytes(b"x" * (hooks.MAX_POLICY_FILE_BYTES + 1))
    assert run({"PE_HOOK_POLICY_FILE": str(path)})[0] == 2                 # too large to read: blocked, not defaulted
    log = tmp_path / "hook.log"
    post_payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {"output": "x"}})
    code = hooks.main(["post"], stdin=io.StringIO(post_payload), stderr=io.StringIO(),
                      environ={"PE_HOOK_POLICY_FILE": str(path), "PE_HOOK_LOG": str(log)})
    assert code == 0 and log.exists() and log.read_text().strip()          # the outcome is still recorded; the post hook never blocks
    assert isinstance(run({})[0], int)                                     # no file named at all: the old default behaviour


def test_a_policy_larger_than_the_hook_will_read_refuses_the_start(tmp_path):
    from planet_express.missions import supervisor as sup
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 3}], budgets=BIG)
    root = tmp_path / "hooks"
    root.mkdir()
    with_install(rig, root)
    rig.sup.hook_policy = lambda mission: "x" * (sup.MAX_POLICY_FILE_BYTES + 1)
    rig.sup.start(rig.mid)
    events = [json.loads(e.body_json) for e in rig.ms.events(rig.mid)]
    assert {"start_failed": "ContainmentError"} in events and rig.ms.live_session(rig.mid) is None


# ── settings sources: PE's settings are the only ones the runtime reads ─────────────────────────────

def test_the_runtime_is_launched_with_pe_settings_forced_and_every_other_source_off(tmp_path):
    from planet_express.missions.hooks import claude_code_argv
    hook_dir = tmp_path / "h"
    got = claude_code_argv(["/usr/bin/claude", "-p", "go", "--model", "m"], hook_dir)
    assert got == ["/usr/bin/claude", "-p", "go", "--model", "m", "--settings", str(hook_dir / "settings.json"), "--setting-sources", ""]


def test_an_operator_argv_cannot_re_enable_other_settings_sources_or_point_at_other_settings(tmp_path):
    from planet_express.missions.hooks import claude_code_argv
    hook_dir = tmp_path / "h"
    for hostile in (["--setting-sources", "user,project,local"], ["--setting-sources=project"], ["--settings", "/tmp/x.json"],
                    ["--settings=/tmp/x.json"]):
        got = claude_code_argv(["/usr/bin/claude", *hostile, "-p", "go"], hook_dir)
        assert got == ["/usr/bin/claude", "-p", "go", "--settings", str(hook_dir / "settings.json"), "--setting-sources", ""], hostile


def test_the_supervisor_launches_the_session_with_the_forced_settings_and_a_runtime_without_hooks_is_untouched(tmp_path):
    from planet_express.missions.hooks import claude_code_argv, install_claude_code
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 5}])
    root = tmp_path / "hooks"
    root.mkdir()
    rig.sup.hook_root = root
    rig.sup.hook_install = lambda hook_dir: install_claude_code(hook_dir, COMMAND)
    rig.sup.hook_argv = claude_code_argv
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    argv = rig.cont.spawns(sid)[0]["argv"]
    assert argv[-4:] == ["--settings", str(root / sid / "settings.json"), "--setting-sources", ""]
    plain = Rig(tmp_path / "plain", behaviours=[{"script": [update()] * 5}])
    plain.sup.start(plain.mid)
    plain_argv = plain.cont.spawns(plain.session_ids()[0])[0]["argv"]
    assert "--settings" not in plain_argv and "--setting-sources" not in plain_argv


def test_the_forced_flags_go_before_an_option_terminator_and_nothing_after_it_is_stripped(tmp_path):
    from planet_express.missions.hooks import claude_code_argv
    hook_dir = tmp_path / "h"
    got = claude_code_argv(["/usr/bin/claude", "-p", "--model", "m", "--", "--settings", "tell me about --setting-sources"], hook_dir)
    assert got == ["/usr/bin/claude", "-p", "--model", "m", "--settings", str(hook_dir / "settings.json"), "--setting-sources", "",
                   "--", "--settings", "tell me about --setting-sources"]
    assert claude_code_argv(["/usr/bin/claude", "--settings", "/x", "--", "go"], hook_dir)[:3] == ["/usr/bin/claude", "--settings", str(hook_dir / "settings.json")]


def test_a_settings_flag_with_no_value_fails_the_start_instead_of_swallowing_the_next_argument(tmp_path):
    import pytest
    from planet_express.missions.hooks import claude_code_argv
    for bad in (["/usr/bin/claude", "--setting-sources", "-p", "go"], ["/usr/bin/claude", "-p", "go", "--settings"]):
        with pytest.raises(ValueError, match="no value"):
            claude_code_argv(bad, tmp_path / "h")


def test_forced_settings_with_no_core_owned_hook_directory_refuse_to_launch(tmp_path):
    from planet_express.missions.hooks import claude_code_argv
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 5}])
    rig.sup.hook_argv = claude_code_argv                                  # forced settings, but hook_install and hook_root are unset
    rig.sup.start(rig.mid)
    assert rig.session_ids() == [] or rig.cont.spawns(rig.session_ids()[0]) == []      # nothing was launched
