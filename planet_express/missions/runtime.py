"""The agent-runtime adapter protocol and its strict result models (brief §7, contract §11).

The adapter never spawns anything itself: the Supervisor hands it a `RunContext` whose `spawn()` puts the
process inside the session's containment, under the worker identity. Everything an adapter returns is
untrusted data (invariant I1): it is validated here, redacted before persistence, and never evidence of
success or permission to mutate.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .containment import ContainedSession
from .models import Mission, VerificationPlan

Line = Annotated[StrictStr, Field(max_length=512)]
Block = Annotated[StrictStr, Field(max_length=8192)]
MAX_ITEMS = 64
MAX_RESULT_CHARS = 128 * 1024
# What a runtime may write into the event log. State and decision events belong to Core alone.
RUNTIME_EVENT_KINDS = ("tool", "note", "diff")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentResult(_Strict):
    """The child's final JSON. Strict and bounded: unknown keys, floats-as-ints, oversize text all fail."""
    diagnosis: Block
    evidence: Annotated[list[Block], Field(max_length=MAX_ITEMS)] = []
    suggested_actions: Annotated[list[Block], Field(max_length=MAX_ITEMS)] = []
    verification_plan: VerificationPlan | None = None
    memory_candidates: Annotated[list[Block], Field(max_length=MAX_ITEMS)] = []
    unknowns: Annotated[list[Block], Field(max_length=MAX_ITEMS)] = []
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def _bounded_in_total(self) -> "AgentResult":
        # Persistence redacts the whole document at once, and redact() refuses anything over its own cap.
        if len(self.model_dump_json()) > MAX_RESULT_CHARS:
            raise ValueError(f"the result exceeds {MAX_RESULT_CHARS} characters in total")
        return self


class AgentEvent(_Strict):
    kind: Literal["tool", "note", "diff"]
    body: dict[Line, object] = {}


class AgentUpdate(_Strict):
    """One advisory status report. `state` is the adapter's claim; Core's mission row is the truth."""
    state: Literal["running", "finished", "failed"] = "running"
    events: Annotated[list[AgentEvent], Field(max_length=256)] = []
    output_bytes: Annotated[StrictInt, Field(ge=0, le=2**40)] = 0      # bytes of output since the last poll


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    session_id: str
    pid: int | None = None


@dataclass(frozen=True)
class RunContext:
    """What the Supervisor gives `AgentRuntime.start`. `token_fd` is the read end of a pipe holding the
    session token (never argv, never env); `spawn` runs argv inside the session's containment."""
    brief: str
    session: ContainedSession
    token_fd: int
    env: dict[str, str]
    cwd: str
    spawn: Callable[..., int] = field(repr=False)     # spawn(argv, env=None, cwd=None, extra_fds=()) -> pid


class AgentRuntime(Protocol):
    def start(self, mission: Mission, context: RunContext, grant) -> RunHandle: ...
    def status(self, handle: RunHandle) -> AgentUpdate: ...
    def cancel(self, handle: RunHandle) -> None: ...
    def collect(self, handle: RunHandle) -> AgentResult: ...


__all__ = [
    "RUNTIME_EVENT_KINDS", "AgentEvent", "AgentResult", "AgentRuntime", "AgentUpdate", "RunContext", "RunHandle",
]
