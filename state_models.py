"""
state_models.py — versioned models for Planet Express's on-disk state files.

Producers write through these models at the file boundary (right before the
write_text() call) rather than restructuring producer internals — the goal is a
validated, versioned contract so a future consumer (the read-only dashboard) can
detect a shape change via schema_version instead of a silent KeyError, not an
exhaustive schema of every nested dict.

Findings comes from LLM output and uses extra="allow" -- Hermes
already have documented fallback paths for malformed LLM JSON, and machine-generated
content should tolerate shape drift rather than crash the pipeline. MonitorSnapshot/
RunStatus/UpdateHistory are internally-produced and stay strict.
"""

from typing import Literal

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
    unraid_exports: dict = {}
    nfs_mount_health: list[dict] = []
    vpn_port_forwarding: dict = {}
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


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    state: str
    # Kept as the pipeline's "what is it busy with" fields: a typed execution puts its id here
    # while it runs. The shell-plan meaning ("a plan card is waiting") went in slice 5b-5.
    pending_plan_id: str | None = None
    pending_msg_id: int | None = None
    updated_at: str


class UpdateHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str
    stack: str
    service: str
    # service_image_id() legitimately returns None (e.g. a stopped service with no
    # container to inspect) -- these must stay optional, not required strings.
    old_id: str | None = None
    new_id: str | None = None
    status: str
    reason: str | None = None


class UpdateHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    entries: list[UpdateHistoryEntry] = []
