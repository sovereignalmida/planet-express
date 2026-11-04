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
                 "effort": "2 minutes", "scope": {"stacks": ["media"], "services": ["media/qbittorrent"]}, "checks": [copy.deepcopy(CHECK)]},
                {"name": "Restart the gateway", "recommended": False, "summary": "Restart only the gateway.", "changes": ["gluetun"],
                 "will_not_touch": ["the dependents"], "risk": {"level": "medium", "text": "dependents may not recover"},
                 "rolls_back_to": "the checkpoint", "effort": "1 minute", "scope": {"stacks": ["media"], "services": ["media/qbittorrent"]}, "checks": [copy.deepcopy(CHECK)]}]}


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
    (lambda d: d["options"][0].pop("risk"), "no risk"),
    (lambda d: d["options"][0].pop("rolls_back_to"), "nothing to roll back to"),
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


def test_duplicate_check_ids_in_one_option_are_made_unique_not_refused():
    doc = good()
    doc["options"][0]["checks"] = [copy.deepcopy(CHECK), copy.deepcopy(CHECK)]
    assert [c.id for c in parse(doc).options[0].checks] == ["gateway-up", "gateway-up-2"]


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
    assert "cannot approve" in text and "before the operator confirms" in text


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


def test_a_deeply_nested_plan_file_is_a_refused_plan_not_a_crash(tmp_path):
    (tmp_path / "plan.json").write_text("[" * 100_000)
    with pytest.raises(PlanError, match="not valid JSON"):
        load_file(str(tmp_path))


# ── the structured scope (what a grant is made from if the operator confirms) ─────────────────────

class Inv:
    stacks = ("media", "network")
    services = ("media/qbittorrent", "media/gluetun", "network/adguard")


def test_every_option_must_name_a_scope_and_its_names_are_plain():
    for scope in ({}, {"stacks": [], "services": []}, {"stacks": ["../etc"]}, {"services": ["noslash"]}, {"services": ["a/b/c"]},
                  {"stacks": ["media"], "paths": ["/etc"]}, {"stacks": ["x" * 129]}, {"stacks": [f"s{i}" for i in range(9)]}):
        doc = good()
        doc["options"][0]["scope"] = scope
        with pytest.raises(PlanError):
            parse(doc)
    doc = good()
    del doc["options"][0]["scope"]
    with pytest.raises(PlanError):
        parse(doc)


def test_a_scope_naming_something_the_host_does_not_have_is_refused_when_the_inventory_is_known():
    assert parse(good(), Inv)                                                            # media and media/qbittorrent exist
    for scope in ({"stacks": ["ghost"]}, {"services": ["media/ghost"]}, {"stacks": ["media"], "services": ["network/nothing"]}):
        doc = good()
        doc["options"][1]["scope"] = scope
        with pytest.raises(PlanError, match=r"option 2: `scope\.(stacks|services)` item \d is not a (stack|service) on this host"):
            parse(doc, Inv)
    assert parse(doc)                                                                    # with no inventory only the shape is checked


def test_exclusions_must_be_resource_ids_and_are_part_of_the_hash():
    doc = good()
    doc["options"][0]["exclude"] = ["service:media/gluetun", "host:local/path:/srv/data"]
    plan = parse(doc, Inv)
    assert plan.options[0].exclude == ["service:media/gluetun", "host:local/path:/srv/data"]
    other = good()
    assert plan_mod.plan_hash(parse(other)) != plan_mod.plan_hash(plan)                  # scope and exclusions are what the operator confirms
    for bad in ("the gateway", "service:no-slash", "kind:x", "host:../service:a/b", "s" * 600):
        doc = good()
        doc["options"][0]["exclude"] = [bad]
        with pytest.raises(PlanError, match="option 1: `exclude` item 1 is not a resource id"):
            parse(doc)


def test_the_prompt_and_the_schema_describe_the_scope_and_point_at_the_inventory_file():
    text = plan_mod.instructions()
    assert "INVENTORY.md" in text and "scope" in text and "exclude" in text
    option = plan_mod.json_schema()["properties"]["options"]["items"]
    assert "scope" in option["required"] and option["properties"]["scope"]["additionalProperties"] is False


def test_what_an_option_will_not_touch_must_fit_the_grant_it_becomes_part_of():
    doc = good()
    doc["options"][0]["will_not_touch"] = ["x" * 501]
    with pytest.raises(PlanError):
        parse(doc)
    doc = good()
    doc["options"][0]["will_not_touch"] = [f"thing {i}" for i in range(20)]
    doc["options"][0]["exclude"] = [f"service:media/s{i}" for i in range(13)]
    with pytest.raises(PlanError):                                                    # 33 together: none is silently dropped on the way to the grant
        parse(doc)
    doc["options"][0]["exclude"] = doc["options"][0]["exclude"][:12]
    assert len(parse(doc).options[0].exclude) == 12


# ── only the checks this host can run (rc14.1, live M-19: an exec_probe on a host with an empty exec allowlist) ──────
from planet_express.missions.verify.checks import Availability, CheckEnv, availability  # noqa: E402

EXEC = {"id": "prowlarr-responds", "check": "exec_probe", "params": {"container": "CASA_PROWLARR", "argv": ["curl", "-sf", "http://localhost:9696/ping"]}}


def with_check(check):
    doc = good()
    doc["options"][0]["checks"].append(copy.deepcopy(check))
    return doc


def test_an_unavailable_check_is_refused_with_a_plain_reason_and_never_reaches_confirm():
    host = availability(CheckEnv())                                     # the live host: empty exec allowlist
    assert "exec_probe" in host.unavailable
    with pytest.raises(PlanError) as err:
        parse(with_check(EXEC), available=host)
    assert str(err.value) == ("option 1, check 2 (exec_probe): exec_probe is not available on this host "
                              "(missions.verify.exec_allowlist is empty); use one of: container_state, file_state, http, log_absent, mount_readable")
    assert "curl" not in str(err.value) and "prowlarr" not in str(err.value)
    parse(good(), available=host)                                       # the same plan without it is fine
    parse(with_check(EXEC))                                             # and with no host to ask, the catalogue alone decides (as before)


def test_exec_probe_is_offered_only_with_the_allowlisted_binaries_and_others_are_refused():
    host = Availability({}, frozenset({"curl"}))
    parse(with_check(EXEC), available=host)
    other = copy.deepcopy(EXEC)
    other["params"]["argv"] = ["bash", "-c", "true"]
    with pytest.raises(PlanError, match="exec allowlist"):
        parse(with_check(other), available=host)
    assert "argv[0]: curl" in plan_mod.instructions(host)


def test_the_prompt_and_schema_list_only_this_hosts_checks():
    host = availability(CheckEnv())
    text = plan_mod.instructions(host)
    assert "  exec_probe: " not in text and "  query: " not in text and "  project_complete: " not in text
    assert "  container_state: " in text and "  http: " in text
    assert "Not available on this host, so never use: exec_probe, project_complete, query" in text
    kinds = plan_mod.json_schema(host)["properties"]["options"]["items"]["properties"]["checks"]["items"]["properties"]["check"]["enum"]
    assert kinds == host.kinds() and "exec_probe" not in kinds


# ── rc14.2: lenient where it costs nothing, and refusals that name what is wrong ─────────────────────────────────────────────

def test_a_check_without_an_id_gets_one_and_kind_whitespace_and_case_are_normalised():
    doc = good()
    del doc["options"][0]["checks"][0]["id"]                          # live: "options.0.checks.0.id: missing", three times
    doc["options"][1]["checks"][0]["check"] = " Container_State "
    del doc["options"][1]["checks"][0]["id"]
    plan = parse(doc)
    assert plan.options[0].checks[0].id == "container_state-1"
    assert plan.options[1].checks[0].check == "container_state" and plan.options[1].checks[0].id == "container_state-1"
    assert plan_mod.plan_hash(parse(doc)) == plan_mod.plan_hash(plan)     # the filled-in plan is what is stored and hashed


def test_missing_optional_fields_get_their_defaults():
    doc = good()
    for key in ("known", "unsure"):
        del doc[key]
    del doc["options"][1]
    for key in ("will_not_touch", "changes", "effort", "recommended"):
        del doc["options"][0][key]
    del doc["options"][0]["scope"]["services"]
    del doc["options"][0]["checks"][0]["params"]["min_uptime_s"]
    plan = parse(doc)
    option = plan.options[0]
    assert plan.known == [] and option.recommended and option.will_not_touch == [] and option.effort == "not stated"
    assert option.scope.services == [] and option.scope.stacks == ["media"]


def test_when_one_option_is_marked_recommended_the_unmarked_ones_are_not():
    doc = good()
    del doc["options"][1]["recommended"]
    assert [o.recommended for o in parse(doc).options] == [True, False]


def test_none_recommended_among_several_is_still_refused_because_it_is_a_choice():
    doc = good()
    for option in doc["options"]:
        del option["recommended"]
    with pytest.raises(PlanError):
        parse(doc)


@pytest.mark.parametrize("mutate, words", [
    (lambda c: c["params"].update(timeout_s=10_000), "option 1, check 1 (container_state): parameter `timeout_s` must be at most 60"),
    (lambda c: c["params"].pop("container"), "option 1, check 1 (container_state): parameter `container` is missing"),
    (lambda c: c["params"].update(SECRETPARAM=1), "option 1, check 1 (container_state): a parameter container_state does not take (it takes: "),
    (lambda c: c.update(check="SECRETKIND"), "option 1, check 1: the kind is not one PE knows; use one of: container_state, exec_probe"),
    (lambda c: c["params"].update(min_uptime_s="30"), "option 1, check 1 (container_state): parameter `min_uptime_s` must be a whole number"),
])
def test_a_refused_check_names_the_option_the_position_the_kind_and_the_problem(mutate, words):
    doc = good()
    mutate(doc["options"][0]["checks"][0])
    with pytest.raises(PlanError) as err:
        parse(doc)
    assert str(err.value).startswith(words)
    assert "SECRET" not in str(err.value)


def test_a_refusal_names_several_problems_so_one_retry_can_fix_them():
    doc = good()
    doc["options"][0]["summary"] = "x" * 700
    doc["options"][1]["name"] = ""
    with pytest.raises(PlanError) as err:
        parse(doc)
    assert str(err.value) == "option 1: `summary` is longer than 600 characters; option 2: `name` must not be empty"


def test_an_unknown_field_is_named_as_unknown_and_never_echoed():
    doc = good()
    doc["options"][0]["checks"][0]["SECRETFIELD"] = 1
    with pytest.raises(PlanError) as err:
        parse(doc)
    assert str(err.value).startswith("option 1, check 1 (container_state): has a field PE does not know")
    assert "SECRET" not in str(err.value)


def test_a_refused_exec_probe_names_the_allowlist():
    host = Availability({}, frozenset({"curl", "wget"}))
    other = copy.deepcopy(EXEC)
    other["params"]["argv"] = ["bash"]
    with pytest.raises(PlanError) as err:
        parse(with_check(other), available=host)
    assert str(err.value).endswith("exec allowlist (curl, wget)") and "bash" not in str(err.value)


def test_the_planner_and_the_repair_brief_say_to_back_up_stateful_data_and_to_answer_a_go_ahead_with_a_plan():
    from planet_express.missions import brief
    text = plan_mod.instructions()
    assert ".pe-backup-" in text and "sudo cp -a" in text and "rolls_back_to" in text
    assert "If the operator says to go ahead" in text and "Authorise button" in text
    repair = brief.instructions_for("repair")
    assert ".pe-backup-" in repair and "sudo cp -a" in repair and "backup path in your result" in repair
    assert ".pe-backup-" not in brief.instructions_for("observe")
