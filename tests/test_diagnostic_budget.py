"""
Unit tests for casa_farnsworth.py's diagnostic call-budget enforcement
(_diagnostic_tool_output). Pure logic -- no LLM SDK calls, no real host.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import casa_farnsworth as farnsworth


def test_executes_and_increments_under_budget(monkeypatch):
    monkeypatch.setattr(farnsworth, "_run_diagnostic_tool", lambda cmd: f"ran: {cmd}")
    output, calls_made = farnsworth._diagnostic_tool_output("docker ps", 0)
    assert output == "ran: docker ps"
    assert calls_made == 1


def test_rejects_without_executing_once_budget_exhausted(monkeypatch):
    called = []
    monkeypatch.setattr(farnsworth, "_run_diagnostic_tool", lambda cmd: called.append(cmd))
    output, calls_made = farnsworth._diagnostic_tool_output(
        "docker ps", farnsworth.MAX_DIAGNOSTIC_ROUNDS
    )
    assert output.startswith("REJECTED: diagnostic call budget exhausted")
    assert calls_made == farnsworth.MAX_DIAGNOSTIC_ROUNDS  # unchanged, not incremented
    assert called == []  # never actually ran the command


def test_multiple_calls_in_one_turn_share_the_same_budget(monkeypatch):
    # A single model turn (e.g. several tool_use blocks in one Anthropic response,
    # or several function_calls in one OpenAI response) must not be able to spend
    # more than MAX_DIAGNOSTIC_ROUNDS calls between them.
    monkeypatch.setattr(farnsworth, "_run_diagnostic_tool", lambda cmd: "ok")
    calls_made = 0
    executed = 0
    for _ in range(farnsworth.MAX_DIAGNOSTIC_ROUNDS + 3):
        output, calls_made = farnsworth._diagnostic_tool_output("docker ps", calls_made)
        if output == "ok":
            executed += 1
    assert executed == farnsworth.MAX_DIAGNOSTIC_ROUNDS
    assert calls_made == farnsworth.MAX_DIAGNOSTIC_ROUNDS
