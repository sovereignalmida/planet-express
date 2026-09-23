"""Validated, canonical runbook documents for the schema-v5 execution boundary."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

Risk = Literal["R0", "R1", "R2", "R3", "R4"]
RollbackKind = Literal["none", "conditional", "automatic"]
PortNumber = Annotated[int, Field(ge=1, le=65535)]
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RISK_ORDER = {value: index for index, value in enumerate(("R0", "R1", "R2", "R3", "R4"))}


def _no_dotdot(value: str) -> str:
    if ".." in value:
        raise ValueError("must not contain '..'")
    return value


# The approved document validates names on its own, so a stored plan can never carry a path-like or
# shell-looking target even before execution re-resolves it (own review, T38). Same rules as
# actions._NAME_RE / _CONTAINER_NAME_RE.
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"), AfterValidator(_no_dotdot)]
ContainerName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")]
ContainerId = Annotated[str, Field(pattern=r"^[0-9a-f]{12,64}$")]
UnitName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9@_.:-]{1,200}\.(service|timer|mount|socket|target|path)$"),
    AfterValidator(_no_dotdot),
]
AbsolutePath = Annotated[str, Field(pattern=r"^/[^\x00]{1,4095}$"), AfterValidator(_no_dotdot)]
LogMatch = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reference(StrictModel):
    from_step: int = Field(ge=1)
    output: str = Field(min_length=1)


class Empty(StrictModel):
    pass


class ServiceParams(StrictModel):
    stack: Name
    service: Name


class StackParams(StrictModel):
    stack: Name


class WaitParams(StrictModel):
    seconds: int = Field(ge=1, le=300)


class CheckContainerParams(StrictModel):
    expect: Literal["running", "healthy", "stopped"]


class CheckLogParams(StrictModel):
    match: LogMatch
    port: PortNumber | Reference | None = None


class UnitActionParams(StrictModel):
    action: Literal["start", "stop", "restart"]
    unit: UnitName


class ComposeBinding(StrictModel):
    compose_path: AbsolutePath
    compose_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: Name


class ServiceBinding(ComposeBinding):
    # The Compose service the container belongs to: container-only steps (check.*, read.*) carry no
    # service in their params, so the binding must hold the full identity (Codex review, T38).
    service: Name
    container: ContainerName
    container_id: ContainerId


class StackBinding(ComposeBinding):
    services: list[Name] = Field(min_length=1)


class BoundStack(StrictModel):
    stack: Name
    compose_path: AbsolutePath
    compose_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: Name
    services: list[Name] = Field(min_length=1)


class StackSetBinding(StrictModel):
    stacks: list[BoundStack] = Field(min_length=1)


class UnitBinding(StrictModel):
    unit: UnitName


# An image id as Docker prints it, normalised to bare hex before it is stored (Zoidberg's
# _normalize_image_id: `compose images -q` prints bare hex, `image inspect` prints sha256:<hex>).
ImageId = Annotated[str, Field(pattern=r"^[0-9a-f]{12,64}$")]
# A canonical mutable reference, `name:tag`, optionally registry- and namespace-qualified. A
# digest-pinned reference (`repo@sha256:…`) has no tag to move and is deliberately not matched:
# those services are ineligible for a canary update (design §4.5).
# 512 is the same bound `actions.is_canary_reference` applies: Docker's own 255-character limit is
# on the repository name alone, so a registry-qualified reference with a long tag can legitimately
# be longer. The two must agree, or a deploy that succeeded would fail output validation and be
# recorded as a crash (Codex, T42).
IMAGE_REFERENCE_MAX = 512
ImageReference = Annotated[
    str,
    Field(min_length=3, max_length=IMAGE_REFERENCE_MAX,
          pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*:[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$"),
    AfterValidator(_no_dotdot),
]


class StackServiceOutput(StrictModel):
    project: Name
    service: Name
    container_name: ContainerName
    container_id: ContainerId


StackServiceOutputs = Annotated[list[StackServiceOutput], Field(min_length=1)]


@dataclass(frozen=True)
class StepType:
    params_model: type[BaseModel]
    binding_model: type[BaseModel]
    risk: Risk
    rollback: RollbackKind
    outputs: dict[str, Any]
    reference_fields: dict[str, Any]
    target: str
    rollback_by_action: dict[str, RollbackKind] | None = None

    def rollback_for(self, params: dict[str, Any]) -> RollbackKind:
        if self.rollback_by_action is None:
            return self.rollback
        return self.rollback_by_action.get(params.get("action"), self.rollback)


STEP_TYPES: dict[str, StepType] = {
    "service.restart": StepType(ServiceParams, ServiceBinding, "R1", "none", {}, {}, "service"),
    "service.start": StepType(ServiceParams, ServiceBinding, "R1", "conditional", {}, {}, "service"),
    "service.stop": StepType(ServiceParams, ServiceBinding, "R2", "conditional", {}, {}, "service"),
    "stack.up": StepType(
        StackParams, StackBinding, "R1", "none",
        {"services": StackServiceOutputs}, {}, "stack",
    ),
    "stack.down": StepType(StackParams, StackBinding, "R2", "none", {}, {}, "stack"),
    "stack.up_all": StepType(Empty, StackSetBinding, "R2", "none", {}, {}, "stack_set"),
    "stack.down_all": StepType(Empty, StackSetBinding, "R3", "none", {}, {}, "stack_set"),
    "stack.down_ingress": StepType(StackParams, StackBinding, "R3", "none", {}, {}, "stack"),
    "wait": StepType(WaitParams, Empty, "R0", "none", {}, {}, "none"),
    "check.container": StepType(CheckContainerParams, ServiceBinding, "R0", "none", {}, {}, "none"),
    "check.log_since_start": StepType(
        CheckLogParams, ServiceBinding, "R0", "none", {}, {"port": PortNumber}, "none",
    ),
    "read.qbittorrent_session_port": StepType(
        Empty, ServiceBinding, "R0", "none", {"port": PortNumber}, {}, "none",
    ),
    "unit.action": StepType(
        UnitActionParams, UnitBinding, "R3", "none", {}, {}, "unit",
        {"start": "conditional", "stop": "conditional"},
    ),
    # D34: automatic under the weekly canary window, with the inverse built into the step itself
    # (retag the recorded old image and bring the service back) rather than offered as a control.
    "update.canary": StepType(
        ServiceParams, ServiceBinding, "R2", "automatic",
        {"image_reference": ImageReference, "old_image_id": ImageId, "new_image_id": ImageId},
        {}, "service",
    ),
    "prune.safe": StepType(Empty, Empty, "R2", "none", {}, {}, "global"),
}


def _check_binding_matches_params(step_type: str, params: dict, binding: dict) -> None:
    """The card and T24 key are derived from params; the engine acts on the binding. They must
    name the same target, or an approval would show one service and touch another (own review,
    T38)."""
    stack = params.get("stack")
    if stack is not None and "compose_path" in binding:
        parts = binding["compose_path"].rstrip("/").split("/")
        if len(parts) < 2 or parts[-2] != stack:
            raise ValueError(f"{step_type}: binding compose_path is not the {stack!r} stack")
    if "unit" in params and "unit" in binding and params["unit"] != binding["unit"]:
        raise ValueError(f"{step_type}: binding unit differs from params")
    service = params.get("service")
    if service is not None and "service" in binding and binding["service"] != service:
        raise ValueError(f"{step_type}: binding service differs from params")
    if service is not None and "services" in binding and service not in binding["services"]:
        raise ValueError(f"{step_type}: service {service!r} is not in the bound services")


class Step(StrictModel):
    type: str
    params: dict[str, Any]
    binding: dict[str, Any]

    @model_validator(mode="after")
    def validate_registered_shape(self) -> Step:
        spec = STEP_TYPES.get(self.type)
        if spec is None:
            raise ValueError(f"unknown step type {self.type!r}")
        params = spec.params_model.model_validate(self.params)
        binding = spec.binding_model.model_validate(self.binding)
        self.params = params.model_dump(mode="json")
        self.binding = binding.model_dump(mode="json")
        _check_binding_matches_params(self.type, self.params, self.binding)
        return self


class Runbook(StrictModel):
    kind: Literal["runbook"] = "runbook"
    version: Literal[1] = 1
    title: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)
    artifacts: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_document_links(self) -> Runbook:
        for index, step in enumerate(self.steps, start=1):
            spec = STEP_TYPES[step.type]
            for field, value in step.params.items():
                if not isinstance(value, dict) or set(value) != {"from_step", "output"}:
                    continue
                reference = Reference.model_validate(value)
                expected_type = spec.reference_fields.get(field)
                if expected_type is None:
                    raise ValueError(f"{step.type}.{field} does not accept a reference")
                if reference.from_step >= index:
                    raise ValueError("references must point to an earlier step")
                producer = STEP_TYPES[self.steps[reference.from_step - 1].type]
                output_type = producer.outputs.get(reference.output)
                if output_type is None:
                    raise ValueError(f"step {reference.from_step} has no output {reference.output!r}")
                if output_type != expected_type:
                    raise ValueError("reference output type does not match the consuming field")

        used_artifacts = set()
        for key, content in self.artifacts.items():
            if _SHA256_RE.fullmatch(key) is None:
                raise ValueError("artifact keys must be lowercase sha256 digests")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != key:
                raise ValueError("artifact content does not match its sha256 key")
            for step in self.steps:
                if step.params.get("content_sha256") == key:
                    used_artifacts.add(key)
        unused = set(self.artifacts) - used_artifacts
        if unused:
            raise ValueError("runbook contains unknown or unused artifacts")
        return self


def canonical_json(runbook: Runbook) -> str:
    return json.dumps(
        runbook.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def plan_sha256(runbook: Runbook) -> str:
    return hashlib.sha256(canonical_json(runbook).encode("utf-8")).hexdigest()


def validate_outputs(step_type: str, outputs: dict[str, Any]) -> dict[str, Any]:
    spec = STEP_TYPES.get(step_type)
    if spec is None:
        raise ValueError(f"unknown step type {step_type!r}")
    if set(outputs) != set(spec.outputs):
        raise ValueError("step outputs do not match the declared schema")
    # Validated, then dumped back to plain JSON values: outputs are persisted with json.dumps
    # and substituted into later steps, so model instances must never escape (own review, T38).
    result = {}
    for name, output_type in spec.outputs.items():
        adapter = TypeAdapter(output_type)
        result[name] = adapter.dump_python(
            adapter.validate_python(outputs[name], strict=True), mode="json"
        )
    return result


def rollback_kind(step: Step) -> RollbackKind:
    return STEP_TYPES[step.type].rollback_for(step.params)


def load_stored_plan(plan_json: str, expected_sha256: str) -> Runbook:
    runbook = Runbook.model_validate_json(plan_json)
    if not isinstance(expected_sha256, str) or not secrets_compare(
        plan_sha256(runbook), expected_sha256
    ):
        raise ValueError("stored runbook hash mismatch")
    return runbook


def secrets_compare(left: str, right: str) -> bool:
    # Kept local so loading a plan never depends on a caller remembering to compare hashes.
    import hmac

    return hmac.compare_digest(left, right)


def risk(runbook: Runbook) -> Risk:
    return max((STEP_TYPES[step.type].risk for step in runbook.steps), key=_RISK_ORDER.__getitem__)


def _target_key(step: Step, spec: StepType) -> str:
    if spec.target == "service":
        return f"{step.params['stack']}/{step.params['service']}"
    if spec.target == "stack":
        return f"stack:{step.params['stack']}"
    if spec.target == "stack_set":
        return "stacks:" + ",".join(item["stack"] for item in step.binding["stacks"])
    if spec.target == "unit":
        return f"unit:{step.params['unit']}"
    return "host"


def mutating_pairs(runbook: Runbook) -> list[tuple[str, str]]:
    result = []
    for step in runbook.steps:
        spec = STEP_TYPES[step.type]
        if spec.risk != "R0":
            result.append((step.type, _target_key(step, spec)))
    return result
