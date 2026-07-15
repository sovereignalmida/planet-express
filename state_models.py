"""
state_models.py — versioned models for Planet Express's on-disk state files.

Producers write through these models at the file boundary (right before the
write_text() call) rather than restructuring producer internals — the goal is a
validated, versioned contract so a future consumer (the read-only dashboard) can
detect a shape change via schema_version instead of a silent KeyError, not an
exhaustive schema of every nested dict.

Findings/PlanSet come from LLM output and use extra="allow" -- Hermes/Farnsworth
already have documented fallback paths for malformed LLM JSON, and machine-generated
content should tolerate shape drift rather than crash the pipeline. MonitorSnapshot/
RunStatus/RollbackCandidates/UpdateHistory are internally-produced and stay strict.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class MonitorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    timestamp: str
    agent: str = "casa_leela"
    mode: Literal["full", "status", "updates"] = "full"
    containers: list[dict] = []
    stack_completeness: list[dict] = []
    disk: list[dict] = []
    docker_disk: dict = {}
    mounts: dict = {}
    system: dict = {}
    backups: dict = {}
    services: dict = {}
    image_candidates: list[dict] = []
    certs: list[dict] = []


class Findings(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    analyzed_at: str
    findings: list[dict] = []
    has_critical: bool = False
    has_high: bool = False
    update_candidates: list[dict] = []


class PlanSet(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    planned_at: str
    plans: list[dict] = []
    expires_at: Optional[str] = None


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    state: str
    pending_plan_id: Optional[str] = None
    pending_msg_id: Optional[int] = None
    updated_at: str


class RollbackCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stack: str
    service: str
    old_image_id: str
    recorded_at: str
    expires_at: str


class RollbackCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    candidates: list[RollbackCandidate] = []


class UpdateHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str
    stack: str
    service: str
    old_id: str
    new_id: str
    status: str
    reason: Optional[str] = None


class UpdateHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    entries: list[UpdateHistoryEntry] = []
