"""The plan contract: strict, bounded, checks only from PE's catalogue with their own bounds; the agent cannot widen or invent."""
import copy
import json
import os

import pytest

from planet_express.console import plan as plan_mod
from planet_express.console.plan import PlanError, load_file, parse
from planet_express.missions.verify.checks import PARAM_MODELS

CHECK = {"id": "gateway-up", "check": "container_state", "params": {"container": "gluetun", "min_uptime_s": 30}}


def good():
    return {"known": ["the gateway is healthy"], "unsure": ["whether the start order caused it"],
            "options": [
                {"name": "Recreate dependents", "recommended": True, "summary": "Recreate the four dependents in order.",
                 "changes": ["4 services in the media stack"], "will_not_touch": ["the gateway", "ingress", "DNS"],
                 "risk": {"level": "low", "text": "about a minute of downtime"}, "rolls_back_to": "the checkpoint taken just before",
                 "effort": "2 minutes", "checks": [copy.deepcopy(CHECK)]},
                {"name": "Restart the gateway", "recommended": False, "summary": "Restart only the gateway.", "changes": ["gluetun"],
                 "will_not_touch": ["the dependents"], "risk": {"level": "medium", "text": "dependents may not recover"},
                 "rolls_back_to": "the checkpoint", "effort": "1 minute", "checks": [copy.deepcopy(CHECK)]}]}


def test_a_good_plan_is_accepted_and_hashes_stably():
    plan = parse(good())
    assert len(plan.options) == 2 and plan.options[0].recommended
    assert plan_mod.plan_hash(parse(good())) == plan_mod.plan_hash(plan)
    other = good()
    other["options"][1]["summary"] = "changed"
    assert plan_mod.plan_hash(parse(other)) != plan_mod.plan_hash(plan)


@pytest.mark.parametrize("mutate, why", [
    (lambda d: d.update(grant={"tier": "repair"}), "top-level extra"),
    (lambda d: d.update(tier="repair"), "a tier"),
    (lambda d: d.update(approved=True), "self-approval"),
    (lambda d: d["options"][0].update(scope={"stacks": ["*"]}), "widening scope on an option"),
    (lambda d: d["options"][0].update(grant="x"), "a grant on an option"),
    (lambda d: d["options"][0]["checks"][0].update(extra=1), "extra field on a check"),
    (lambda d: d["options"][0]["risk"].update(level="catastrophic"), "invented risk level"),
    (lambda d: d["options"][0]["risk"].update(extra="x"), "extra risk field"),
    (lambda d: d.update(options=[]), "no options"),
    (lambda d: d.update(options=d["options"] * 2), "four options"),
    (lambda d: d["options"][1].update(recommended=True), "two recommended"),
    (lambda d: [o.update(recommended=False) for o in d["options"]], "none recommended"),
    (lambda d: d["options"][0].update(checks=[]), "no checks"),
    (lambda d: d["options"][0].update(name=""), "empty name"),
    (lambda d: d["options"][0].update(changes="restart it"), "changes not a list"),
    (lambda d: d["options"][0].update(summary="x" * 601), "summary too long"),
    (lambda d: d["options"][0].update(recommended="yes"), "recommended not a bool"),
    (lambda d: d.pop("known"), "missing known"),
])
def test_a_plan_that_adds_removes_or_loosens_anything_is_refused(mutate, why):
    doc = good()
    mutate(doc)
    with pytest.raises(PlanError):
        parse(doc)


@pytest.mark.parametrize("check", [
    {"id": "x", "check": "shell", "params": {"command": "rm -rf /"}},                                           # an invented check kind
    {"id": "x", "check": "container_state", "params": {"container": "a", "extra": 1}},     # an extra parameter
    {"id": "x", "check": "container_state", "params": {"container": "a", "timeout_s": 10_000}},   # out of bounds
    {"id": "x", "check": "container_state", "params": {"container": "a b"}},                # not a container name
    {"id": "x", "check": "container_state", "params": {}},                                                      # a required parameter missing
    {"id": "x", "check": "http", "params": {"url": "file:///etc/passwd"}},
])
def test_an_invented_check_or_out_of_limits_parameters_are_refused_by_pes_own_catalogue(check):
    doc = good()
    doc["options"][0]["checks"] = [check]
    with pytest.raises(PlanError, match="option 1"):
        parse(doc)


def test_duplicate_check_ids_in_one_option_are_refused():
    doc = good()
    doc["options"][0]["checks"] = [copy.deepcopy(CHECK), copy.deepcopy(CHECK)]
    with pytest.raises(PlanError):
        parse(doc)


def test_the_error_never_echoes_the_agents_text():
    shape, kind, extra = good(), good(), good()
    shape["options"][0]["summary"] = "sk-or-v1-" + "a" * 48 + "x" * 601                 # too long: the shape error
    kind["options"][1]["checks"][0]["check"] = "SECRETKIND"                           # an invented kind: the catalogue error
    extra["options"][0]["SECRETFIELD"] = "sk-or-v1-" + "b" * 48                       # an extra field
    for doc in (shape, kind, extra):
        with pytest.raises(PlanError) as err:
            parse(doc)
        assert "sk-or-v1" not in str(err.value) and "SECRETKIND" not in str(err.value) and "SECRETFIELD" not in str(err.value)


# ── the file method ───────────────────────────────────────────────────────────────────────────────

def test_a_plan_file_in_the_workspace_is_read_strictly(tmp_path):
    (tmp_path / "plan.json").write_text(json.dumps(good()))
    assert len(load_file(str(tmp_path)).options) == 2


@pytest.mark.parametrize("content, why", [
    ('{"known": [], "known": []}', "duplicate key"), ("not json", "not json"), ('{"known": NaN}', "NaN"),
    ('{"known": [], "unsure": [], "options": [{"name": 1.5}]}', "a float"), ("[]", "not an object"), ("", "empty"),
    ("\xff", "not utf-8"),
])
def test_a_malformed_plan_file_is_refused(tmp_path, content, why):
    (tmp_path / "plan.json").write_bytes(content.encode("latin-1"))
    with pytest.raises(PlanError):
        load_file(str(tmp_path))


def test_a_missing_oversize_symlinked_or_non_regular_plan_file_is_refused(tmp_path):
    with pytest.raises(PlanError, match="no plan file"):
        load_file(str(tmp_path))
    (tmp_path / "plan.json").write_text(" " * (plan_mod.MAX_PLAN_BYTES + 1))
    with pytest.raises(PlanError, match="size limit"):
        load_file(str(tmp_path))
    (tmp_path / "plan.json").unlink()
    (tmp_path / "real.json").write_text(json.dumps(good()))
    os.symlink(tmp_path / "real.json", tmp_path / "plan.json")
    with pytest.raises(PlanError, match="no plan file"):                      # O_NOFOLLOW: a link is as good as nothing
        load_file(str(tmp_path))
    (tmp_path / "plan.json").unlink()
    os.mkfifo(tmp_path / "plan.json")
    with pytest.raises(PlanError):
        load_file(str(tmp_path))
    with pytest.raises(PlanError, match="workspace"):
        load_file(str(tmp_path / "nowhere"))


def test_the_stored_form_is_redacted():
    doc = good()
    doc["known"] = ["found OPENROUTER_API_KEY=sk-or-v1-" + "a" * 48 + " in the env"]
    stored = plan_mod.canonical(parse(doc))
    assert "sk-or-v1-" + "a" * 48 not in json.dumps(stored)


# ── generated from the catalogue ──────────────────────────────────────────────────────────────────

def test_the_prompt_carries_every_check_kind_with_its_bounds_and_the_schema_lists_exactly_them():
    text = plan_mod.instructions()
    for kind in PARAM_MODELS:
        assert f"  {kind}: " in text
    assert '"maximum"' in text and '"minimum"' in text                         # the bounds, which the first phase-0 prompt hid
    schema = plan_mod.json_schema()
    kinds = schema["properties"]["options"]["items"]["properties"]["checks"]["items"]["properties"]["check"]["enum"]
    assert kinds == sorted(PARAM_MODELS)
    assert schema["additionalProperties"] is False and schema["properties"]["options"]["maxItems"] == 3
    assert "cannot approve" in text and "carries no permissions" in text


def test_a_plan_valid_against_the_generated_json_schema_is_one_pe_accepts():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(good(), plan_mod.json_schema())
    parse(good())


def test_the_plan_hash_is_the_hash_of_exactly_what_is_stored_even_when_redaction_changed_a_string():
    from planet_express.missions.canonical import loads_strict, sha256_hex
    doc = good()
    doc["known"] = ["found OPENROUTER_API_KEY=sk-or-v1-" + "a" * 48 + " in the env"]
    plan = parse(doc)
    stored = plan_mod.canonical(plan)
    assert stored["known"] != doc["known"]                                          # redaction did change it
    assert plan_mod.plan_hash(plan) == sha256_hex(stored)
    assert plan_mod.plan_hash(parse(loads_strict(json.dumps(stored)))) == plan_mod.plan_hash(plan)   # re-parsing the stored JSON agrees


def test_a_refused_check_names_only_the_option_never_the_agents_check_id_or_kind():
    doc = good()
    doc["options"][1]["checks"] = [{"id": "AGENTCHOSENID", "check": "AGENTCHOSENKIND", "params": {}}]
    with pytest.raises(PlanError) as err:
        parse(doc)
    assert "option 2" in str(err.value) and "AGENTCHOSEN" not in str(err.value)
