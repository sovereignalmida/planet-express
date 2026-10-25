"""The plan an agent proposes to the operator, and the one place that decides whether PE accepts it.

A plan is untrusted agent output. It is strict (unknown keys are refused, never dropped), bounded, and every option's
verification checks must be kinds in PE's own catalogue with parameters inside that kind's own limits (`verify.plan.parse_plan`,
the same function that later checks the approved grant's plan). The agent cannot widen anything by adding fields, cannot
invent a check, and cannot approve its own plan: the plan carries no grant, no tier and no approval; confirming an option is
a separate act by the operator that mints the bound grant (P2). The prompt and the `--json-schema` document are GENERATED from the
catalogue here, never hand-written: with only field names and types the model got every check's bounds wrong (phase 0, E5),
and with each check's full schema 26 of 26 plans validated.
"""

import json
import os
import stat
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    model_validator,
)

from planet_express.missions.canonical import CanonicalError, loads_strict, sha256_hex
from planet_express.missions.events import redact_value
from planet_express.missions.verify.checks import PARAM_MODELS
from planet_express.missions.verify.plan import CATALOGUE_V, PlanInvalid, parse_plan

MAX_PLAN_BYTES = 128 * 1024
PLAN_FILE = "plan.json"
Line = Annotated[StrictStr, Field(min_length=1, max_length=600)]
Short = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class PlanError(ValueError):
    """The plan is not acceptable. The message names the problem, never echoes the agent's text."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Risk(_Strict):
    level: Literal["low", "medium", "high"]
    text: Line


class CheckSpec(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    check: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    params: dict[str, Any]


class Option(_Strict):
    name: Short
    recommended: StrictBool
    summary: Line
    changes: Annotated[list[Line], Field(max_length=32)]
    will_not_touch: Annotated[list[Line], Field(max_length=32)]
    risk: Risk
    rolls_back_to: Line
    effort: Short
    checks: Annotated[list[CheckSpec], Field(min_length=1, max_length=32)]


class Plan(_Strict):
    known: Annotated[list[Line], Field(max_length=32)]
    unsure: Annotated[list[Line], Field(max_length=32)]
    options: Annotated[list[Option], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def _one_recommendation(self) -> "Plan":
        if sum(1 for o in self.options if o.recommended) != 1:
            raise ValueError("exactly one option must be recommended")
        return self


_KNOWN_FIELDS = frozenset(name for model in (Plan, Option, Risk, CheckSpec) for name in model.model_fields)


def parse(doc: Any) -> Plan:
    """Validate a decoded plan: the shape, then every option's checks against the catalogue. Raises PlanError."""
    try:
        plan = Plan.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors()[0]
        # A field NAME in the path is the agent's text when the field is an extra one: only names the contract defines are echoed.
        where = ".".join(str(p) if isinstance(p, int) or p in _KNOWN_FIELDS else "?" for p in first["loc"])
        raise PlanError(f"the plan is not valid: {where[:80]}: {first['type']}") from None
    for index, option in enumerate(plan.options):
        try:
            parse_plan({"catalogue_v": CATALOGUE_V, "checks": [c.model_dump() for c in option.checks]})
        except PlanInvalid:
            # PE's catalogue refused a check. Which one, and what it was called, is the agent's text: only the option is named.
            raise PlanError(f"option {index + 1}: a verification check is not one PE can run, or its parameters are out of bounds") from None
    return plan


def load_file(workspace: str, *, name: str = PLAN_FILE) -> Plan:
    """The file method: the agent writes `plan.json` in its workspace. The worker controls that file, so it is opened without
    following links, size-capped and parsed strictly (no duplicate keys, floats or NaN), then validated."""
    try:
        dir_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PlanError("the workspace is unavailable") from None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            raise PlanError("there is no plan file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PLAN_BYTES:
                raise PlanError("the plan file is not a regular file within the size limit")
            data = os.read(fd, MAX_PLAN_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    if len(data) > MAX_PLAN_BYTES:
        raise PlanError("the plan file is larger than its size limit")
    try:
        return parse(loads_strict(data.decode("utf-8")))
    except (CanonicalError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError("the plan file is not valid JSON") from None


def canonical(plan: Plan) -> dict[str, Any]:
    """The plan as stored: redacted string leaves, so nothing an agent smuggled into prose is kept as a secret."""
    return redact_value(plan.model_dump(mode="json"))


def plan_hash(plan: Plan) -> str:
    """The hash of the plan EXACTLY as it is stored and shown (`canonical`, redacted): a grant bound to this hash is bound to the
    content the operator reviewed, and re-parsing the stored JSON gives the same hash."""
    return sha256_hex(canonical(plan))


# ── generated from the catalogue (never hand-written) ─────────────────────────────────────────────

def catalogue_text() -> str:
    """Every check kind with its full JSON Schema, bounds and required fields included, for the prompt."""
    rows = []
    for kind in sorted(PARAM_MODELS):
        schema = PARAM_MODELS[kind].model_json_schema()
        schema.pop("title", None)
        rows.append(f"  {kind}: {json.dumps(schema, separators=(',', ':'))}")
    return "\n".join(rows)


def json_schema() -> dict[str, Any]:
    """The plan contract as JSON Schema, for `--json-schema`. A real schema matters: a generic object made the model return
    `options` as a JSON string. PE still validates every plan itself with `parse`."""
    text = {"type": "string"}
    risk = {"type": "object", "properties": {"level": {"enum": ["low", "medium", "high"]}, "text": text},
            "required": ["level", "text"], "additionalProperties": False}
    check = {"type": "object", "properties": {"id": text, "check": {"enum": sorted(PARAM_MODELS)}, "params": {"type": "object"}},
             "required": ["id", "check", "params"], "additionalProperties": False}
    names = ["name", "recommended", "summary", "changes", "will_not_touch", "risk", "rolls_back_to", "effort", "checks"]
    option = {"type": "object", "additionalProperties": False, "required": names, "properties": {
        "name": text, "recommended": {"type": "boolean"}, "summary": text, "changes": {"type": "array", "items": text},
        "will_not_touch": {"type": "array", "items": text}, "risk": risk, "rolls_back_to": text, "effort": text,
        "checks": {"type": "array", "items": check, "minItems": 1}}}
    return {"type": "object", "additionalProperties": False, "required": ["known", "unsure", "options"], "properties": {
        "known": {"type": "array", "items": text}, "unsure": {"type": "array", "items": text},
        "options": {"type": "array", "items": option, "minItems": 1, "maxItems": 3}}}


PLAN_INSTRUCTIONS = (
    "When the right response is a change, write your plan as plan.json in the current directory (use the Write tool). It is a JSON "
    "object with `known` (what the evidence shows), `unsure` (what it does not), and 1 to 3 `options`. Exactly one option has "
    "`recommended` true. Each option has: name, recommended, summary (one sentence), changes (services or files it would touch), "
    "will_not_touch, risk {level low|medium|high, text}, rolls_back_to, effort, and checks. `checks` are how PE will verify the "
    "option worked, and may ONLY use these check kinds, with params that match the schema EXACTLY (respect every bound, no extra "
    "fields):\n{catalogue}\nYou cannot approve a plan, and a plan carries no permissions: the operator confirms an option and PE "
    "applies only what that option says. Do not change anything on the host yourself.")


def instructions() -> str:
    return PLAN_INSTRUCTIONS.replace("{catalogue}", catalogue_text())


__all__ = ["MAX_PLAN_BYTES", "PLAN_FILE", "Option", "Plan", "PlanError", "canonical", "catalogue_text", "instructions",
           "json_schema", "load_file", "parse", "plan_hash"]
