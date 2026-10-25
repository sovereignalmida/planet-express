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
    "rolls_back_to": "r", "effort": "e", "checks": [{"id": "up", "check": "container_state", "params": {"container": "gluetun"}}]}]}


class Broker:
    def __init__(self, tokens=0, known=True):
        self.caps, self.tokens, self.known = {}, tokens, known

    def set_session_cap(self, session_id, tokens):
        self.caps[session_id] = tokens
        return self.known

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
    assert argv[0] == "/usr/local/bin/claude" and argv[1:3] == ["-p", "why is the media stack slow?"]
    assert argv[argv.index("--session-id") + 1] == rig.console.get(rig.conv.id).agent_session_id and "--resume" not in argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Grep,Glob,Write" and argv[argv.index("--disallowedTools") + 1].startswith("WebFetch")
    assert "Edit" not in argv[argv.index("--allowedTools") + 1] and "stream-json" in argv and "--include-partial-messages" in argv
    state = str(tmp_path / "ws" / rig.conv.state_dir)
    assert spawned["state_dir"] == rig.conv.state_dir and spawned["cwd"] == str(workspace)
    assert env["HOME"] == state and env["CLAUDE_CONFIG_DIR"] == f"{state}/cfg" and env["FROM_POLICY"] == "1" and env["PE_SESSION_TOKEN_FD"] == "3"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8192" and "ANTHROPIC_API_KEY" not in env
    assert (workspace / "BRIEF.md").read_text() == "THE BRIEF" and "plan.json" in (workspace / "PLAN_FORMAT.md").read_text()
    assert oct((workspace / "BRIEF.md").stat().st_mode & 0o777) == "0o400"
    assert rig.broker.caps[cs.session_id] == 150_000                       # the turn may spend what is left of the conversation
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
    assert "MESSAGE.md" in argv[2] and len(argv[2]) < 300 and (workspace / "MESSAGE.md").read_text().startswith("please look at this log")


def test_a_finished_turn_keeps_the_reply_the_session_id_the_events_and_charges_the_brokers_count(tmp_path):
    rig = Rig(tmp_path, broker=Broker(tokens=4106))
    handle, _, cs = rig.start()
    update, result = rig.finish(handle, cs)
    assert update.state == "finished" and result.diagnosis == "all done here" and result.confidence == "medium"
    assert any(e.kind == "tool" and e.body["call"] == "Bash" for e in update.events)
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
    nobody = Rig(tmp_path / "d", broker=Broker(known=False))
    with pytest.raises(TurnRefused, match="broker"):
        nobody.start()
    for rig in (spent, closed, quiet, nobody):
        assert rig.spawned == []
    assert nobody.console.get(nobody.conv.id).turns == 0 and nobody.console.pending(nobody.conv.id) is not None   # still waiting


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
