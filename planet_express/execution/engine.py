"""The runbook engine (slice 5b-1, design §4.4): runs an approved, validated runbook step by step.

    for each step:  abort requested? ─► aborted
                    re-resolve + drift check against the approved binding ─► failed / not_applied
                    resolve references (earlier steps' persisted outputs)
                    record pre_state ─► consume attempt ─► mark dispatched ─► argv (no shell)
                    effect from host state ─► verifier ─► finish (outputs validated first)
    first failure stops the run; later steps are skipped and their attempts released.

The caller (CommandService) holds the host-mutation lock for the whole run and owns notifications.
Executors reach the host only through the service's ports (`svc._run_argv`, `svc._resolve`,
`svc._verify`, …), looked up at call time so tests and callers can substitute them, plus the
`Binder` for bookkeeping reads. Every stored or notified text is redacted.
"""

import logging
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import casa_bender as bender
import config
from planet_express.core.redact import redact
from planet_express.execution import actions, policy, runbook as runbooks

log = logging.getLogger("planetexpress.engine")

LEGACY_STEP_TYPES = {
    actions.RESTART_SERVICE: "service.restart",
    actions.UP_STACK: "stack.up",
    actions.DOWN_STACK: "stack.down",
    actions.UP_ALL: "stack.up_all",
    actions.DOWN_ALL: "stack.down_all",
    actions.DOWN_INGRESS: "stack.down_ingress",
}
STACK_STEP_ACTIONS = {step_type: action for action, step_type in LEGACY_STEP_TYPES.items()
                      if step_type.startswith("stack.")}
UP_STEP_TYPES = frozenset({"stack.up", "stack.up_all"})

REASON_LIMIT = 300
LOG_CAPTURE_BYTES = 1024 * 1024
QBIT_CONFIG = "/config/qBittorrent/qBittorrent.conf"
_NO_LIMIT = 10**9
# Canary timings: the watch and the grace window Zoidberg has used since the feature shipped.
CANARY_WATCH_SECONDS = 90
CANARY_PULL_TIMEOUT_SECONDS = 600
CANARY_ROLLBACK_WATCH_SECONDS = 30
CANARY_CANDIDATE_GRACE_SECONDS = 15 * 60

_STARTED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")


def unit_active(unit: str) -> str:
    """`systemctl is-active` exits non-zero for inactive/failed units, so only an empty answer or a
    timeout means the state could not be read — never silently call that "stopped" (Codex review)."""
    rc, out, err = bender.run_argv(["systemctl", "is-active", unit],
                                   timeout=actions.DOCKER_TIMEOUT_SECONDS)
    value = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT or not value:
        raise actions.TargetError(f"could not read the state of {unit}: {_clip(err) or 'no answer'}")
    return value


def _clip(text: str) -> str:
    return redact((text or "").strip())[:REASON_LIMIT]


@dataclass
class StepOutcome:
    status: str  # passed | failed
    effect: str  # applied | not_applied | unknown
    reason: str
    output: dict | None = None


@dataclass
class EngineResult:
    status: str  # passed | failed | aborted
    reason: str
    steps: list = field(default_factory=list)


class _Dispatch:
    """Handed to an executor: call it immediately before the first mutating argv."""

    def __init__(self, engine, execution_id: str, n: int, mutating: bool):
        self._engine, self._execution_id, self._n, self._mutating = engine, execution_id, n, mutating
        self.n = n          # executors that record their own rows (the canary candidate) need it
        self.done = False

    def __call__(self, pre_state: dict | None = None) -> None:
        if self.done:
            return
        store = self._engine.svc._store
        if pre_state is not None:
            store.set_step_pre_state(self._execution_id, self._n, pre_state)
        if self._mutating:
            store.consume_attempt(self._execution_id, self._n)
        store.mark_step_dispatched(self._execution_id, self._n)
        self.done = True


class RunbookEngine:
    def __init__(
        self, svc, *, is_abort_requested: Callable[[], bool] | None = None,
        on_failure: Callable[[dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    ):
        self.svc = svc
        self._is_abort_requested = is_abort_requested or (lambda: False)
        self._on_failure = on_failure
        self._sleep = sleep
        self._monotonic = monotonic

    # ── run ─────────────────────────────────────────────────────────────────
    def run(self, execution_id: str, runbook: runbooks.Runbook, *, origin: str) -> EngineResult:
        store = self.svc._store
        store.create_steps(execution_id, runbook.steps)
        mutating = [
            (n, step.type, runbooks._target_key(step, runbooks.STEP_TYPES[step.type]))
            for n, step in enumerate(runbook.steps, start=1)
            if runbooks.STEP_TYPES[step.type].risk != "R0"
        ]
        # Proposal checks the limits too, but two different plans can each hold a card for the same
        # target, so the binding check has to happen again — atomically — where the attempts are
        # actually reserved (Codex, T41). Operator origins reserve without a cap, as they always have.
        now = self.svc._clock()
        if mutating:
            enforced = origin not in policy.OPERATOR_ORIGINS
            autonomy = config.AUTONOMY
            refusal = store.reserve_runbook_attempts(
                execution_id, mutating,
                window_start=now - policy.DAY_SECONDS,
                cooldown_start=(now - autonomy.cooldown_seconds) if enforced else now + 1,
                max_per_day=autonomy.max_attempts_per_day if enforced else _NO_LIMIT, now=now,
            )
            if refusal is not None:
                # Blame the step whose target is at its limit, not whatever step happens to be
                # first: the steps around it were never evaluated either (Codex, T41).
                store.finish_step(execution_id, refusal.step_n, status="failed",
                                  effect="not_applied", reason=f"refused: {refusal.reason}")
                for n in range(1, len(runbook.steps) + 1):
                    if n != refusal.step_n:
                        store.finish_step(execution_id, n, status="skipped",
                                          reason=f"not run: {refusal.reason}")
                return EngineResult("failed", refusal.reason, [])
        mutating_ns = {n for n, _type, _key in mutating}
        results = []
        for n, step in enumerate(runbook.steps, start=1):
            if self._is_abort_requested():
                self._abandon(execution_id, runbook, n, "aborted", "aborted by the operator")
                return EngineResult("aborted", "aborted by the operator", results)
            dispatch = _Dispatch(self, execution_id, n, n in mutating_ns)
            try:
                outcome = self._execute(execution_id, n, step, runbook, dispatch)
            except Exception as exc:
                log.exception(f"Step {n} of {execution_id} crashed")
                outcome = StepOutcome(
                    "failed", "unknown" if dispatch.done else "not_applied",
                    f"crashed: {_clip(str(exc))}",
                )
            if outcome.status == "aborted":
                store.finish_step(execution_id, n, status="failed", effect="not_applied",
                                  reason=outcome.reason)
                if n in mutating_ns:
                    store.release_attempts(execution_id, [n])
                self._abandon(execution_id, runbook, n + 1, "aborted", "aborted by the operator")
                return EngineResult("aborted", outcome.reason, results)
            output = None
            if runbooks.STEP_TYPES[step.type].outputs and (
                    outcome.status == "passed" or outcome.output is not None):
                try:
                    output = runbooks.validate_outputs(step.type, outcome.output or {})
                except ValueError:
                    # A passing step must produce what it declares; a failed one records whatever
                    # it managed to learn, and a partial record is dropped rather than raised
                    # (a canary that deployed and rolled back still reports both image ids).
                    if outcome.status == "passed":
                        raise
                    log.warning(f"Step {n} of {execution_id} reported unusable outputs")
            if not dispatch.done:
                if outcome.status == "passed":
                    dispatch()  # a step that never reached argv (e.g. a pure check) still passes
                else:
                    outcome.effect = "not_applied"
                    if n in mutating_ns:
                        store.release_attempts(execution_id, [n])
            store.finish_step(execution_id, n, status=outcome.status, effect=outcome.effect,
                              output=output, reason=_clip(outcome.reason))
            results.append(outcome)
            if outcome.status != "passed":
                self._abandon(execution_id, runbook, n + 1, "skipped", f"not run: step {n} failed")
                self._report_failure(execution_id, runbook, n, outcome)
                return EngineResult("failed", outcome.reason, results)
            if n < len(runbook.steps):
                store.set_execution_status(execution_id, "running")
        return EngineResult("passed", results[-1].reason if results else "", results)

    def _abandon(self, execution_id: str, runbook, first_n: int, status: str, reason: str) -> None:
        store = self.svc._store
        remaining = list(range(first_n, len(runbook.steps) + 1))
        for n in remaining:
            store.finish_step(execution_id, n, status=status, reason=reason)
        store.release_attempts(execution_id, remaining)

    def _report_failure(self, execution_id: str, runbook, n: int, outcome: StepOutcome) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure({
                "execution_id": execution_id, "title": runbook.title, "failed_step": n,
                "type": runbook.steps[n - 1].type, "binding": runbook.steps[n - 1].binding,
                "reason": _clip(outcome.reason), "effect": outcome.effect,
                "steps": self.svc._store.list_steps(execution_id),
            })
        except Exception:  # noqa: BLE001 -- investigation is best-effort, never affects the outcome
            log.warning(f"Post-failure hook failed for {execution_id}")

    # ── references ──────────────────────────────────────────────────────────
    def _resolved_params(self, execution_id: str, step: runbooks.Step) -> dict:
        params = dict(step.params)
        steps = None
        for name, value in step.params.items():
            if isinstance(value, dict) and set(value) == {"from_step", "output"}:
                if steps is None:
                    steps = {row["n"]: row for row in self.svc._store.list_steps(execution_id)}
                producer = steps.get(value["from_step"])
                if producer is None or producer["status"] != "passed" or not producer["output"]:
                    raise ValueError(f"step {value['from_step']} produced no {value['output']!r}")
                outputs = runbooks.validate_outputs(producer["type"], producer["output"])
                params[name] = outputs[value["output"]]
        return params

    # ── executors ───────────────────────────────────────────────────────────
    def _execute(self, execution_id, n, step, runbook, dispatch) -> StepOutcome:
        params = self._resolved_params(execution_id, step)
        handler = {
            "service.restart": self._service_restart,
            "service.start": self._service_start_stop,
            "service.stop": self._service_start_stop,
            "wait": self._wait,
            "check.container": self._check_container,
            "check.log_since_start": self._check_log,
            "read.qbittorrent_session_port": self._read_qbit_port,
            "unit.action": self._unit_action,
            "update.canary": self._canary,
            "prune.safe": self._prune,
        }.get(step.type)
        if step.type in STACK_STEP_ACTIONS:
            return self._stack(execution_id, step, dispatch)
        if handler is None:
            return StepOutcome("failed", "not_applied", f"no executor for {step.type}")
        return handler(execution_id, step, params, dispatch)

    def _refuse(self, reason: str) -> StepOutcome:
        return StepOutcome("failed", "not_applied", reason)

    def _bound_target(self, step) -> tuple[actions.Target | None, StepOutcome | None]:
        """Re-resolve a service step and compare it with its approved binding (design §4.2)."""
        binding = step.binding
        stack = step.params.get("stack") or binding["compose_path"].rstrip("/").split("/")[-2]
        service = step.params.get("service") or binding["service"]
        try:
            target = self.svc._resolve(stack, service, for_mutation=True)
        except actions.TargetError as exc:
            return None, self._refuse(f"target no longer valid: {exc}")
        if target.container != binding["container"]:
            return None, self._refuse(
                f"container changed since approval ({binding['container']} → {target.container})"
            )
        try:
            current = self.svc._binder.service(target, timeout=actions.DOCKER_TIMEOUT_SECONDS)
        except actions.TargetError as exc:
            return None, self._refuse(f"target no longer valid: {exc}")
        if current["compose_sha256"] != binding["compose_sha256"]:
            return None, self._refuse("compose file changed since approval")
        if current["container_id"] != binding["container_id"]:
            return None, self._refuse(
                f"container {binding['container']} was recreated since approval"
            )
        return target, None

    def _service_restart(self, execution_id, step, params, dispatch) -> StepOutcome:
        target, refused = self._bound_target(step)
        if refused:
            return refused
        baseline = self.svc._restart_count(target.container)
        dispatch()
        rc, out, err = self.svc._run_argv(actions.restart_argv(target), timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0:
            return StepOutcome("failed", "unknown",
                               f"restart command failed (exit {rc}): {_clip(err or out)}")
        self.svc._store.set_execution_status(execution_id, "verifying")
        ok, reason = self.svc._verify(target.container, baseline)
        return StepOutcome("passed" if ok else "failed", "applied", reason)

    def _health(self, container):
        return self.svc._read_health(container)

    def _service_start_stop(self, execution_id, step, params, dispatch) -> StepOutcome:
        target, refused = self._bound_target(step)
        if refused:
            return refused
        verb = "start" if step.type == "service.start" else "stop"
        before = self._health(target.container)
        if before.error is not None:
            return self._refuse(f"inspect failed: {_clip(before.error)}")
        was_running = before.status == "running"
        dispatch({"running": was_running})
        argv = ["docker", "compose", "-f", str(actions.compose_file(target.stack)), verb, target.service]
        rc, out, err = self.svc._run_argv(argv, timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0:
            return StepOutcome("failed", "unknown", f"{verb} command failed (exit {rc}): {_clip(err or out)}")
        self.svc._store.set_execution_status(execution_id, "verifying")
        if verb == "start":
            effect = "not_applied" if was_running else "applied"
            ok, reason = self.svc._verify(target.container, None)
            return StepOutcome("passed" if ok else "failed", effect, reason)
        after = self._health(target.container)
        if after.error is None and after.status != "running":
            return StepOutcome("passed", "applied" if was_running else "not_applied", "stopped")
        return StepOutcome("failed", "unknown", f"still {after.status or 'unknown'} after stop")

    def _stack(self, execution_id, step, dispatch) -> StepOutcome:
        """T37's stack execution, unchanged in behaviour and report text, plus binding drift."""
        action = STACK_STEP_ACTIONS[step.type]
        if action in actions.ALL_STACK_ACTIONS:
            approved = {"scope": "all", "stacks": [item["stack"] for item in step.binding["stacks"]]}
            bound = {item["stack"]: item for item in step.binding["stacks"]}
            selector = "all"
        else:
            approved = {"stack": step.params["stack"]}
            bound = {step.params["stack"]: step.binding}
            selector = step.params["stack"]
        try:
            target = self.svc._resolve_for_action(
                action, selector, None, timeout=actions.DOCKER_TIMEOUT_SECONDS, approved_target=approved,
            )
        except actions.TargetError as exc:
            return self._refuse(f"target no longer valid: {exc}")
        stacks = list(target.stacks) if isinstance(target, actions.AllStacksTarget) else [target.stack]
        for stack in stacks:
            try:
                current = self.svc._binder.stack(stack, timeout=actions.DOCKER_TIMEOUT_SECONDS)
            except actions.TargetError as exc:
                return self._refuse(f"target no longer valid: {exc}")
            if stack not in bound or current["compose_sha256"] != bound[stack]["compose_sha256"]:
                return self._refuse(f"compose file of {stack} changed since approval")
            # An include: or profile change can move the service set without touching the top-level
            # file's hash — the approval named these services (Codex review, 5b-1).
            if list(current["services"]) != list(bound[stack]["services"]):
                return self._refuse(f"the services of {stack} changed since approval")
        passed = []
        for index, stack in enumerate(stacks):
            dispatch()
            rc, out, err = self.svc._run_argv(
                actions.stack_argv(action, stack), timeout=actions.STACK_VERIFY_TIMEOUT_SECONDS
            )
            if rc != 0:
                report = self.svc._stack_report(
                    passed, stack, f"compose exited {rc}: {_clip(err or out)}", stacks[index + 1:]
                )
                return StepOutcome("failed", "applied" if passed else "unknown", report)
            self.svc._store.set_execution_status(execution_id, "verifying")
            verifier = self.svc._verify_stack_up if step.type in UP_STEP_TYPES else self.svc._verify_stack_down
            ok, reason = verifier(stack)
            if not ok:
                return StepOutcome("failed", "applied",
                                   self.svc._stack_report(passed, stack, reason, stacks[index + 1:]))
            passed.append(stack)
            if index + 1 < len(stacks):
                self.svc._store.set_execution_status(execution_id, "running")
        output = None
        if step.type == "stack.up":
            try:
                output = {"services": self.svc._binder.stack_containers(
                    stacks[0], step.binding["services"], timeout=actions.DOCKER_TIMEOUT_SECONDS,
                )}
            except actions.TargetError as exc:
                # The stack IS up and verified; only its outputs could not be recorded, which later
                # steps would need. Say exactly that instead of reporting a crash (own review, T39).
                return StepOutcome("failed", "applied",
                                   f"{stacks[0]} is up and verified, but its container identities "
                                   f"could not be recorded: {_clip(str(exc))}")
        return StepOutcome("passed", "applied", self.svc._stack_report(passed, None, None, []), output)

    def _wait(self, execution_id, step, params, dispatch) -> StepOutcome:
        dispatch()
        deadline = self._monotonic() + params["seconds"]
        while True:
            left = deadline - self._monotonic()
            if left <= 0:
                return StepOutcome("passed", "not_applied", f"waited {params['seconds']}s")
            if self._is_abort_requested():
                return StepOutcome("aborted", "not_applied", "aborted by the operator during wait")
            self._sleep(min(1.0, left))

    def _bound_container(self, step) -> tuple[str | None, StepOutcome | None]:
        binding = step.binding
        # Container-only steps (check.*, read.*) carry the full service binding, so they get the same
        # drift protection as a mutating step (Codex review, 5b-1).
        try:
            current = self.svc._binder.service(
                actions.Target(binding["project"], binding["service"], binding["container"]),
                timeout=actions.DOCKER_TIMEOUT_SECONDS,
            )
        except actions.TargetError as exc:
            return None, self._refuse(f"target no longer valid: {exc}")
        if current["compose_sha256"] != binding["compose_sha256"]:
            return None, self._refuse("compose file changed since approval")
        # Binder.service() already inspected the container: one docker call, not two
        # (Codex review round 2, 5b-1).
        if current["container_id"] != binding["container_id"]:
            return None, self._refuse(f"container {binding['container']} was recreated since approval")
        return binding["container"], None

    def _check_container(self, execution_id, step, params, dispatch) -> StepOutcome:
        container, refused = self._bound_container(step)
        if refused:
            return refused
        reading = self._health(container)
        if reading.error is not None:
            return StepOutcome("failed", "not_applied", f"inspect failed: {_clip(reading.error)}")
        expect = params["expect"]
        ok = {
            "running": reading.status == "running",
            "healthy": reading.status == "running" and reading.health in ("healthy", "none"),
            "stopped": reading.status != "running",
        }[expect]
        detail = f"{container}: status={reading.status}, health={reading.health}"
        return StepOutcome("passed" if ok else "failed", "not_applied", detail)

    def _check_log(self, execution_id, step, params, dispatch) -> StepOutcome:
        container, refused = self._bound_container(step)
        if refused:
            return refused
        needle = params["match"] + (str(params["port"]) if params.get("port") is not None else "")
        rc, started, err = bender.run_argv(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
            timeout=actions.DOCKER_TIMEOUT_SECONDS,
        )
        started = started.strip()
        # Docker's RFC 3339 StartedAt, passed back as --since; anything else is refused, never
        # handed to the CLI (argv, no shell, but still only a value we recognise).
        if rc != 0 or _STARTED_AT_RE.fullmatch(started) is None:
            return StepOutcome("failed", "not_applied", f"could not read StartedAt: {_clip(err)}")
        rc, out, err, _truncated = bender.run_argv_bounded(
            ["docker", "logs", "--since", started, container],
            timeout=actions.DOCKER_TIMEOUT_SECONDS, max_bytes=LOG_CAPTURE_BYTES,
        )
        if rc != 0:
            return StepOutcome("failed", "not_applied", f"docker logs failed: {_clip(err)}")
        found = needle in out or needle in err
        return StepOutcome("passed" if found else "failed", "not_applied",
                           f"{'found' if found else 'did not find'} {needle!r} since {started}")

    def _read_qbit_port(self, execution_id, step, params, dispatch) -> StepOutcome:
        container, refused = self._bound_container(step)
        if refused:
            return refused
        # Only the one line: the file also holds the WebUI username and password hash.
        rc, out, _err = bender.run_argv(
            ["docker", "exec", container, "grep", "-m", "1", "^Session\\\\Port=", QBIT_CONFIG],
            timeout=actions.DOCKER_TIMEOUT_SECONDS,
        )
        line = out.strip().splitlines()[0] if out.strip() else ""
        value = line.partition("=")[2].strip()
        if rc != 0 or not value.isdigit() or not 1 <= int(value) <= 65535:
            return StepOutcome("failed", "not_applied", "could not read qBittorrent's Session\\Port")
        return StepOutcome("passed", "not_applied", f"port {int(value)}", {"port": int(value)})

    def _unit_active(self, unit: str) -> str:
        return unit_active(unit)

    def _unit_action(self, execution_id, step, params, dispatch) -> StepOutcome:
        action, unit = params["action"], params["unit"]
        try:
            bender._check_sudo_allowlist(f"sudo systemctl {action} {unit}")
        except bender.SafetyError as exc:
            return self._refuse(f"not in the sudo allowlist: {_clip(str(exc))}")
        try:
            before = self._unit_active(unit)
        except actions.TargetError as exc:
            return self._refuse(str(exc))
        dispatch({"active": before})
        rc, out, err = self.svc._run_argv(["sudo", "-n", "systemctl", action, unit],
                                          timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0:
            return StepOutcome("failed", "unknown", f"systemctl {action} failed (exit {rc}): {_clip(err or out)}")
        try:
            after = self._unit_active(unit)
        except actions.TargetError as exc:
            # The command ran; we just cannot confirm the result, so the effect is unknown.
            return StepOutcome("failed", "unknown", str(exc))
        want_active = action in ("start", "restart")
        ok = (after == "active") == want_active
        if action == "restart":
            effect = "applied"
        else:
            effect = "applied" if (before == "active") != (after == "active") else "not_applied"
        return StepOutcome("passed" if ok else "failed", effect, f"{unit} is {after}")

    # ── canary updates (slice 5b-3, design §4.5) ────────────────────────────
    def _image_id(self, argv, *, allow_missing: bool = False) -> tuple[str | None, str | None]:
        """(image id, error). A missing image is only ever `None, None` where the caller says so."""
        rc, out, err = self.svc._run_argv(argv, timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0 or not out.strip():
            if allow_missing and rc != 0:
                return None, None
            return None, _clip(err or out or "no image id")
        return actions.normalize_image_id(out.strip().splitlines()[0]), None

    def _canary(self, execution_id, step, params, dispatch) -> StepOutcome:
        """Two-phase, per design §4.5: resolve and pull, then deploy that exact image, with an
        automatic inverse back to the image that was running if anything goes wrong."""
        target, refused = self._bound_target(step)
        if refused:
            return refused
        stack, service, container = target.stack, target.service, target.container

        rc, out, err = self.svc._run_argv(
            actions.compose_config_images_argv(stack, service), timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0 or not out.strip():
            return self._refuse(f"cannot resolve the image reference: {_clip(err or out)}")
        reference = out.strip().splitlines()[0]
        if not actions.is_canary_reference(reference):
            # Digest-pinned or build-only: there is no mutable tag to move (design §4.5).
            return self._refuse(f"{reference} is not a canary-eligible image reference")
        running_id, error = self._image_id(actions.compose_images_argv(stack, service))
        if error is not None:
            return self._refuse(f"cannot read the running image: {error}")
        before_id, _missing = self._image_id(actions.image_id_argv(reference), allow_missing=True)

        pre_state = {"phase": "pull_dispatched", "compose_sha256": step.binding["compose_sha256"],
                     "image_reference": reference, "running_image_id": running_id,
                     "reference_image_id": before_id, "container": container}
        # The window opens before the pull, so a prune can never remove the image this step would
        # roll back to — and it closes again if the step never actually dispatched.
        self.svc._store.open_rollback_candidate(
            execution_id, dispatch.n, stack=stack, service=service, image_reference=reference,
            old_image_id=running_id,
            expires_at=self.svc._clock() + CANARY_CANDIDATE_GRACE_SECONDS,
        )
        try:
            dispatch(pre_state)
        except Exception:
            self.svc._store.close_rollback_candidate(execution_id, dispatch.n)
            raise
        closed = False
        try:
            rc, out, err = self.svc._run_argv(
                actions.compose_pull_argv(stack, service), timeout=CANARY_PULL_TIMEOUT_SECONDS)
            if rc != 0:
                closed = True  # nothing was deployed, so nothing needs a rollback window
                return StepOutcome("failed", "not_applied", f"pull failed: {_clip(err or out)}")
            new_id, error = self._image_id(actions.image_id_argv(reference))
            if error is not None:
                closed = True
                return StepOutcome("failed", "not_applied", f"cannot read the pulled image: {error}")
            self.svc._store.note_step_progress(
                execution_id, dispatch.n, {"phase": "pulled", "pulled_image_id": new_id})
            if new_id == running_id:
                closed = True
                # A passing step still reports what it looked at: nothing moved, and the card and
                # the history say which image that was.
                return StepOutcome("passed", "not_applied", f"already on the current {reference}",
                                   {"image_reference": reference, "old_image_id": running_id,
                                    "new_image_id": new_id})

            # Phase 2 deploys exactly what phase 1 resolved: refuse if anything moved under us.
            # A pull can take minutes, which is plenty of time for the container to be recreated.
            drifted = self._canary_drift(step, target, reference)
            if drifted is not None:
                closed = True
                return StepOutcome("failed", "not_applied", drifted)
            outcome = self._canary_deploy(execution_id, dispatch.n, target, reference,
                                          running_id, new_id)
            closed = outcome.effect != "unknown"
            return outcome
        finally:
            # Any exit that is not a clean "nothing is in flight" — a failed inverse, or a crash
            # anywhere after dispatch — must outlive the ordinary grace period, or the next prune
            # removes the only image that can restore the service (Codex, T42).
            if closed:
                self.svc._store.close_rollback_candidate(execution_id, dispatch.n)
            else:
                self.svc._store.hold_rollback_candidate(execution_id, dispatch.n)

    def _canary_drift(self, step, target, reference: str) -> str | None:
        """The full bound-target check again (compose file, container name **and id**), plus the
        image reference the pull resolved against (Codex, T42)."""
        _target, refused = self._bound_target(step)
        if refused is not None:
            return f"{refused.reason} during the update"
        rc, out, _err = self.svc._run_argv(
            actions.compose_config_images_argv(target.stack, target.service),
            timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0 or out.strip().splitlines()[:1] != [reference]:
            return "the image reference changed during the update"
        return None

    def _canary_deploy(self, execution_id, step_n, target, reference, old_id, new_id) -> StepOutcome:
        outputs = {"image_reference": reference, "old_image_id": old_id, "new_image_id": new_id}
        self.svc._store.note_step_progress(execution_id, step_n, {"phase": "deploying"})
        failure = self._canary_up(target, new_id, reference)
        if failure is None:
            self.svc._store.set_execution_status(execution_id, "verifying")
            stable, reason = self.svc._watch(target.container, CANARY_WATCH_SECONDS)
            if stable:
                return StepOutcome("passed", "applied",
                                   f"{reference}: {old_id[:12]} → {new_id[:12]}, {reason}", outputs)
            failure = f"canary watch failed: {reason}"
        # Automatic inverse: put the image that was running back on the same reference — and watch
        # it too. An old image that crash-loops on restore is not a successful rollback, and saying
        # so would close the window that protects it (Codex, T42).
        self.svc._store.note_step_progress(execution_id, step_n, {"phase": "rolling_back"})
        undone = self._canary_up(target, old_id, reference)
        if undone is None:
            stable, reason = self.svc._watch(target.container, CANARY_ROLLBACK_WATCH_SECONDS)
            if stable:
                return StepOutcome("failed", "not_applied",
                                   f"{failure}; rolled back to {old_id[:12]}", outputs)
            undone = f"the restored image is not stable: {reason}"
        return StepOutcome("failed", "unknown", f"{failure}; rollback also failed: {undone}", outputs)

    def _canary_up(self, target, image_id: str, reference: str) -> str | None:
        """Point the reference at this exact image and recreate the service on it. None = done."""
        rc, out, err = self.svc._run_argv(actions.tag_argv(image_id, reference),
                                          timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0:
            return f"tagging {image_id[:12]} failed: {_clip(err or out)}"
        rc, out, err = self.svc._run_argv(
            actions.compose_up_pinned_argv(target.stack, target.service),
            timeout=actions.DOCKER_TIMEOUT_SECONDS)
        if rc != 0:
            return f"up -d failed: {_clip(err or out)}"
        deployed, error = self._image_id(actions.container_image_id_argv(target.container))
        if error is not None:
            return f"cannot read the deployed image: {error}"
        if deployed != image_id:
            return f"container runs {deployed[:12]}, expected {image_id[:12]}"
        return None

    def _prune(self, execution_id, step, params, dispatch) -> StepOutcome:
        dispatch()
        reports, all_ok, any_ok = [], True, False
        for label, command in bender.SAFE_PRUNE_STEPS:
            rc, _out, err, _truncated = bender.run_argv_bounded(
                shlex.split(command), timeout=bender.COMMAND_TIMEOUT_SECONDS, max_bytes=64 * 1024,
            )
            all_ok &= rc == 0
            any_ok |= rc == 0
            reports.append(f"{label}: {'ok' if rc == 0 else f'failed ({_clip(err)})'}")
        return StepOutcome("passed" if all_ok else "failed",
                           "applied" if any_ok else "unknown", "; ".join(reports))


def settle_crashed_execution(store, execution_id: str) -> None:
    """The engine itself raised: close this execution's open steps and attempts so none is left
    `pending`/`dispatched` under a failed execution (own review, T39)."""
    for row in store.list_steps(execution_id):
        if row["status"] == "dispatched":
            spec = runbooks.STEP_TYPES.get(row["type"])
            effect = "not_applied" if spec is not None and spec.risk == "R0" else "unknown"
            store.finish_step(execution_id, row["n"], status="failed", effect=effect,
                              reason="the engine stopped unexpectedly")
        elif row["status"] == "pending":
            store.finish_step(execution_id, row["n"], status="skipped",
                              reason="not run: the engine stopped unexpectedly")
    with store._write() as conn:
        conn.execute(
            "UPDATE attempts SET state = CASE WHEN EXISTS (SELECT 1 FROM execution_steps s "
            "WHERE s.execution_id = attempts.execution_id AND s.n = attempts.step_n "
            "AND s.effect = 'unknown') THEN 'consumed' ELSE 'released' END, settled_at = ? "
            "WHERE execution_id = ? AND state = 'reserved'",
            (store._clock(), execution_id),
        )


def _reconcile_canary(svc, row: dict) -> tuple[str, str]:
    """A canary step interrupted mid-flight: what is the service actually running now?

    Comparing the container's image with the one recorded before the pull answers it without
    guessing — still the old image means the deploy never happened; the new one means it did but
    was never watched, so the outcome stays `unknown` and its rollback window stays open
    (design §4.5).
    """
    pre = row.get("pre_state") or {}
    container, old_id = pre.get("container"), pre.get("running_image_id")
    if svc is None or not container or not old_id:
        return "unknown", "interrupted: Planet Express restarted mid-update"
    rc, out, _err = svc._run_argv(actions.container_image_id_argv(container),
                                  timeout=actions.DOCKER_TIMEOUT_SECONDS)
    if rc != 0 or not out.strip():
        return "unknown", "interrupted mid-update; the container could not be inspected"
    current = actions.normalize_image_id(out.strip().splitlines()[0])
    if pre.get("phase") == "rolling_back":
        # The old image being back does not mean the rollback is good: the normal path only calls
        # it rolled back after a 30-second watch that never ran here (Codex, T42).
        return "unknown", "interrupted while rolling back; the restored image was never watched"
    if current == old_id:
        return "not_applied", f"interrupted before the new image was deployed ({old_id[:12]} still running)"
    pulled = pre.get("pulled_image_id")
    if pulled and current == pulled:
        return "unknown", f"interrupted after deploying {pulled[:12]}; it was never watched"
    return "unknown", f"interrupted mid-update; the container now runs {current[:12]}"


def startup_reconcile(store, svc=None) -> dict:
    """Core restarted: dispatched steps of unfinished executions have an unknown outcome (R0 steps
    changed nothing); their pending steps never ran. Reserved attempts are then settled."""
    settled = {"unknown": 0, "not_applied": 0, "skipped": 0}
    for row in store.unfinished_steps():
        if row["status"] == "dispatched":
            spec = runbooks.STEP_TYPES.get(row["type"])
            reason = "interrupted: Planet Express restarted mid-step"
            if row["type"] == "update.canary":
                effect, reason = _reconcile_canary(svc, row)
            else:
                effect = "not_applied" if spec is not None and spec.risk == "R0" else "unknown"
            store.finish_step(row["execution_id"], row["n"], status="failed", effect=effect,
                              reason=reason)
            if row["type"] == "update.canary":
                if effect == "not_applied":
                    # Nothing was deployed, so nothing needs the image kept back from a prune.
                    store.close_rollback_candidate(row["execution_id"], row["n"])
                else:
                    # The service is in an unverified state; the old image has to survive every
                    # prune until a human settles it, not just for the grace period (Codex, T42).
                    store.hold_rollback_candidate(row["execution_id"], row["n"])
            settled[effect] += 1
        else:
            store.finish_step(row["execution_id"], row["n"], status="skipped",
                              reason="not run: Planet Express restarted")
            settled["skipped"] += 1
    settled.update(store.reconcile_reserved_attempts())
    return settled


# ── rollback planning (slice 5b-1, T40; design §4.4) ────────────────────────────
_INVERSE_SERVICE = {"service.start": "service.stop", "service.stop": "service.start"}
_INVERSE_UNIT = {"start": "stop", "stop": "start"}
# A run is rollback-eligible once it is terminal; `rolled_back`/`rollback_failed` are terminal too,
# so a second attempt is answered by the child-execution checks, not "it is still going" (T40).
TERMINAL_RUN_STATUSES = ("passed", "failed", "aborted", "interrupted", "rolled_back", "rollback_failed")


@dataclass
class RollbackPlan:
    runbook: runbooks.Runbook | None
    undo: list          # labels of the steps that will be reversed (in rollback order)
    not_reversible: list  # applied steps with no inverse (restart, up/down, prune, …)
    unknown: list       # steps whose effect is still unknown after a fresh check: left alone


def _is_conditional(row: dict) -> bool:
    spec = runbooks.STEP_TYPES.get(row["type"])
    return spec is not None and spec.rollback_for(row["params"] or {}) == "conditional"


def rollback_preview(step_rows: list[dict]) -> dict:
    """What a rollback would consider, from stored rows only (no host reads) — cheap enough for the
    dashboard's 2s status poll. `unknown` steps are re-checked against the host at rollback time."""
    undo, unknown, not_reversible = [], [], []
    for row in reversed(step_rows):
        spec = runbooks.STEP_TYPES.get(row["type"])
        if spec is None or spec.risk == "R0" or row["effect"] in (None, "not_applied"):
            continue
        target = unknown if row["effect"] == "unknown" and _is_conditional(row) else (
            undo if _is_conditional(row) else not_reversible)
        target.append(row["n"])
    return {"undo": undo, "unknown": unknown, "not_reversible": not_reversible}


def _now_effect(svc, row: dict) -> str:
    """Re-reconcile an `unknown` conditional step against fresh host state (design §11 A2)."""
    pre = row["pre_state"] or {}
    if row["type"] in _INVERSE_SERVICE:
        container = (row["binding"] or {}).get("container")
        reading = svc._read_health(container) if container else None
        if reading is None or reading.error is not None or "running" not in pre:
            return "unknown"
        running_now = reading.status == "running"
        wanted = row["type"] == "service.start"
        if running_now == wanted and pre["running"] != wanted:
            return "applied"
        return "not_applied" if running_now == pre["running"] else "unknown"
    if row["type"] == "unit.action":
        if "active" not in pre:
            return "unknown"
        try:
            now = unit_active(row["params"]["unit"])
        except actions.TargetError:
            return "unknown"
        wanted = row["params"]["action"] == "start"
        if (now == "active") == wanted and (pre["active"] == "active") != wanted:
            return "applied"
        return "not_applied" if now == pre["active"] else "unknown"
    return "unknown"


def plan_rollback(svc, title: str, step_rows: list[dict]) -> RollbackPlan:
    """Inverse steps, newest first, for every conditional step that changed the host. Built with
    fresh bindings (the authority is the original approval plus the operator's rollback request)."""
    steps, undo, not_reversible, unknown = [], [], [], []
    for row in reversed(step_rows):
        spec = runbooks.STEP_TYPES.get(row["type"])
        if spec is None or spec.risk == "R0" or row["effect"] in (None, "not_applied"):
            continue
        label = f"{row['n']}. {row['type']}"
        if not _is_conditional(row):
            not_reversible.append(label)
            continue
        effect = row["effect"] if row["effect"] != "unknown" else _now_effect(svc, row)
        if effect == "not_applied":
            continue
        if effect != "applied":
            unknown.append(label)
            continue
        params = row["params"] or {}
        if row["type"] in _INVERSE_SERVICE:
            target = svc._resolve(params["stack"], params["service"], for_mutation=True)
            steps.append({"type": _INVERSE_SERVICE[row["type"]], "params": dict(params),
                          "binding": svc._binder.service(target, timeout=actions.DOCKER_TIMEOUT_SECONDS)})
        else:
            inverse = _INVERSE_UNIT[params["action"]]
            steps.append({"type": "unit.action", "params": {"action": inverse, "unit": params["unit"]},
                          "binding": {"unit": params["unit"]}})
        undo.append(label)
    runbook = None
    if steps:
        runbook = runbooks.Runbook.model_validate(
            {"title": f"Roll back: {title}"[:200], "steps": steps, "artifacts": {}}
        )
    return RollbackPlan(runbook, undo, not_reversible, unknown)
