"""One Console turn: what is launched, what is kept of the stream, and what a refused or interrupted turn leaves behind."""
import json
import os
from pathlib import Path

import pytest
from missions_helpers import make_store

from config_schema import MissionConsoleConfig
from planet_express.console import plan as plan_mod
from planet_express.console.runtime import MAX_ARG_PROMPT, ConsoleRuntime, TurnRefused
from planet_express.console.store import ConsoleStore
from planet_express.missions.containment import FakeContainment
from planet_express.missions.launcher.spec import LaunchSpec
from planet_express.missions.models import Grant
from planet_express.missions.runtime import RunContext
from planet_express.missions.store import MissionStore

FIXTURE = Path(__file__).parent / "fixtures" / "console_stream_tool_turn.jsonl"
GOOD_PLAN = {"known": ["a"], "unsure": ["b"], "options": [{
    "name": "n", "recommended": True, "summary": "s", "changes": ["c"], "will_not_touch": ["w"], "risk": {"level": "low", "text": "t"},
    "rolls_back_to": "r", "effort": "e", "scope": {"stacks": ["media"], "services": ["media/qbittorrent"]}, "checks": [{"id": "up", "check": "container_state", "params": {"container": "gluetun"}}]}]}


class Broker:
    def __init__(self, tokens=0):
        self.caps, self.tokens = {}, tokens

    def set_mission_cap(self, mission_id, tokens):
        self.caps[mission_id] = tokens

    def clear_mission_cap(self, mission_id):
        pass

    def usage(self, session_id):
        return {"requests": 3, "tokens": self.tokens, "refused": 0}


class Rig:
    def __init__(self, tmp_path, *, broker=None, budget=150_000, cfg=None, text="why is the media stack slow?"):
        self.store, _ = make_store(tmp_path)
        self.ms = MissionStore(self.store)
        self.console = ConsoleStore(self.store)
        self.mission = self.ms.create_mission(title="t", origin="operator", grant=Grant.model_validate({"tier": "observe"}), goal="g",
                                              success_criteria="s", runtime="console")
        self.conv = self.console.create(self.mission.id, title=text, origin="operator", token_budget=budget)
        self.msg = self.console.add_message(self.conv.id, "operator", text)
        self.cont = FakeContainment()
        self.cfg = cfg or MissionConsoleConfig()
        self.broker = broker
        self.runtime = ConsoleRuntime(self.cont, self.console, self.cfg, worker_socket="/run/w.sock", broker=broker,
                                      env_policy=lambda grant, mid, ws, env: {**env, "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "FROM_POLICY": "1"})
        self.root = tmp_path / "ws"
        self.n = 0
        self.spawned = []

    def start(self, *, sid=None):
        self.n += 1
        sid = sid or f"S-{self.n}"
        workspace = self.root / sid
        workspace.mkdir(parents=True, exist_ok=True)
        cs = self.cont.launch(sid, LaunchSpec(uid=60000, cwd=str(workspace), argv=("/x/claude",), token="t" * 40))

        def spawn(argv, env=None, cwd=None, extra=(), state_dir=None):
            self.spawned.append({"argv": argv, "env": env, "cwd": cwd, "state_dir": state_dir})
            return 4242
        context = RunContext(brief="THE BRIEF", session=cs, token_fd=3, env={}, cwd=str(workspace), spawn=spawn)
        return self.runtime.start(self.mission, context, Grant.model_validate({"tier": "observe"})), workspace, cs

    def finish(self, handle, cs, stream=None):
        stream = FIXTURE.read_bytes() if stream is None else stream
        self.cont.write_output(cs.session_id, stream)
        self.cont.crash(cs.session_id)                        # the process ends
        update = self.runtime.status(handle)
        return update, self.runtime.collect(handle) if update.state == "finished" else None


def test_the_first_turn_is_launched_as_the_agent_with_a_new_session_id_in_its_state_directory(tmp_path):
    rig = Rig(tmp_path, broker=Broker())
    _handle, workspace, cs = rig.start()
    spawned = rig.spawned[0]
    argv, env = spawned["argv"], spawned["env"]
    assert argv[0] == "/usr/local/bin/claude" and argv[1] == "-p" and argv[-2:] == ["--", "why is the media stack slow?"]    # the message is last, after `--`
    assert len(argv[argv.index("--session-id") + 1]) == 36 and "--resume" not in argv
    assert rig.console.get(rig.conv.id).agent_session_id is None                 # stored only once the stream has confirmed it
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Grep,Glob,Write" and argv[argv.index("--disallowedTools") + 1].startswith("WebFetch")
    assert "Edit" not in argv[argv.index("--allowedTools") + 1] and "stream-json" in argv and "--include-partial-messages" in argv
    state = str(tmp_path / "ws" / rig.conv.state_dir)
    assert spawned["state_dir"] == rig.conv.state_dir and spawned["cwd"] == str(workspace)
    assert env["HOME"] == state and env["CLAUDE_CONFIG_DIR"] == f"{state}/cfg" and env["FROM_POLICY"] == "1" and env["PE_SESSION_TOKEN_FD"] == "3"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192" and "ANTHROPIC_API_KEY" not in env
    assert (workspace / "BRIEF.md").read_text() == "THE BRIEF" and "plan.json" in (workspace / "PLAN_FORMAT.md").read_text()
    assert oct((workspace / "BRIEF.md").stat().st_mode & 0o777) == "0o400"
    assert rig.broker.caps[rig.mission.id] == 150_000                      # the turn may spend what is left of the conversation (set before the session exists)
    assert rig.console.get(rig.conv.id).turns == 1 and rig.console.pending(rig.conv.id) is None


def test_a_later_turn_resumes_the_id_the_last_stream_reported_and_every_prompt_argument_fits_the_launcher(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=4000))
    handle, _, cs = rig.start()
    rig.finish(handle, cs)
    assert rig.console.get(rig.conv.id).agent_session_id == "2dc11a5c-9cef-4ce6-98ca-232a0476de7a"      # the stream's id, not ours
    rig.console.add_message(rig.conv.id, "operator", "and the second thing?")
    rig.start()
    argv = rig.spawned[1]["argv"]
    assert argv[argv.index("--resume") + 1] == "2dc11a5c-9cef-4ce6-98ca-232a0476de7a" and "--session-id" not in argv
    assert all(len(a) <= 4096 for a in argv) and rig.console.get(rig.conv.id).turns == 2


def test_a_long_message_travels_as_a_file_not_as_an_argument(tmp_path):
    long = "please look at this log: " + "x" * (MAX_ARG_PROMPT + 100)
    rig = Rig(tmp_path, text=long)
    _, workspace, _ = rig.start()
    argv = rig.spawned[0]["argv"]
    assert "MESSAGE.md" in argv[-1] and len(argv[-1]) < 300 and (workspace / "MESSAGE.md").read_text().startswith("please look at this log")


def test_a_finished_turn_keeps_the_reply_the_session_id_the_events_and_charges_the_brokers_count(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=4106))
    handle, _, cs = rig.start()
    update, result = rig.finish(handle, cs)
    assert update.state == "finished" and result.diagnosis == "all done here" and result.confidence == "medium"
    assert any(e.kind == "note" and e.body.get("call") == "Bash" for e in update.events) and not any(e.kind == "tool" for e in update.events)
    messages = rig.console.messages(rig.conv.id)
    assert [(m.role, m.text, m.turn) for m in messages] == [("operator", "why is the media stack slow?", None), ("agent", "all done here", 1)]
    conv = rig.console.get(rig.conv.id)
    assert conv.tokens_used == 4106 and conv.tokens_left == 150_000 - 4106       # the broker's number, not the stream's own


def test_an_interrupted_turn_keeps_what_it_said_and_says_it_was_stopped(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=900))
    handle, _, cs = rig.start()
    stream = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "bbbbbbbb-2222-3333",
                         "result": "I was looking at the gateway when", "usage": {"input_tokens": 5}}).encode()
    update, result = rig.finish(handle, cs, stream)
    assert update.state == "finished" and result.confidence == "low"
    texts = [(m.role, m.text) for m in rig.console.messages(rig.conv.id)]
    assert ("agent", "I was looking at the gateway when") in texts and ("system", "This turn was stopped before it finished.") in texts
    assert rig.console.get(rig.conv.id).agent_session_id == "bbbbbbbb-2222-3333"


def test_a_turn_with_no_result_line_is_a_failed_turn_and_the_message_stays_taken(tmp_path):
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    update, result = rig.finish(handle, cs, b'{"type":"system","subtype":"init","session_id":"aaaaaaaa-1111"}\n')
    assert update.state == "failed" and result is None


def test_a_valid_plan_file_is_recorded_and_a_refused_one_is_reported_without_echoing_it(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=1))
    handle, workspace, cs = rig.start()
    (workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    rig.finish(handle, cs)
    plan = rig.console.current_plan(rig.conv.id)
    assert plan["turn"] == 1 and json.loads(plan["plan_json"])["options"][0]["name"] == "n" and plan["plan_hash"] == plan_mod.plan_hash(plan_mod.parse(GOOD_PLAN))
    rig.console.add_message(rig.conv.id, "operator", "another")
    handle2, workspace2, cs2 = rig.start()
    bad = json.loads(json.dumps(GOOD_PLAN))
    bad["options"][0]["checks"][0]["check"] = "SECRETSHELL"
    (workspace2 / "plan.json").write_text(json.dumps(bad))
    rig.finish(handle2, cs2)
    refused = [m.text for m in rig.console.messages(rig.conv.id) if m.role == "system"]
    assert refused and refused[-1].startswith("The agent's plan was refused") and "SECRETSHELL" not in refused[-1]
    assert rig.console.current_plan(rig.conv.id)["turn"] == 1                  # the earlier valid plan is still the pending one


def test_a_planted_plan_file_from_a_previous_attempt_is_not_read_again(tmp_path):
    rig = Rig(tmp_path)
    rig.n = 5
    workspace = rig.root / "S-6"
    workspace.mkdir(parents=True)
    (workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    handle, _, cs = rig.start(sid="S-6")                                          # the same workspace is reused: the file is cleared at start
    assert not (workspace / "plan.json").exists()
    rig.finish(handle, cs)
    assert rig.console.current_plan(rig.conv.id) is None


def test_a_closed_conversation_a_spent_budget_or_no_message_refuses_the_turn_before_anything_is_spawned(tmp_path):
    spent = Rig(tmp_path / "a", budget=19_999)
    with pytest.raises(TurnRefused, match="budget"):
        spent.start()
    closed = Rig(tmp_path / "b")
    closed.console.set_state(closed.conv.id, "archiving")
    with pytest.raises(TurnRefused, match="closed"):
        closed.start()
    quiet = Rig(tmp_path / "c")
    quiet.console.consume(quiet.msg, 1)
    with pytest.raises(TurnRefused, match="no message"):
        quiet.start()
    for rig in (spent, closed, quiet):
        assert rig.spawned == []
    assert not any(m.role == "system" for m in spent.console.messages(spent.conv.id))            # the service says why, once (turn_ended)
    assert spent.console.get(spent.conv.id).turns == 0 and spent.console.pending(spent.conv.id) is not None


def test_a_spawn_that_fails_leaves_the_message_waiting(tmp_path):
    rig = Rig(tmp_path)
    workspace = rig.root / "S-1"
    workspace.mkdir(parents=True)
    cs = rig.cont.launch("S-1", LaunchSpec(uid=60000, cwd=str(workspace), argv=("/x/claude",), token="t" * 40))

    def broken(*a, **kw):
        raise OSError("no")
    with pytest.raises(OSError):
        rig.runtime.start(rig.mission, RunContext(brief="b", session=cs, token_fd=3, env={}, cwd=str(workspace), spawn=broken),
                          Grant.model_validate({"tier": "observe"}))
    assert rig.console.pending(rig.conv.id) is not None


def test_the_secrets_in_the_stream_never_reach_the_record(tmp_path):
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    secret = "sk-or-v1-" + "a" * 48
    stream = json.dumps({"type": "result", "subtype": "success", "result": f"found OPENROUTER_API_KEY={secret} in the env", "usage": {}}).encode()
    update, _ = rig.finish(handle, cs, stream)
    blob = json.dumps([m.text for m in rig.console.messages(rig.conv.id)]) + json.dumps([e.body for e in update.events])
    assert secret not in blob


def test_cancel_kills_the_session_and_releases_the_pipe(tmp_path):
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    rig.runtime.cancel(handle)
    assert rig.cont.is_empty(cs) and handle.run_id not in rig.runtime._runs
    rig.runtime.cancel(handle)                                                    # idempotent


def test_collect_refuses_while_the_session_is_not_provably_empty(tmp_path):
    rig = Rig(tmp_path)
    handle, _, _ = rig.start()
    with pytest.raises(RuntimeError, match="provably empty"):
        rig.runtime.collect(handle)
    assert os.path.exists(rig.root)


# ── regressions from the second review ───────────────────────────────────────────────────────────

def test_the_real_env_policy_cannot_move_the_agents_home_or_drop_the_output_cap(tmp_path):
    """build_session_env forces HOME to the workspace and drops anything named like a token: the Console's own values go in after it."""
    from planet_express.missions.grants import build_session_env
    from planet_express.missions.protected import ProtectedSet
    rig = Rig(tmp_path)

    def real_policy(grant, mission_id, workspace, env):
        return dict(build_session_env(grant, mission_id=mission_id, workspace=workspace, worker_socket="/run/w.sock", stacks_root=None,
                                      protected=ProtectedSet(host="local", resources=(), domains={}), base_env=env, known_tools=[]).env)
    rig.runtime.env_policy = real_policy
    _, workspace, _ = rig.start()
    env = rig.spawned[0]["env"]
    state = str(tmp_path / "ws" / rig.conv.state_dir)
    assert env["HOME"] == state and env["CLAUDE_CONFIG_DIR"] == f"{state}/cfg" and env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    assert env["HOME"] != str(workspace)


@pytest.mark.parametrize("message", ["--", "- a bullet about the logs", "--verbose", "--settings=/tmp/evil.json", "--setting-sources=project",
                                     "--resume x", "-p"])
def test_whatever_the_operator_types_it_is_the_last_argument_after_the_terminator_and_the_forced_flags_still_lead(tmp_path, message):
    from planet_express.missions.hooks import claude_code_argv
    rig = Rig(tmp_path, text=message)
    rig.start()
    argv = rig.spawned[0]["argv"]
    assert argv[-2:] == ["--", message] and argv.count("--") == (2 if message == "--" else 1)
    forced = claude_code_argv(argv, tmp_path / "hooks")
    hook_settings = str(tmp_path / "hooks" / "settings.json")
    assert forced[:5] == ["/usr/local/bin/claude", "--settings", hook_settings, "--setting-sources", ""]
    assert forced[-2:] == ["--", message]                                     # nothing the operator typed was read as an option
    for flag in ("--session-id", "--tools", "--allowedTools", "--disallowedTools", "--max-turns", "--output-format"):
        assert flag in forced[:forced.index("--", 5)]


def test_a_turn_that_is_killed_is_still_charged_and_keeps_what_it_said(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=7000))
    handle, _, cs = rig.start()
    rig.cont.write_output(cs.session_id, json.dumps({"type": "assistant", "session_id": "dddddddd-4444-5555", "message": {"content": [
        {"type": "text", "text": "halfway through the logs"}]}}).encode() + b"\n")
    rig.runtime.cancel(handle)                                                   # a budget, a crash or a tamper ends it this way
    conv = rig.console.get(rig.conv.id)
    assert conv.tokens_used == 7000 and conv.agent_session_id == "dddddddd-4444-5555"
    texts = [(m.role, m.text) for m in rig.console.messages(rig.conv.id)]
    assert ("agent", "halfway through the logs") in texts and ("system", "This turn was stopped before it finished.") in texts


def test_a_symlinked_or_unreadable_plan_file_is_a_refused_plan_and_the_turn_is_still_charged(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=3000))
    handle, workspace, cs = rig.start()
    os.symlink("/etc/passwd", workspace / "plan.json")
    rig.finish(handle, cs)
    assert rig.console.get(rig.conv.id).tokens_used == 3000                      # an agent cannot make its turn free
    assert any(m.role == "system" and m.text.startswith("The agent's plan was refused") for m in rig.console.messages(rig.conv.id))
    assert rig.console.current_plan(rig.conv.id) is None


def test_more_than_the_per_poll_event_limit_never_fails_the_turn_and_keeps_every_call(tmp_path):
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    calls = "\n".join(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i), "name": "Read",
                                                                                   "input": {"file_path": f"/a/{i}"}}]}}) for i in range(270))
    rig.cont.write_output(cs.session_id, (calls + "\n" + json.dumps({"type": "result", "subtype": "success", "result": "done",
                                                                      "usage": {}})).encode())     # one poll's worth: under the pipe's 64 KB
    rig.cont.crash(cs.session_id)
    update = rig.runtime.status(handle)                                              # AgentUpdate allows 256; a longer list would raise
    assert update.state == "finished" and len(update.events) == 256
    assert sum(1 for e in update.events if "call" in e.body) == 255 and update.events[-1].body == {"omitted": 270 - 255}


def test_a_very_large_stream_still_ends_with_its_result_line(tmp_path):
    """A turn can print more than any byte cap we might pick: the result line at the end must still be read."""
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    delta = (json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "t" * 400}}})
             + "\n").encode()
    chunk = delta * 120                                                              # about 50 KB: under the fake pipe's capacity
    for _ in range(200):                                                             # about 10 MB in all
        rig.cont.write_output(cs.session_id, chunk)
        assert rig.runtime.status(handle).state == "running"
    rig.cont.write_output(cs.session_id, json.dumps({"type": "result", "subtype": "success", "result": "the end", "usage": {}}).encode())
    rig.cont.crash(cs.session_id)
    assert rig.runtime.status(handle).state == "finished" and rig.runtime.collect(handle).diagnosis == "the end"


def test_a_killed_turns_plan_is_not_offered_and_a_start_that_fails_leaves_no_cap_behind(tmp_path):
    class CapBroker(Broker):
        def __init__(self):
            super().__init__(tokens=500)
            self.cleared = []

        def clear_mission_cap(self, mission_id):
            self.cleared.append(mission_id)
    rig = Rig(tmp_path, broker=CapBroker())
    handle, workspace, cs = rig.start()
    (workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    rig.runtime.cancel(handle)                                                      # killed (a tamper, a crash, a budget)
    assert rig.console.current_plan(rig.conv.id) is None and rig.broker.cleared == [rig.mission.id]
    assert rig.console.get(rig.conv.id).tokens_used == 500                          # but it is still charged
    rig.console.add_message(rig.conv.id, "operator", "again")
    workspace2 = rig.root / "S-9"
    workspace2.mkdir(parents=True)
    cs2 = rig.cont.launch("S-9", LaunchSpec(uid=60000, cwd=str(workspace2), argv=("/x/claude",), token="t" * 40))

    def broken(*a, **kw):
        raise OSError("the launcher refused")
    with pytest.raises(OSError):
        rig.runtime.start(rig.mission, RunContext(brief="b", session=cs2, token_fd=3, env={}, cwd=str(workspace2), spawn=broken),
                          Grant.model_validate({"tier": "observe"}))
    assert rig.broker.cleared == [rig.mission.id, rig.mission.id]                    # the failed start released its ceiling


def test_events_beyond_the_per_poll_limit_are_carried_to_the_next_poll_while_the_turn_runs(tmp_path):
    rig = Rig(tmp_path)
    handle, _, cs = rig.start()
    calls = "\n".join(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i), "name": "Read",
                                                                                   "input": {"file_path": f"/a/{i}"}}]}}) for i in range(270))
    rig.cont.write_output(cs.session_id, calls.encode() + b"\n")
    first = rig.runtime.status(handle)
    second = rig.runtime.status(handle)
    assert first.state == second.state == "running" and len(first.events) == 256 and len(second.events) == 14      # nothing lost while running
    assert [e.body["args"]["file_path"] for e in first.events + second.events] == [f"/a/{i}" for i in range(270)]


def test_a_request_still_in_flight_when_the_turn_is_cut_is_charged_by_what_it_was_promised(tmp_path):
    class InFlight(Broker):
        def usage(self, session_id):
            return {"requests": 1, "tokens": 1000, "refused": 0, "reserved": 8500}       # the handler has not finalised it yet
    rig = Rig(tmp_path, broker=InFlight())
    handle, _, cs = rig.start()
    rig.cont.write_output(cs.session_id, b'{"type":"assistant","message":{"content":[{"type":"text","text":"partway"}]}}\n')
    rig.runtime.cancel(handle)
    assert rig.console.get(rig.conv.id).tokens_used == 9500                              # conservative: never an undercount


def test_an_interrupted_turn_never_publishes_the_plan_it_had_written(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=100))
    handle, workspace, cs = rig.start()
    (workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    rig.cont.write_output(cs.session_id, b'{"type":"assistant","message":{"content":[{"type":"text","text":"drafting"}]}}\n')
    rig.cont.crash(cs.session_id)                                                         # the stream ends with no result line: interrupted
    assert rig.runtime.status(handle).state == "failed"
    result = rig.runtime.collect(handle)                                                  # the supervisor's interrupt path collects it anyway
    assert result.confidence == "low" and rig.console.current_plan(rig.conv.id) is None


class Inv:
    stacks = ("media",)
    services = ("media/qbittorrent",)


def test_the_agent_is_given_the_inventory_and_a_plan_naming_anything_else_is_refused(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=1))
    rig.runtime.inventory = lambda: Inv
    handle, workspace, cs = rig.start()
    inventory = (workspace / "INVENTORY.md").read_text()
    assert "- media\n" in inventory and "- media/qbittorrent\n" in inventory and oct((workspace / "INVENTORY.md").stat().st_mode & 0o777) == "0o400"
    bad = json.loads(json.dumps(GOOD_PLAN))
    bad["options"][0]["scope"] = {"stacks": ["not-a-stack"], "services": []}
    (workspace / "plan.json").write_text(json.dumps(bad))
    rig.finish(handle, cs)
    assert rig.console.current_plan(rig.conv.id) is None
    assert any("does not have" in m.text for m in rig.console.messages(rig.conv.id) if m.role == "system")


def test_with_no_inventory_the_agent_is_told_it_cannot_name_a_scope(tmp_path):
    rig = Rig(tmp_path)
    _, workspace, _ = rig.start()
    assert "do not write plan.json" in (workspace / "INVENTORY.md").read_text()
    rig.runtime.inventory = lambda: (_ for _ in ()).throw(OSError("unreadable"))
    assert "do not write plan.json" in rig.runtime._inventory_text()                       # an unreadable inventory is no inventory, never a wider one
