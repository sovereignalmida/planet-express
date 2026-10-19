"""The verifier (contract 10): a mission completes only when Core's own pre-agreed checks pass AND a fresh scan shows no
new finding in scope. `Verifier.run` never trusts, reads or asks the agent: it runs the approved plan's checks, takes
its own scan, compares it with the baseline captured at the checkpoint, and stores a result bound to the grant hash and
checkpoint. Anything that is not a clean pass (a check error or timeout, a scan failure, a missing plan or baseline) is
`failed`, never `passed`.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from ..canonical import CanonicalError, loads_strict
from ..models import Mission, VerificationPlan
from ..resources import BadResource, canon
from ..store import MissionStore, grant_resources
from .baseline import Finding, in_scope, new_findings, normalize
from .checks import CheckEnv, run_check
from .plan import (
    CheckResult,
    FindingRef,
    PlanInvalid,
    ScanResult,
    VerificationResult,
    binding_problems,
    plan_hash,
    validate_plan,
)

log = logging.getLogger("planetexpress.missions.verify")
ACTOR = "core"
MAX_REASON = 300


class VerifierDisabled(RuntimeError):
    """Missions are disabled: nothing is verified."""


class VerifyRefused(RuntimeError):
    """The mission is not in a state where it can be verified."""


class Verifier:
    def __init__(self, *, missions: MissionStore, checkpoint: Any, scan: Callable[[], Mapping[str, Any]],
                 env: CheckEnv, stacks_root: str | None = None, scan_timeout_s: float = 120.0,
                 enabled: bool | Callable[[], bool] = False, host: str = "local"):
        self.ms = missions
        self.checkpoint = checkpoint              # `CheckpointService`: `.baseline(checkpoint_id)`
        self.scan = scan                          # `casa_leela.run_full` in production
        self.env = env
        self.stacks_root = stacks_root
        self.scan_timeout_s = scan_timeout_s
        self.host = host
        self._enabled = enabled if callable(enabled) else (lambda: bool(enabled))

    @property
    def enabled(self) -> bool:
        return bool(self._enabled())

    # ── the run ─────────────────────────────────────────────────────────────
    def run(self, mission: Mission | str) -> VerificationResult:
        if not self.enabled:
            raise VerifierDisabled("missions are disabled")
        mission_id = mission if isinstance(mission, str) else mission.id
        mission = self.ms.get_mission(mission_id)               # always the stored row, never a caller's copy
        if mission.status != "verifying":
            raise VerifyRefused(f"{mission_id} is {mission.status}, not verifying")
        checks: list[CheckResult] = []
        scan: ScanResult | None = None
        reason = ""
        plan_sha: str | None = None
        try:
            plan, reason = self._plan(mission)
            if plan is not None:
                plan_sha = plan_hash(plan)
                checks = self._run_checks(mission, plan)
                scan, scan_reason = self._compare(mission)
                failed = [c.id for c in checks if not c.ok]
                if failed:
                    reason = f"failed checks: {', '.join(failed)[:200]}"
                elif not scan.ok or scan.new_in_scope_count:
                    reason = scan_reason
        except Exception as exc:
            log.exception("verification of %s raised", mission_id)
            reason = f"the verifier failed ({type(exc).__name__})"
        passed = (not reason and plan_sha is not None and bool(checks) and all(c.ok for c in checks)
                  and scan is not None and scan.ok and scan.new_in_scope_count == 0)
        result = VerificationResult(
            status="passed" if passed else "failed", grant_hash=mission.grant_hash, checkpoint_id=mission.checkpoint_id,
            plan_sha256=plan_sha, at=int(self.ms._now()), reason=reason[:MAX_REASON], checks=checks, scan=scan)
        self.ms.set_verification_result(mission_id, result, ACTOR)
        self.ms.append_event(mission_id, "note", ACTOR, {
            "event": "verify.result", "status": result.status, "reason": result.reason,
            "checks": [{"id": c.id, "ok": c.ok} for c in checks],
            "new_in_scope": scan.new_in_scope_count if scan else None})
        return result

    # ── plan ────────────────────────────────────────────────────────────────
    def _plan(self, mission: Mission) -> tuple[VerificationPlan | None, str]:
        if not mission.verification_plan_json:
            return None, "the mission has no verification plan"
        try:
            plan = VerificationPlan.model_validate(loads_strict(mission.verification_plan_json))
            validate_plan(plan)
        except (CanonicalError, PlanInvalid, ValueError):
            return None, "the verification plan is invalid"
        problems = binding_problems(mission.grant(), plan)
        if problems:
            return None, problems[0][:MAX_REASON]
        return plan, ""

    # ── checks ──────────────────────────────────────────────────────────────
    def _run_checks(self, mission: Mission, plan: VerificationPlan) -> list[CheckResult]:
        env = replace(self.env, mission_id=mission.id)
        results = []
        for check in plan.checks:
            result = run_check(check, env)
            results.append(result)
            self.ms.append_event(mission.id, "note", ACTOR, {
                "event": "verify.check", "id": result.id, "check": result.check, "ok": result.ok,
                "attempts": result.attempts, "detail": result.detail})
        return results

    # ── scan ────────────────────────────────────────────────────────────────
    def _fresh_scan(self) -> list[Finding]:
        box: list[Any] = []

        def target() -> None:
            try:
                box.append(normalize(self.scan(), host=self.host))
            except Exception as exc:                            # noqa: BLE001
                box.append(exc)

        thread = threading.Thread(target=target, daemon=True, name="verify-scan")
        thread.start()
        thread.join(self.scan_timeout_s)
        if not box:
            raise TimeoutError("scan timed out")
        if isinstance(box[0], Exception):
            raise box[0]
        return box[0]

    def _compare(self, mission: Mission) -> tuple[ScanResult, str]:
        if not mission.checkpoint_id:
            return ScanResult(ok=False, error="no checkpoint, so no baseline"), "there is no checkpoint baseline"
        try:
            baseline = self.checkpoint.baseline(mission.checkpoint_id)
        except Exception as exc:                                # noqa: BLE001
            return ScanResult(ok=False, error=f"baseline unreadable ({type(exc).__name__})"), "the baseline is unreadable"
        if baseline is None:
            return ScanResult(ok=False, error="the checkpoint has no scan baseline"), "the checkpoint has no scan baseline"
        try:
            scope = [canon(r) for r in grant_resources(mission.grant())]
        except BadResource:
            return ScanResult(ok=False, error="grant scope is malformed"), "the grant scope is malformed"
        try:
            fresh = self._fresh_scan()
        except Exception as exc:                                # noqa: BLE001
            return (ScanResult(ok=False, error=f"scan failed ({type(exc).__name__})", baseline_findings=len(baseline)),
                    "the fresh scan failed")
        new = new_findings(baseline, fresh)
        mine = [f for f in new if in_scope(f, scope, stacks_root=self.stacks_root)]
        scan = ScanResult(
            ok=True, baseline_findings=len(baseline), fresh_findings=len(fresh), new_in_scope_count=len(mine),
            new_in_scope=[FindingRef(kind=f.kind, resource_id=f.resource_id, severity=f.severity) for f in mine[:20]],
            new_out_of_scope=len(new) - len(mine))
        return scan, (f"{len(mine)} new finding(s) in scope" if mine else "")


def verifier_from_config(cfg: Any, *, missions: MissionStore, checkpoint: Any, provider: Any = None, broker: Any = None,
                         scan: Callable[[], Mapping[str, Any]] | None = None, stacks_root: str | None = None,
                         enabled: bool | Callable[[], bool] | None = None) -> Verifier:
    """A Verifier over the `missions` config block. `scan` defaults to Leela's full scan; `enabled` to `cfg.enabled`."""
    from .checks import env_from_config
    if scan is None:
        import casa_leela
        scan = casa_leela.run_full
    return Verifier(missions=missions, checkpoint=checkpoint, scan=scan,
                    env=env_from_config(cfg.verify, provider=provider, broker=broker), stacks_root=stacks_root,
                    scan_timeout_s=float(cfg.verify.scan_timeout_s),
                    enabled=(lambda: cfg.enabled) if enabled is None else enabled)
