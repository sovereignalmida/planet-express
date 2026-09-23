"""Typed planning (slice 5b-2): the planner chooses steps, never writes commands.

The model returns *intent* — a step type from the catalogue, or a code-owned recipe, with plain
params. This module turns that into a validated `Runbook` whose bindings, risk, verifiers and
rollback semantics all come from code (design §4, §5). Anything it cannot express is refused with a
reason, and the operator gets the diagnosis instead (D37).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import pydantic

import casa_bender as bender
import config
from planet_express.execution import actions, runbook as runbooks

log = logging.getLogger("planetexpress.planner")

MAX_PLANS = 5
MAX_STEPS = 12


class PlanRefused(Exception):
    """The model's plan cannot be expressed with the typed catalogue."""


PLAN_RUNBOOK_PROMPT = """You are Professor Hubert J. Farnsworth, planner for the Planet Express home lab.
You receive structured findings from Hermes (and, when present, real diagnostic evidence gathered
before planning) and choose REMEDIATION STEPS FROM A FIXED CATALOGUE. You never write shell
commands: you pick step types and their parameters, and Planet Express builds, verifies and (where
possible) reverses them.

Return ONLY valid JSON. No prose, no markdown fences.

STEP CATALOGUE (the only steps that exist):
- {"type": "service.restart", "params": {"stack": "<stack>", "service": "<service>"}}
    Restart one compose service. Verified by the container being healthy again afterwards.
- {"type": "service.start"  , "params": {"stack": "<stack>", "service": "<service>"}}
- {"type": "service.stop"   , "params": {"stack": "<stack>", "service": "<service>"}}
- {"type": "stack.up"       , "params": {"stack": "<stack>"}}
- {"type": "stack.down"     , "params": {"stack": "<stack>"}}
- {"type": "wait"           , "params": {"seconds": <1-300>}}
- {"type": "check.container", "params": {"expect": "running|healthy|stopped"},
   "container": "<CONTAINER_NAME>"}
- {"type": "unit.action"    , "params": {"action": "start|stop|restart", "unit": "<name>.service"}}
    Only units in the sudo allowlist (casa-stacks.service and *.mount units) can run; anything
    else is refused, so do not propose it.
- {"type": "prune.safe", "params": {}}   Remove images and networks no container uses.

RECIPES (preferred when they fit — they encode knowledge you do not have to restate):
- {"recipe": "vpn.resync_port_forward", "params": {"mode": "dead_forward"}}
    gluetun reports NO forwarded port (dead forward): restarts gluetun, waits for the tunnel, then
    restarts the two containers that share its network namespace, and verifies the new port really
    reached qBittorrent.
- {"recipe": "vpn.resync_port_forward", "params": {"mode": "sync_mismatch"}}
    gluetun has a valid port but qBittorrent is out of sync: restarts only the sync pair.

RULES:
- Group related findings into ONE plan per group.
- A step list runs in order and stops at the first failure. Put a `wait` between a restart and
  anything that depends on it.
- You do NOT need verification steps after a restart/start/stop: every step is verified from host
  state automatically. Add `check.container` only when a LATER step depends on that state.
- Never plan image updates or anything in update_candidates — Zoidberg handles those weekly.
- Never touch the clawbot or ai stacks, and never Traefik or AdGuard (they are network-guarded and
  are refused).
- If no catalogue step or recipe can fix a finding, DO NOT invent one: leave that finding out and
  say why in "needs_human". A diagnosis with no action is a valid, useful answer.
- Use the stack/service names exactly as they appear in the findings' `stack_completeness` or
  diagnostic evidence. For `check.container`, give the container name in "container".

OUTPUT SCHEMA — return exactly this, nothing else:
{
  "plans": [
    {
      "id": "p1",
      "priority": "critical|high|medium|low",
      "title": "short descriptive title",
      "finding_ids": ["f1"],
      "steps": [ {"type": "service.restart", "params": {"stack": "media", "service": "sonarr"}} ]
    }
  ],
  "needs_human": [ {"finding_ids": ["f2"], "why": "one short sentence"} ]
}

If nothing can be fixed with the catalogue, return an empty "plans" array and explain in
"needs_human"."""


# ── recipes ─────────────────────────────────────────────────────────────────────
VPN_GATEWAY = "CASA_GLUETON"
VPN_SYNC = "CASA_GSP"
VPN_CLIENT = "CASA_QBIT"
VPN_TUNNEL_WAIT_SECONDS = 20
VPN_PORT_MARKER = "New : "


def _vpn_resync(params: dict) -> list[dict]:
    """The gluetun/GSP/qBittorrent port-forward repair, as code (it was prose in the old prompt).

    `dead_forward` restarts the gateway first, waits for the tunnel and port-forward RPC, then the
    two containers that share its network namespace (they are orphaned on the old namespace by a
    gateway restart). `sync_mismatch` leaves the gateway alone. Both end by reading qBittorrent's
    configured port and requiring GSP's log to show that exact port since GSP last started.
    """
    mode = params.get("mode")
    if mode not in ("dead_forward", "sync_mismatch"):
        raise PlanRefused("vpn.resync_port_forward needs mode dead_forward or sync_mismatch")
    steps: list[dict] = []
    if mode == "dead_forward":
        steps += [
            {"type": "service.restart", "container": VPN_GATEWAY},
            {"type": "wait", "params": {"seconds": VPN_TUNNEL_WAIT_SECONDS}},
        ]
    steps += [
        {"type": "service.restart", "container": VPN_SYNC},
        {"type": "service.restart", "container": VPN_CLIENT},
        {"type": "wait", "params": {"seconds": 5}},
        {"type": "read.qbittorrent_session_port", "params": {}, "container": VPN_CLIENT},
        {"type": "check.log_since_start",
         "params": {"match": VPN_PORT_MARKER, "port": {"from_step": "$port", "output": "port"}},
         "container": VPN_SYNC},
    ]
    return steps


RECIPES: dict[str, Callable[[dict], list[dict]]] = {"vpn.resync_port_forward": _vpn_resync}


def _params_of(item: dict) -> dict:
    """A step's params, or a refusal. A model that answers `"params": "restart it"` must not reach
    `dict()` and crash the whole planning run (Codex, T41)."""
    params = item.get("params")
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise PlanRefused("a step's params must be an object")
    return params


def _first_error(exc: pydantic.ValidationError) -> str:
    """One readable line out of a validation error — the refusal log is read by a human."""
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"]) or "plan"
    return f"{where}: {error['msg']}"


@dataclass(frozen=True)
class TypedPlan:
    title: str
    runbook: runbooks.Runbook
    finding_ids: list
    priority: str


class RunbookBuilder:
    """Turns model intent into a validated runbook with server-built bindings."""

    def __init__(self, *, binder, resolve_target=None, resolve_identities=None,
                 resolve_stack=None):
        self._binder = binder
        self._resolve = resolve_target or actions.resolve_target
        self._resolve_stack = resolve_stack or actions.resolve_stack_target
        self._identities = resolve_identities or actions.container_compose_identities

    def _identity(self, container: str) -> tuple[str, str]:
        try:
            found = self._identities([container])
        except actions.TargetError as exc:
            raise PlanRefused(f"cannot identify {container}: {exc}") from None
        if container not in found:
            raise PlanRefused(f"{container} is not a compose-managed container on this host")
        return found[container]

    def _expand(self, item: dict) -> list[dict]:
        params = _params_of(item)
        if "recipe" in item:
            name = item["recipe"]
            if not isinstance(name, str):
                raise PlanRefused("a recipe name must be a string")
            recipe = RECIPES.get(name)
            if recipe is None:
                raise PlanRefused(f"unknown recipe {name!r}")
            return recipe(params)
        return [item]

    def build(self, plan: dict) -> TypedPlan:
        title = str(plan.get("title") or "").strip()
        if not title:
            raise PlanRefused("a plan needs a title")
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanRefused("a plan needs at least one step")
        expanded: list[dict] = []
        for item in raw_steps:
            if not isinstance(item, dict):
                raise PlanRefused("each step must be an object")
            expanded += self._expand(item)
        if len(expanded) > MAX_STEPS:
            raise PlanRefused(f"a plan may have at most {MAX_STEPS} steps")

        steps, produced = [], {}
        for index, item in enumerate(expanded, start=1):
            steps.append(self._step(item, index, produced))
        try:
            runbook = runbooks.Runbook.model_validate(
                {"title": title[:200], "steps": steps, "artifacts": {}}
            )
        except pydantic.ValidationError as exc:
            raise PlanRefused(f"the plan is not a valid runbook: {_first_error(exc)}") from None
        finding_ids = plan.get("finding_ids") or []
        if not isinstance(finding_ids, list):
            raise PlanRefused("finding_ids must be a list")
        return TypedPlan(title[:200], runbook, [str(f) for f in finding_ids],
                         str(plan.get("priority") or "medium"))

    def _step(self, item: dict, index: int, produced: dict) -> dict:
        step_type = item.get("type")
        if not isinstance(step_type, str):
            raise PlanRefused("a step type must be a string")
        spec = runbooks.STEP_TYPES.get(step_type)
        if spec is None:
            raise PlanRefused(f"unknown step type {step_type!r}")
        if step_type in ("stack.up_all", "stack.down_all", "stack.down_ingress", "compose.write"):
            raise PlanRefused(f"{step_type} is not available to the planner")
        params = dict(_params_of(item))
        for name, value in list(params.items()):
            # A recipe references an earlier step's output by name; resolve it to that step's number.
            if isinstance(value, dict) and value.get("from_step") == "$port":
                if "port" not in produced:
                    raise PlanRefused("a port reference has no producing step")
                params[name] = {"from_step": produced["port"], "output": "port"}
        binding = self._binding(step_type, item, params)
        # Targeting params the step itself does not take (a container-scoped check given a
        # stack/service) did their job in the binding; they are not part of the document.
        params = {name: value for name, value in params.items()
                  if name in spec.params_model.model_fields}
        try:
            spec.params_model.model_validate(params)
        except pydantic.ValidationError as exc:
            raise PlanRefused(f"{step_type}: {_first_error(exc)}") from None
        if "port" in spec.outputs:
            produced["port"] = index
        return {"type": step_type, "params": params, "binding": binding}

    def _binding(self, step_type: str, item: dict, params: dict) -> dict:
        if step_type == "wait" or step_type == "prune.safe":
            return {}
        if step_type == "unit.action":
            unit, action = str(params.get("unit", "")), str(params.get("action", ""))
            try:
                # Fail here rather than after the operator approves a plan that cannot run:
                # the same check the engine makes, against the same declared allowlist.
                bender._check_sudo_allowlist(f"sudo systemctl {action} {unit}")
            except bender.SafetyError as exc:
                raise PlanRefused(f"unit.action is not in the sudo allowlist: {exc}") from None
            return {"unit": unit}
        if step_type in ("stack.up", "stack.down"):
            stack = params.get("stack")
            if not isinstance(stack, str):
                raise PlanRefused(f"{step_type} needs a stack")
            # The planner's own exclusions, which are stricter than an operator's: the shared
            # resolver only refuses forbidden stacks on the way *up* and only guards ingress on the
            # way down, and neither is something a model may propose at all (Codex, T41).
            if stack in config.FORBIDDEN_STACKS:
                raise PlanRefused(f"stack {stack!r} is forbidden")
            if actions.is_ingress_stack(stack):
                raise PlanRefused(f"stack {stack!r} is ingress and is not the planner's to move")
            try:
                # Then the same resolver every stack action uses, for everything else it enforces.
                self._resolve_stack(
                    actions.UP_STACK if step_type == "stack.up" else actions.DOWN_STACK, stack)
                return self._binder.stack(stack, timeout=actions.DOCKER_TIMEOUT_SECONDS)
            except actions.TargetError as exc:
                raise PlanRefused(f"cannot bind stack {stack}: {exc}") from None
        # service and container steps
        container = item.get("container")
        if container is not None:
            # The model names a container; the stack/service identity comes from Docker's own
            # compose labels, never from the model.
            stack, service = self._identity(str(container))
            # The labels win: a model that names a container cannot also mislabel its stack.
            fields = runbooks.STEP_TYPES[step_type].params_model.model_fields
            if "stack" in fields:
                params["stack"] = stack
            if "service" in fields:
                params["service"] = service
        else:
            stack, service = params.get("stack"), params.get("service")
            if not isinstance(stack, str) or not isinstance(service, str):
                raise PlanRefused(f"{step_type} needs a stack and service (or a container)")
        mutating = runbooks.STEP_TYPES[step_type].risk != "R0"
        try:
            target = self._resolve(stack, service, for_mutation=mutating)
            return self._binder.service(target, timeout=actions.DOCKER_TIMEOUT_SECONDS)
        except actions.TargetError as exc:
            raise PlanRefused(f"cannot bind {stack}/{service}: {exc}") from None


def parse_plans(raw: str) -> tuple[list[dict], list[dict]]:
    """The model's JSON → (plans, needs_human). Malformed output yields nothing, never an exception."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanRefused(f"the planner's response did not parse: {exc}") from None
    if not isinstance(data, dict):
        raise PlanRefused("the planner's response was not an object")
    plans = [p for p in (data.get("plans") or []) if isinstance(p, dict)][:MAX_PLANS]
    needs_human = [n for n in (data.get("needs_human") or []) if isinstance(n, dict)]
    return plans, needs_human
