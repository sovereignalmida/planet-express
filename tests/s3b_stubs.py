"""Local stand-ins for S3a's interface modules, installed only when the real ones are absent.

S3b is built in parallel with S3a (containment.py / runtime.py / sessions.py). The shapes below
follow docs/handoff/v4/S3-common.md exactly; once S3a is merged the real modules are imported and
this file installs nothing. Import this module BEFORE any planet_express.missions.* S3b module.
"""

import importlib
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("CASA_CONFIG", str(_ROOT / "config.example.yaml"))


def _have(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ModuleNotFoundError:
        return False


def install() -> None:
    if not _have("planet_express.missions.containment"):
        mod = types.ModuleType("planet_express.missions.containment")

        @dataclass(frozen=True)
        class ContainedSession:
            session_id: str
            kind: Literal["local_cgroup", "local_uid", "fake"]
            cgroup_path: str | None
            session_uid: int | None

        class SessionContainment(Protocol):
            def create(self, session_id): ...
            def spawn(self, cs, argv, env, cwd, pass_fds=()): ...
            def pids(self, cs): ...
            def freeze(self, cs): ...
            def thaw(self, cs): ...
            def kill(self, cs): ...
            def is_empty(self, cs): ...
            def cleanup(self, cs): ...

        mod.ContainedSession = ContainedSession
        mod.SessionContainment = SessionContainment
        sys.modules[mod.__name__] = mod
    if not _have("planet_express.missions.runtime"):
        mod = types.ModuleType("planet_express.missions.runtime")

        class AgentResult(BaseModel):
            model_config = ConfigDict(extra="forbid")
            diagnosis: str = Field(max_length=8000)
            evidence: list[str] = Field(default_factory=list, max_length=50)
            suggested_actions: list[str] = Field(default_factory=list, max_length=50)
            verification_plan: str | None = Field(default=None, max_length=8000)
            memory_candidates: list[str] = Field(default_factory=list, max_length=50)
            unknowns: list[str] = Field(default_factory=list, max_length=50)
            confidence: float = Field(ge=0, le=1)

        @dataclass(frozen=True)
        class AgentUpdate:
            kind: str
            text: str = ""
            running: bool = True

        @dataclass
        class RunHandle:
            run_id: str
            mission_id: str
            extra: dict = field(default_factory=dict)

        class AgentRuntime(Protocol):
            def start(self, mission, context, grant): ...
            def status(self, handle): ...
            def cancel(self, handle): ...
            def collect(self, handle): ...

        for obj in (AgentResult, AgentUpdate, RunHandle, AgentRuntime):
            setattr(mod, obj.__name__, obj)
        sys.modules[mod.__name__] = mod


install()
