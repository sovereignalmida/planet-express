"""Shared loop contracts and provider wire-format regression checks."""
from copy import deepcopy

import pytest
from test_diagnostic_evidence import (
    _an,
    _an_text,
    _an_tool,
    _install_anthropic,
    _install_openai,
    _oa_calls,
    _oa_text,
)

import casa_farnsworth as farnsworth
from planet_express.execution.tool_loop import ToolCall, Turn, run_tool_loop


class FakeAdapter:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = 0
        self.records = []

    def request(self):
        self.requests += 1
        return next(self.turns)

    def record(self, turn, results):
        self.records.append((turn, results))


def test_no_calls_logs_text_records_and_returns_none():
    turn = Turn("untrusted text", [])
    adapter = FakeAdapter([turn])
    logged = []

    def execute(command, count):
        pytest.fail("no calls should execute")

    assert run_tool_loop(adapter, execute, max_calls=5, log_text=logged.append) is None
    assert logged == ["untrusted text"]
    assert adapter.requests == 1
    assert adapter.records == [(turn, [])]


@pytest.mark.parametrize("sizes", [(5,), (2, 3)])
def test_shared_budget_rejects_rest_of_turn_and_stops(sizes):
    calls = [ToolCall(str(i), f"command {i}") for i in range(sum(sizes))]
    turns = []
    offset = 0
    for size in sizes:
        turns.append(Turn(f"text {offset}", calls[offset:offset + size]))
        offset += size
    adapter = FakeAdapter(turns)
    attempts = []
    logged = []

    def execute(command, count):
        attempts.append((command, count))
        return ("ran", count + 1) if count < 3 else ("rejected", count)

    run_tool_loop(adapter, execute, max_calls=3, log_text=logged.append)
    assert attempts == [(call.command, min(i, 3)) for i, call in enumerate(calls)]
    assert adapter.requests == len(sizes)
    assert logged == [turn.text for turn in turns]
    assert adapter.records == [
        (turn, [(call, "ran" if int(call.id) < 3 else "rejected") for call in turn.calls])
        for turn in turns
    ]


@pytest.mark.parametrize("cap", [0, 1, 3])
def test_request_cap_even_if_executor_does_not_spend_budget(cap):
    turn = Turn("", [ToolCall("id", "command")])
    adapter = FakeAdapter([turn] * (cap + 1))
    run_tool_loop(adapter, lambda cmd, n: ("rejected", n), max_calls=cap, log_text=lambda t: None)
    assert adapter.requests == cap
    assert len(adapter.records) == cap


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_provider_two_turn_request_kwargs(monkeypatch, provider):
    monkeypatch.setattr(farnsworth.config, "model_for", lambda tier: "fixture-model")
    if provider == "anthropic":
        first = _an(_an_text("checking"), _an_tool("docker ps"))
        last = _an(_an_text("done"))
        requests = _install_anthropic(monkeypatch, [first, last])
        adapter = farnsworth._AnthropicDiagnosticAdapter("[]")
        endpoint = adapter.client.messages
    else:
        first = _oa_calls("docker ps", text="checking")
        last = _oa_text("done")
        requests = _install_openai(monkeypatch, [first, last])
        adapter = farnsworth._OpenAIDiagnosticAdapter("[]")
        endpoint = adapter.client.responses
    create = endpoint.create
    snapshots = []

    def capture(**kwargs):
        # The existing Anthropic fake retains a reference to the mutable messages list.
        snapshots.append(deepcopy(kwargs))
        return create(**kwargs)

    monkeypatch.setattr(endpoint, "create", capture)
    run_tool_loop(adapter, lambda cmd, n: ("result", n + 1), max_calls=5, log_text=lambda t: None)
    assert len(requests) == len(snapshots) == 2
    user = {"role": "user", "content": "Findings:\n[]"}
    if provider == "anthropic":
        base = {"model": "fixture-model", "max_tokens": farnsworth.DIAGNOSTIC_MAX_TOKENS,
                    "system": farnsworth.DIAGNOSTIC_SYSTEM_PROMPT,
                    "tools": [farnsworth.DIAGNOSTIC_TOOL_SCHEMA]}
        history = [user, {"role": "assistant", "content": first.content},
                   {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "t0", "content": "result"}]}]
        assert snapshots == [dict(base, messages=[user]), dict(base, messages=history)]
        assert adapter.messages == history + [{"role": "assistant", "content": last.content}]
    else:
        base = {"model": "fixture-model", "reasoning": {"effort": "low"},
                    "max_output_tokens": farnsworth.DIAGNOSTIC_MAX_TOKENS,
                    "tools": [{"type": "function", "name": "run_diagnostic",
                            "description": farnsworth.DIAGNOSTIC_TOOL_SCHEMA["description"],
                            "parameters": farnsworth.DIAGNOSTIC_TOOL_SCHEMA["input_schema"]}]}
        system = {"role": "system", "content": farnsworth.DIAGNOSTIC_SYSTEM_PROMPT}
        history = [user] + first.output + [
            {"type": "function_call_output", "call_id": "c0", "output": "result"}]
        assert snapshots == [dict(base, input=[system, user]), dict(base, input=[system] + history)]
        assert adapter.input_items == history
