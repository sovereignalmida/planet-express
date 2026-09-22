import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution.runbook import (
    Runbook,
    canonical_json,
    load_stored_plan,
    mutating_pairs,
    plan_sha256,
    risk,
    rollback_kind,
    validate_outputs,
)

SHA = "a" * 64


def service_step(kind="service.restart", *, stack="media", service="sonarr"):
    return {
        "type": kind,
        "params": {"stack": stack, "service": service},
        "binding": {
            "compose_path": f"/stacks/{stack}/docker-compose.yml",
            "compose_sha256": SHA,
            "project": stack,
            "service": service,
            "container": f"{stack}-{service}-1",
            "container_id": "0123456789ab",
        },
    }


def document(*steps, **extra):
    return {"kind": "runbook", "version": 1, "title": "Test", "steps": list(steps),
            "artifacts": {}, **extra}


def test_origin_and_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        Runbook.model_validate(document(service_step(), origin="planner"))
    bad = service_step() | {"risk": "R1"}
    with pytest.raises(ValidationError):
        Runbook.model_validate(document(bad))


@pytest.mark.parametrize("step", [
    {"type": "unknown", "params": {}, "binding": {}},
    {"type": "wait", "params": {"seconds": 0}, "binding": {}},
    {"type": "wait", "params": {"seconds": 301}, "binding": {}},
    {"type": "wait", "params": {"seconds": 1, "extra": True}, "binding": {}},
])
def test_unknown_step_and_bad_params_are_rejected(step):
    with pytest.raises(ValidationError):
        Runbook.model_validate(document(step))


@pytest.mark.parametrize("reference", [
    {"from_step": 2, "output": "port"},
    {"from_step": 1, "output": "port"},
])
def test_forward_and_self_references_are_rejected(reference):
    producer = {"type": "read.qbittorrent_session_port", "params": {},
                "binding": service_step()["binding"]}
    consumer = {"type": "check.log_since_start", "params": {"match": "New :", "port": reference},
                "binding": service_step()["binding"]}
    steps = [consumer, producer] if reference["from_step"] == 2 else [consumer]
    with pytest.raises(ValidationError, match="earlier"):
        Runbook.model_validate(document(*steps))


def test_undeclared_output_and_type_mismatch_are_rejected(monkeypatch):
    producer = {"type": "read.qbittorrent_session_port", "params": {},
                "binding": service_step()["binding"]}
    consumer = {"type": "check.log_since_start",
                "params": {"match": "New :", "port": {"from_step": 1, "output": "missing"}},
                "binding": service_step()["binding"]}
    with pytest.raises(ValidationError, match="no output"):
        Runbook.model_validate(document(producer, consumer))

    from planet_express.execution import runbook as module
    original = module.STEP_TYPES["read.qbittorrent_session_port"]
    monkeypatch.setitem(module.STEP_TYPES, "read.qbittorrent_session_port",
                        original.__class__(original.params_model, original.binding_model,
                                           original.risk, original.rollback, {"port": str},
                                           original.reference_fields, original.target))
    with pytest.raises(ValidationError, match="type"):
        Runbook.model_validate(document(producer, consumer | {
            "params": {"match": "New :", "port": {"from_step": 1, "output": "port"}}
        }))


def test_valid_reference_canonical_hash_and_tamper_detection():
    producer = {"type": "read.qbittorrent_session_port", "params": {},
                "binding": service_step()["binding"]}
    consumer = {"type": "check.log_since_start",
                "params": {"match": "New :", "port": {"from_step": 1, "output": "port"}},
                "binding": service_step()["binding"]}
    first = Runbook.model_validate(document(producer, consumer))
    reordered = json.loads(json.dumps(document(producer, consumer), sort_keys=False))
    second = Runbook.model_validate(reordered)
    assert canonical_json(first) == canonical_json(second)
    assert plan_sha256(first) == plan_sha256(second)
    assert load_stored_plan(canonical_json(first), plan_sha256(first)) == first
    with pytest.raises(ValueError, match="hash mismatch"):
        load_stored_plan(canonical_json(first).replace("Test", "Tampered"), plan_sha256(first))


def test_artifact_hash_and_unused_artifact_are_rejected():
    content = "services: {}\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(ValidationError, match="does not match"):
        Runbook.model_validate(document(service_step(), artifacts={"0" * 64: content}))
    with pytest.raises(ValidationError, match="unused"):
        Runbook.model_validate(document(service_step(), artifacts={digest: content}))


def test_risk_and_mutating_pairs_preserve_multiplicity():
    runbook = Runbook.model_validate(document(
        service_step(),
        {"type": "wait", "params": {"seconds": 1}, "binding": {}},
        service_step(),
        service_step("service.stop"),
    ))
    assert risk(runbook) == "R2"
    assert mutating_pairs(runbook) == [
        ("service.restart", "media/sonarr"),
        ("service.restart", "media/sonarr"),
        ("service.stop", "media/sonarr"),
    ]


def test_runtime_output_validation_enforces_port_range_and_stack_service_identities():
    assert validate_outputs("read.qbittorrent_session_port", {"port": 65535}) == {"port": 65535}
    for invalid in (0, 65536, "123", 123.0, True):
        with pytest.raises(ValidationError):
            validate_outputs("read.qbittorrent_session_port", {"port": invalid})

    outputs = validate_outputs("stack.up", {"services": [
        {"project": "media", "service": "sonarr", "container_name": "media-sonarr-1",
         "container_id": "5011a22c0de1"},
        {"project": "media", "service": "radarr", "container_name": "media-radarr-1",
         "container_id": "7ad1a22c0de1"},
    ]})
    assert [item["service"] for item in outputs["services"]] == ["sonarr", "radarr"]
    json.dumps(outputs)  # persisted with json.dumps: must be plain JSON (own review, T38)
    with pytest.raises(ValidationError):
        validate_outputs("stack.up", {"services": []})


@pytest.mark.parametrize(("action", "expected"), [
    ("start", "conditional"),
    ("stop", "conditional"),
    ("restart", "none"),
])
def test_unit_action_rollback_depends_on_action(action, expected):
    step = Runbook.model_validate(document({
        "type": "unit.action",
        "params": {"action": action, "unit": "casa-dashboard.service"},
        "binding": {"unit": "casa-dashboard.service"},
    })).steps[0]
    assert rollback_kind(step) == expected


@pytest.mark.parametrize("mutate", [
    lambda s: s["params"].update(stack="../../etc"),
    lambda s: s["params"].update(stack="media;rm -rf /"),
    lambda s: s["params"].update(service="a b"),
    lambda s: s["binding"].update(container="$(id)"),
    lambda s: s["binding"].update(container_id="not-hex"),
    lambda s: s["binding"].update(compose_path="relative/docker-compose.yml"),
    lambda s: s["binding"].update(compose_path="/stacks/../etc/docker-compose.yml"),
])
def test_names_and_paths_are_validated_in_the_document(mutate):
    # Own review, T38: the approved document itself refuses path-like or shell-looking targets,
    # independent of execution-time re-resolution.
    step = service_step()
    mutate(step)
    with pytest.raises(ValidationError):
        Runbook.model_validate(document(step))


def test_unit_names_must_be_systemd_units():
    step = {"type": "unit.action", "params": {"action": "restart", "unit": "casa-stacks.service"},
            "binding": {"unit": "casa-stacks.service"}}
    Runbook.model_validate(document(step))
    for bad in ("casa-stacks", "../x.service", "a;b.service"):
        broken = {"type": "unit.action", "params": {"action": "restart", "unit": bad},
                  "binding": {"unit": bad}}
        with pytest.raises(ValidationError):
            Runbook.model_validate(document(broken))


def test_binding_must_name_the_same_target_as_params():
    # Own review, T38: the card and T24 key come from params, the engine acts on the binding.
    step = service_step(stack="media", service="sonarr")
    step["binding"]["compose_path"] = "/stacks/network/docker-compose.yml"
    with pytest.raises(ValidationError, match="not the 'media' stack"):
        Runbook.model_validate(document(step))


def test_container_bindings_carry_the_compose_service():
    # Codex review, T38: container-only steps must bind the full Compose identity.
    step = {"type": "check.container", "params": {"expect": "running"},
            "binding": service_step()["binding"]}
    Runbook.model_validate(document(step))
    missing = dict(step, binding={k: v for k, v in step["binding"].items() if k != "service"})
    with pytest.raises(ValidationError):
        Runbook.model_validate(document(missing))
    mismatched = service_step(service="sonarr")
    mismatched["binding"]["service"] = "radarr"
    with pytest.raises(ValidationError, match="binding service differs"):
        Runbook.model_validate(document(mismatched))
