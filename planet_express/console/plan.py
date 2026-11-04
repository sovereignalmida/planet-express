"""The plan an agent proposes to the operator, and the one place that decides whether PE accepts it.

A plan is untrusted agent output. It is strict (unknown keys are refused, never dropped), bounded, and every option's
verification checks must be kinds in PE's own catalogue with parameters inside that kind's own limits (`verify.plan.parse_plan`,
the same function that later checks the approved grant's plan). The agent cannot widen anything by adding fields, cannot
invent a check, and cannot approve its own plan: the plan carries no grant, no tier and no approval; confirming an option is
a separate act by the operator that mints the bound grant (P2). The prompt and the `--json-schema` document are GENERATED from the
catalogue here, never hand-written: with only field names and types the model got every check's bounds wrong (phase 0, E5),
and with each check's full schema 26 of 26 plans validated.
"""

import copy
import json
import os
import re
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
from planet_express.missions.resources import BadResource, canon
from planet_express.missions.verify.checks import PARAM_MODELS
from planet_express.missions.verify.plan import CATALOGUE_V, PlanInvalid, parse_plan

MAX_PLAN_BYTES = 128 * 1024
PLAN_FILE = "plan.json"
Line = Annotated[StrictStr, Field(min_length=1, max_length=600)]
Short = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Carve = Annotated[StrictStr, Field(min_length=1, max_length=500)]        # becomes a grant exclusion (512 max): never longer than the grant takes


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


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
NAME_RE, SERVICE_RE = _NAME, _SERVICE


class OptionScope(_Strict):
    """What the option works on, in the host inventory's own vocabulary: stacks by name, services as `<stack>/<service>`. It is what a grant
    is made from if the operator confirms (its leases and what PE checks), so it is checked against the host's inventory, never taken on faith."""
    stacks: Annotated[list[StrictStr], Field(max_length=8)] = []
    services: Annotated[list[StrictStr], Field(max_length=32)] = []

    @model_validator(mode="after")
    def _shape(self) -> "OptionScope":
        if not self.stacks and not self.services:
            raise ValueError("an option must name at least one stack or service")
        if not all(_NAME.fullmatch(s) for s in self.stacks) or not all(_SERVICE.fullmatch(s) for s in self.services):
            raise ValueError("stack and service names are plain names, services as <stack>/<service>")
        return self


class Option(_Strict):
    name: Short
    recommended: StrictBool
    summary: Line
    changes: Annotated[list[Line], Field(max_length=32)]
    will_not_touch: Annotated[list[Carve], Field(max_length=32)]
    risk: Risk
    rolls_back_to: Line
    effort: Short
    scope: OptionScope
    exclude: Annotated[list[StrictStr], Field(max_length=32)] = []      # resource ids (`service:media/gluetun`) the run is told to leave alone
    checks: Annotated[list[CheckSpec], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def _carve_outs_fit_the_grant(self) -> "Option":
        if len(self.exclude) + len(self.will_not_touch) > 32:
            raise ValueError("an option may list at most 32 things it will not touch and excludes, together")        # all of them reach the brief, none silently dropped
        return self


class Plan(_Strict):
    known: Annotated[list[Line], Field(max_length=32)]
    unsure: Annotated[list[Line], Field(max_length=32)]
    options: Annotated[list[Option], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def _one_recommendation(self) -> "Plan":
        if sum(1 for o in self.options if o.recommended) != 1:
            raise ValueError("exactly one option must be recommended")
        return self


_KNOWN_FIELDS = frozenset(name for model in (Plan, Option, Risk, CheckSpec, OptionScope) for name in model.model_fields)
MAX_REPORTED_PROBLEMS = 3            # a refusal names up to this many problems, so one retry can fix them together


# ── leniency: what costs nothing to fill in is filled in, never refused (rc14.2, live: "options.0.checks.0.id: missing") ──────

def _norm_kind(kind: Any) -> Any:
    """`" Container_State "` -> `container_state`: whitespace, case and a dash for an underscore are not worth a refusal."""
    return kind.strip().lower().replace("-", "_") if isinstance(kind, str) else kind


def lenient(doc: Any) -> Any:
    """A copy of the decoded plan with what is harmless filled in: a check without an `id` gets `<kind>-<n>`, a repeated id is made
    unique the same way, a missing `params` is `{}`, a kind's stray whitespace and case are normalised, missing lists (`known`,
    `unsure`, `changes`, `will_not_touch`, `exclude`, a scope's `stacks`/`services`) are empty, a missing `effort` is "not stated", and
    a single option with no `recommended` is the recommended one (and, when exactly one is marked, the unmarked are not). Nothing that the operator reviews or that bounds the run (a check's
    kind or parameters, the scope's names, risk, rolls_back_to) is invented: those are still validated, and refused when wrong."""
    if not isinstance(doc, dict):
        return doc
    doc = copy.deepcopy(doc)
    for key in ("known", "unsure"):
        doc.setdefault(key, [])
    options = doc.get("options")
    if not isinstance(options, list):
        return doc
    chosen = sum(1 for o in options if isinstance(o, dict) and o.get("recommended") is True)
    for option in options:
        if not isinstance(option, dict):
            continue
        if chosen == 1:
            option.setdefault("recommended", False)      # one option is marked recommended: the unmarked ones are not
        for key in ("changes", "will_not_touch", "exclude"):
            option.setdefault(key, [])
        option.setdefault("effort", "not stated")
        if len(options) == 1:
            option.setdefault("recommended", True)
        scope = option.get("scope")
        if isinstance(scope, dict):
            scope.setdefault("stacks", [])
            scope.setdefault("services", [])
        checks = option.get("checks")
        if not isinstance(checks, list):
            continue
        seen: set[str] = set()
        for n, check in enumerate(checks, 1):
            if not isinstance(check, dict):
                continue
            if "check" in check:
                check["check"] = _norm_kind(check["check"])
            check.setdefault("params", {})
            cid = check.get("id")
            if cid is None or (isinstance(cid, str) and not cid.strip()):
                kind = check.get("check") if check.get("check") in PARAM_MODELS else "check"
                cid = f"{kind}-{n}"
            if isinstance(cid, str):
                cid = cid.strip()
                base, k = cid, 2
                while cid in seen:
                    cid, k = f"{base[:120]}-{k}", k + 1
                seen.add(cid)
            check["id"] = cid
    return doc


# ── refusals in words: the option, the check's position and (when it is a catalogue name) its kind, and the problem ─────────

_TYPES = {"string_type": "must be text", "int_type": "must be a whole number", "bool_type": "must be true or false",
          "list_type": "must be a list", "dict_type": "must be an object", "model_type": "must be an object",
          "model_attributes_type": "must be an object", "int_from_float": "must be a whole number", "json_type": "must be JSON"}


def _problem(err: dict[str, Any]) -> str:
    """What is wrong, from pydantic's error TYPE and its context (bounds from PE's own models), never from the input value."""
    t, ctx = err.get("type", ""), err.get("ctx") or {}
    if t == "missing":
        return "is missing"
    if t == "extra_forbidden":
        return "must be removed"
    if t in _TYPES:
        return _TYPES[t]
    if t == "string_too_short":
        return "must not be empty" if ctx.get("min_length") == 1 else f"must be at least {ctx.get('min_length')} characters"
    if t == "string_too_long":
        return f"is longer than {ctx.get('max_length')} characters"
    if t == "too_short":
        return f"needs at least {ctx.get('min_length')} item{'s' if ctx.get('min_length') != 1 else ''}"
    if t == "too_long":
        return f"has more than {ctx.get('max_length')} items"
    for bound, words in (("ge", "at least"), ("le", "at most"), ("gt", "more than"), ("lt", "less than")):
        if t == {"ge": "greater_than_equal", "le": "less_than_equal", "gt": "greater_than", "lt": "less_than"}[bound]:
            return f"must be {words} {ctx.get(bound)}"
    if t == "literal_error":
        return f"must be one of {str(ctx.get('expected'))[:120]}"           # the expected values are PE's own literals
    if t == "string_pattern_mismatch":
        return "does not have the required format"
    if t in ("value_error", "assertion_error"):
        return str(err.get("msg", "")).removeprefix("Value error, ").removeprefix("Assertion failed, ")[:160] or "is not valid"
    return t[:60] or "is not valid"


def _kind_of(doc: Any, option: int, check: int) -> str | None:
    """The kind of check `check` in option `option`, only when it is a name in PE's catalogue (never the agent's text otherwise)."""
    try:
        kind = doc["options"][option]["checks"][check]["check"]
    except (KeyError, IndexError, TypeError):
        return None
    return kind if isinstance(kind, str) and kind in PARAM_MODELS else None


def _where(loc: tuple, doc: Any, fields: frozenset[str] = _KNOWN_FIELDS) -> str:
    """`("options", 0, "checks", 1, "params")` -> `option 1, check 2 (http): params`. A field name is echoed only when PE's contract defines it."""
    parts: list[str] = []
    names: list[str] = []
    option = None
    i = 0
    loc = tuple(loc)
    while i < len(loc):
        p = loc[i]
        nxt = loc[i + 1] if i + 1 < len(loc) else None
        if p == "options" and isinstance(nxt, int):
            option = nxt
            parts.append(f"option {nxt + 1}")
            i += 2
            continue
        if p == "checks" and isinstance(nxt, int):
            kind = _kind_of(doc, option, nxt) if option is not None else None
            parts.append(f"check {nxt + 1}" + (f" ({kind})" if kind else ""))
            i += 2
            continue
        if isinstance(p, int):
            names.append(f"item {p + 1}")
        elif p in fields:
            names.append(p)
        else:
            return f"{', '.join(parts) or 'the plan'}: has a field PE does not know (PLAN_FORMAT.md lists every field), so it "
        i += 1
    head = ", ".join(parts) or "the plan"
    if not names:
        return f"{head}: "
    path = ".".join(n for n in names if not n.startswith("item "))
    items = " ".join(n for n in names if n.startswith("item "))
    return f"{head}: `{path}`{' ' + items if items else ''} " if path else f"{head}: {items} "


def _refuse(problems: list[str]) -> "PlanError":
    shown = problems[:MAX_REPORTED_PROBLEMS]
    more = len(problems) - len(shown)
    return PlanError("; ".join(shown) + (f"; and {more} more" if more > 0 else ""))


def _check_params(kind: str, params: Any, where: str) -> list[str]:
    """The parameter problems of one check against its own catalogue model, each naming the parameter (when the model has it) and
    the bound it breaks. Parameter VALUES are never echoed."""
    model = PARAM_MODELS[kind]
    if not isinstance(params, dict):
        return [f"{where}: `params` must be an object"]
    try:
        model.model_validate(params)
    except ValidationError as exc:
        out = []
        known = frozenset(model.model_fields)
        for err in exc.errors():
            loc = err.get("loc") or ()
            name = loc[0] if loc else None
            if name is None:
                out.append(f"{where}: {_problem(err)}")
            elif name in known:
                out.append(f"{where}: parameter `{name}` {_problem(err)}")
            else:
                out.append(f"{where}: a parameter {kind} does not take (it takes: {', '.join(sorted(known))})")
        return out
    except ValueError:
        return [f"{where}: its parameters are not valid"]
    return []


def parse(doc: Any, inventory: Any = None, available: Any = None) -> Plan:
    """Validate a decoded plan: first `lenient` fills in what costs nothing, then the shape, every option's scope against the host's
    inventory (when one is given) and its exclusions as resource ids, then every option's checks against the catalogue and (when
    `available`, a `verify.checks.Availability`, is given) against what this host can actually run: a check that could only fail at
    verification never reaches Confirm. Raises PlanError naming the option, the check's position and catalogue kind, and the problem
    (never echoing the agent's text)."""
    doc = lenient(doc)
    try:
        plan = Plan.model_validate(doc)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            text = f"{_where(err['loc'], doc)}{_problem(err)}"
            if text not in problems:
                problems.append(text)
        raise _refuse(problems) from None
    kinds = _kinds(available)
    problems: list[str] = []
    for index, option in enumerate(plan.options):
        label = f"option {index + 1}"
        if inventory is not None:
            stacks, services = set(inventory.stacks), set(inventory.services)
            for n, name in enumerate(option.scope.stacks, 1):
                if name not in stacks:
                    problems.append(f"{label}: `scope.stacks` item {n} is not a stack on this host (INVENTORY.md lists them)")
            for n, name in enumerate(option.scope.services, 1):
                if name not in services:
                    problems.append(f"{label}: `scope.services` item {n} is not a service on this host (INVENTORY.md lists them)")
        for n, resource in enumerate(option.exclude, 1):
            try:
                canon(resource)
            except BadResource:
                problems.append(f"{label}: `exclude` item {n} is not a resource id such as service:<stack>/<service>")
        for n, check in enumerate(option.checks, 1):
            known = check.check in PARAM_MODELS
            where = f"{label}, check {n}" + (f" ({check.check})" if known else "")
            if not known:
                problems.append(f"{where}: the kind is not one PE knows; use one of: {', '.join(kinds)}")
                continue
            if available is not None and check.check in getattr(available, "unavailable", {}):
                why = available.problem(check.check, check.params)
                problems.append(f"{where}: {why}; use one of: {', '.join(kinds)}")
                continue
            param_problems = _check_params(check.check, check.params, where)
            problems.extend(param_problems)
            if param_problems:
                continue
            if available is not None:
                why = available.problem(check.check, check.params)      # the kind is a catalogue name by now; the params are never echoed
                if why is not None:
                    allowed = sorted(getattr(available, "exec_allowlist", ()) or ())
                    problems.append(f"{where}: {why}" + (f" ({', '.join(allowed)})" if check.check == "exec_probe" and allowed else ""))
        if not any(p.startswith(label + ",") or p.startswith(label + ":") for p in problems):
            try:
                parse_plan({"catalogue_v": CATALOGUE_V, "checks": [c.model_dump() for c in option.checks]})
            except PlanInvalid:
                # The same gate the grant's plan meets later. Everything it checks was checked above in words; this is the backstop.
                problems.append(f"{label}: its verification checks are not a plan PE can run (a parameter value has a type PE does not take)")
    if problems:
        raise _refuse(problems)
    return plan


def read_doc(workspace: str, name: str, limit: int = MAX_PLAN_BYTES, what: str = "plan") -> Any:
    """A JSON file the agent wrote in its workspace, decoded. The worker controls the file, so it is opened without following links,
    size-capped and parsed strictly (no duplicate keys, floats or NaN). Raises PlanError (never echoing the agent's text)."""
    try:
        dir_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PlanError("the workspace is unavailable") from None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            raise PlanError(f"there is no {what} file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise PlanError(f"the {what} file is not a regular file within the size limit")
            data = os.read(fd, limit + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    if len(data) > limit:
        raise PlanError(f"the {what} file is larger than its size limit")
    try:
        return loads_strict(data.decode("utf-8"))
    except (CanonicalError, UnicodeDecodeError, ValueError, RecursionError):
        raise PlanError(f"the {what} file is not valid JSON") from None


def load_file(workspace: str, *, name: str = PLAN_FILE, inventory: Any = None, available: Any = None) -> Plan:
    """The file method: the agent writes `plan.json` in its workspace; it is read by `read_doc`, then validated."""
    return parse(read_doc(workspace, name), inventory, available)


def stored_before_trust_mode(plan_json: str) -> bool:
    """A plan stored before trust mode: its options carry `restart_only`, which that update removed (with the restart-only grant)."""
    try:
        doc = json.loads(plan_json)
        return any(isinstance(o, dict) and "restart_only" in o for o in doc.get("options", []))
    except (ValueError, AttributeError, TypeError):
        return False


def check_phrase(check: Any) -> str:
    """A check in words (the page has the same phrasing): from PE's catalogue kind and its parameters."""
    params = getattr(check, "params", None) or (check.get("params") if isinstance(check, dict) else None) or {}
    kind = getattr(check, "check", None) or (check.get("check") if isinstance(check, dict) else "a check")
    if kind == "container_state":
        restarted = " and was restarted by this run" if params.get("started_during_mission") else ""
        return f"{params.get('container', 'a container')} is running and stable{restarted}"
    if kind == "http":
        return f"{params.get('url', 'a URL')} answers"
    if kind == "mount_readable":
        return f"{params.get('path', 'a path')} is readable" + (f" inside {params['container']}" if params.get("container") else "")
    if kind == "project_complete":
        return f"the {params.get('project', 'stack')} project has all its services"
    if kind == "exec_probe":
        argv = params.get("argv") or ["a command"]
        return f"{params.get('container', 'a container')} can run {argv[0]}"
    bits = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(params.items())[:3])
    return f"{kind}" + (f" ({bits})" if bits else "")


def goal_text(option: Option) -> str:
    """The approved option as the execution's goal (what the agent is asked to carry out and what the operator's approval card shows).
    Built by PE from the validated option, never from free agent prose outside it."""
    lines = [f"Carry out this plan the operator approved: {option.name}.", option.summary, "", "Change only what it says:"]
    lines += [f"- {c}" for c in option.changes]
    lines += ["", "Do not touch:"] + [f"- {w}" for w in [*option.will_not_touch, *option.exclude]]
    lines += ["", f"Roll back to: {option.rolls_back_to}", "", "PE will check afterwards that:"] + [f"- {check_phrase(c)}" for c in option.checks]
    return "\n".join(lines)[:8000]


def canonical(plan: Plan) -> dict[str, Any]:
    """The plan as stored: redacted string leaves, so nothing an agent smuggled into prose is kept as a secret."""
    return redact_value(plan.model_dump(mode="json"))


def plan_hash(plan: Plan) -> str:
    """The hash of the plan EXACTLY as it is stored and shown (`canonical`, redacted): a grant bound to this hash is bound to the
    content the operator reviewed, and re-parsing the stored JSON gives the same hash."""
    return sha256_hex(canonical(plan))


# ── generated from the catalogue (never hand-written) ─────────────────────────────────────────────

def _kinds(available: Any = None) -> list[str]:
    return available.kinds() if available is not None else sorted(PARAM_MODELS)


def catalogue_text(available: Any = None) -> str:
    """Every check kind this host can run (all of them when `available` is None) with its full JSON Schema, bounds and required
    fields included, for the prompt. A kind the host cannot run is not offered at all."""
    rows = []
    for kind in _kinds(available):
        schema = PARAM_MODELS[kind].model_json_schema()
        schema.pop("title", None)
        rows.append(f"  {kind}: {json.dumps(schema, separators=(',', ':'))}")
    if available is not None and "exec_probe" in _kinds(available):
        rows.append("  (exec_probe may run only these binaries, as argv[0]: " + ", ".join(sorted(available.exec_allowlist)) + ")")
    if available is not None and available.unavailable:
        rows.append("  Not available on this host, so never use: " + ", ".join(sorted(available.unavailable)))
    return "\n".join(rows)


def json_schema(available: Any = None) -> dict[str, Any]:
    """The plan contract as JSON Schema, for `--json-schema`. A real schema matters: a generic object made the model return
    `options` as a JSON string. PE still validates every plan itself with `parse`."""
    text = {"type": "string"}
    risk = {"type": "object", "properties": {"level": {"enum": ["low", "medium", "high"]}, "text": text},
            "required": ["level", "text"], "additionalProperties": False}
    check = {"type": "object", "properties": {"id": text, "check": {"enum": _kinds(available)}, "params": {"type": "object"}},
             "required": ["id", "check", "params"], "additionalProperties": False}
    names = ["name", "recommended", "summary", "changes", "will_not_touch", "risk", "rolls_back_to", "effort", "scope", "checks"]
    scope = {"type": "object", "additionalProperties": False, "required": ["stacks", "services"], "properties": {
        "stacks": {"type": "array", "items": text, "maxItems": 8}, "services": {"type": "array", "items": text, "maxItems": 32}}}
    option = {"type": "object", "additionalProperties": False, "required": names, "properties": {
        "name": text, "recommended": {"type": "boolean"}, "summary": text, "changes": {"type": "array", "items": text},
        "will_not_touch": {"type": "array", "items": text}, "risk": risk, "rolls_back_to": text, "effort": text,
        "scope": scope, "exclude": {"type": "array", "items": text, "maxItems": 32},
        "checks": {"type": "array", "items": check, "minItems": 1}}}
    return {"type": "object", "additionalProperties": False, "required": ["known", "unsure", "options"], "properties": {
        "known": {"type": "array", "items": text}, "unsure": {"type": "array", "items": text},
        "options": {"type": "array", "items": option, "minItems": 1, "maxItems": 3}}}


PLAN_INSTRUCTIONS = (
    "When the right response is a change, write your plan as plan.json in the current directory (use the Write tool). It is a JSON "
    "object with `known` (what the evidence shows), `unsure` (what it does not), and 1 to 3 `options`. Exactly one option has "
    "`recommended` true. Each option has: name, recommended, summary (one sentence), changes (services or files it would touch), "
    "will_not_touch, risk {level low|medium|high, text}, rolls_back_to, effort, scope, exclude (optional) and checks. `scope` is "
    "{stacks: [...], services: [...]}: what the option works on, using ONLY names listed in INVENTORY.md (services as "
    "<stack>/<service>). PE leases it, so no other run works on it at the same time, and checkpoints its compose files before the "
    "run. `exclude` lists resource ids such as service:<stack>/<service> the run is told to leave alone. The run that carries out "
    "a confirmed option has full access to this host (docker, sudo), as the operator has at a terminal. `checks` are how PE will "
    "verify the option worked, and may ONLY use these check kinds, with params that match the schema EXACTLY (respect every bound, "
    "no extra fields):\n{catalogue}\nWhen the fix is a restart, add a container_state check for each container it restarts with "
    "`started_during_mission: true`: PE then checks that the container really started again during the run, not only that it is "
    "running. Stateful changes back up data first: when an option pulls a new image for, or recreates, a service whose data could be "
    "migrated (a database, an *arr app with its SQLite db, anything with a config or data bind mount), its changes start with copying "
    "that data directory to `<dir>.pe-backup-<mission>-<UTC>` beside it with `sudo cp -a`, and its rolls_back_to says that backup is "
    "there (the checkpoint covers compose files, not data). You cannot approve a plan: the operator confirms an option, and only then is it carried out. Investigate as much as "
    "you need, but do not change anything on the host yourself before the operator confirms. If the operator says to go ahead with "
    "what you proposed, write (or refresh) plan.json for exactly that: PE then shows them the Authorise button; never answer that you "
    "cannot change the host. If PE refuses plan.json, it tells you exactly why: fix that and write the whole file again.")


# ── "go ahead" in the chat (rc14.2): a small fixed list, whole message, case-insensitive. Chat text is never the approval: with a
# valid plan it opens the same Authorise card as pressing Confirm; without one it asks the agent for the plan. ─────────────
_GO_AHEAD = re.compile(
    r"(?:please\s+|ok(?:ay)?[,\s]+|yes[,\s]+|yeah[,\s]+)*"
    r"(?:do it|go ahead|go for it|yes|yep|yeah|apply it|apply|apply the fix|authori[sz]e(?: it)?|approve(?: it)?|confirm(?: it)?|"
    r"proceed|run it|let'?s do it|fix it)"
    r"(?:\s+(?:please|now))?[\s.!]*", re.IGNORECASE)
_GO_OPTION = re.compile(r"(?:go (?:ahead )?with|do|apply|authori[sz]e|approve|confirm)\s+option\s+([a-c1-3])[\s.!]*", re.IGNORECASE)


def goes_ahead(text: str) -> tuple[bool, int | None]:
    """(is this whole message a "go ahead", the option it names (0-2) or None for the recommended one)."""
    words = " ".join(str(text or "").split())
    if len(words) > 60:
        return False, None
    named = _GO_OPTION.fullmatch(words)
    if named:
        letter = named.group(1).lower()
        return True, ("abc".index(letter) if letter in "abc" else int(letter) - 1)
    return bool(_GO_AHEAD.fullmatch(words)), None


GO_AHEAD_PROMPT = (
    "The operator wants to go ahead with what you proposed (they wrote: \"{text}\"). Write plan.json now for exactly what was "
    "discussed, following PLAN_FORMAT.md: PE then shows the operator the Authorise button. Do not change the host yourself, and do "
    "not answer that you cannot: the plan is how it gets done. Reply in a sentence or two saying what the plan does.")

RETRY_PROMPT = (
    "PE refused your plan.json: {reason}. Fix exactly that and write the whole plan.json again in the current directory (this turn "
    "runs in a new directory, so the old file is gone; PLAN_FORMAT.md has the format and the checks this host can run). Do not change "
    "the host. Reply in one sentence saying what you fixed.")


def instructions(available: Any = None) -> str:
    """PLAN_FORMAT.md: the check catalogue in it is THIS host's (`available`, a `verify.checks.Availability`; None offers every kind)."""
    return PLAN_INSTRUCTIONS.replace("{catalogue}", catalogue_text(available))


__all__ = ["GO_AHEAD_PROMPT", "MAX_PLAN_BYTES", "PLAN_FILE", "RETRY_PROMPT", "goes_ahead", "lenient", "Option", "Plan", "PlanError", "canonical", "catalogue_text", "check_phrase", "goal_text",
           "instructions", "json_schema", "load_file", "parse", "plan_hash", "read_doc"]
