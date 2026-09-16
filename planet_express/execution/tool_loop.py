"""
planet_express/execution/tool_loop.py — bounded, provider-agnostic tool conversations.

Adapters own SDK calls and wire formats; the executor owns per-call budget enforcement.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    command: str


@dataclass
class Turn:
    text: str
    calls: list[ToolCall]
    raw: object = None


class ToolAdapter(Protocol):
    def request(self) -> Turn:
        """Request one model turn with tools attached."""
        ...

    def record(self, turn: Turn, results: list[tuple[ToolCall, str]]) -> None:
        """Record a turn and ordered results, including turns without calls."""
        ...


def run_tool_loop(
    adapter: ToolAdapter,
    execute: Callable[[str, int], tuple[str, int]],
    *,
    max_calls: int,
    log_text: Callable[[str], None],
) -> None:
    # Tools-less summaries fabricated evidence: see the full note above
    # _run_diagnostic_tool in casa_farnsworth.py. Text is only logged, never forwarded.
    calls_made = 0
    for _ in range(max_calls):
        turn = adapter.request()
        log_text(turn.text)
        results = []
        for call in turn.calls:
            output, calls_made = execute(call.command, calls_made)
            results.append((call, output))
        adapter.record(turn, results)
        if not turn.calls or calls_made >= max_calls:
            return
