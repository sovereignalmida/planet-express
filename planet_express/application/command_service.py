"""
planet_express/application/command_service.py — the single command API every front end
calls (landing 1b: Telegram; landing 1c: the dashboard over RPC).

    propose ─► resolve target ─► policy ─► store.propose (dedup) ─► approval card
    decide(deny)    ─► consume(denied) ─► card "denied by X"             (never locks)
    decide(approve) ─► pre-read ─► try_begin_mutation ─► consume(approved)
                    ─► execution row ─► card "approved by X" ─► worker thread:
                          re-resolve target ─► restart (run_argv) ─► verify ─► passed/failed
                          end_mutation in finally

Rules (design doc, "Approval order" and "Lock rule"):
  - The pre-read answers "already decided" / "expired" without touching the lock; the
    conditional UPDATE in store.consume stays the real guard.
  - Approve takes the host-mutation lock BEFORE consuming, so a busy host leaves the
    approval pending and approvable later. Deny never takes the lock.
  - Until the worker thread has started, releasing the lock is decide()'s job.
  - Verification comes from container state (actions.verify_after_restart), never the
    restart command's exit code alone.
"""

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import casa_bender as bender
import config
from notifier import Decision, Notifier
from planet_express.core.incidents import scan_id
from planet_express.core.redact import redact
from planet_express.core.store import (
    DEFAULT_TTL_SECONDS,
    IncidentSourceError,
    PendingPlanConflict,
    Store,
)
from planet_express.execution import actions, engine, policy, runbook as runbooks
from planet_express.execution.binding import Binder
from state_models import MonitorSnapshot
from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.commands")

_s = TelegramClient.s
_TAG_RE = re.compile(r"<[^>]+>")
_CALLBACK_TEXT_LIMIT = 190  # Telegram answerCallbackQuery allows 200 chars, no markup


@dataclass(frozen=True)
class ProposeResult:
    ok: bool
    approval_id: str | None
    created: bool
    reason: str


@dataclass(frozen=True)
class DecideResult:
    outcome: str  # started | denied | refused | busy | already_decided | expired | unknown
    message: str
    execution_id: str | None = None


@dataclass(frozen=True)
class RequestResult:
    outcome: str  # started | busy | refused | timeout
    message: str
    approval_id: str | None
    execution_id: str | None
    capabilities: dict[str, bool]


STEP_OUTPUT_MAX_BYTES = 256 * 1024


def _bounded_run_argv(argv: list[str], timeout) -> tuple[int, str, str]:
    rc, out, err, _truncated = bender.run_argv_bounded(argv, timeout, STEP_OUTPUT_MAX_BYTES)
    return rc, out.strip(), err.strip()


@dataclass(frozen=True)
class ControlResult:
    """Abort / rollback outcome (slice 5b-1, T40)."""
    outcome: str  # requested | already | not_running | unknown | started | nothing_to_undo | busy | refused
    message: str
    execution_id: str | None = None
    report: dict | None = None


def _spawn_daemon(fn: Callable, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True, name="typed-action").start()


class CommandService:
    def __init__(
        self,
        store: Store,
        notifier: Notifier,
        state,
        *,
        run_argv: Callable | None = None,
        resolve_target: Callable | None = None,
        verify: Callable | None = None,
        read_health: Callable | None = None,
        restart_count: Callable | None = None,
        spawn: Callable | None = None,
        background: Callable[[Callable[[], None]], object] | None = None,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        monitor_path: Path | None = None,
        compose_identities: Callable | None = None,
        resolve_stack_target: Callable | None = None,
        verify_stack_up: Callable | None = None,
        verify_stack_down: Callable | None = None,
        binder: Binder | None = None,
        on_failure: Callable[[dict], None] | None = None,
    ):
        self._store = store
        self._notifier = notifier
        self._state = state  # casa_farnsworth.PipelineState (duck-typed to avoid an import cycle)
        # Bounded by default: every step's argv goes through the memory-capped runner (design §3).
        self._run_argv = run_argv or _bounded_run_argv
        self._resolve = resolve_target or actions.resolve_target
        self._verify = verify or actions.verify_after_restart
        self._read_health = read_health or actions.read_health
        self._restart_count = restart_count or actions.restart_count
        self._spawn = spawn or _spawn_daemon
        # Telegram side effects an RPC reply must not wait on: each can take Telegram's 35s HTTP
        # timeout, longer than the dashboard's 5s RPC deadline, and approval.decide runs on the one
        # reserved worker (Codex review, T14). One serial worker keeps a card's edits in order
        # ("approved…" before "verified good").
        if background is None:
            background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-updates").submit
        self._background = background
        self._clock = clock
        self._ttl = ttl_seconds
        self._monitor_path = Path(monitor_path or config.STATE_MONITOR)
        self._compose_identities = compose_identities or actions.container_compose_identities
        self._resolve_stack = resolve_stack_target or actions.resolve_stack_target
        self._verify_stack_up = verify_stack_up or actions.verify_stack_up
        self._verify_stack_down = verify_stack_down or actions.verify_stack_down
        # Proposal-time bindings and pre-step drift checks (slice 5b, design §4.2).
        self._binder = binder or Binder()
        self._on_failure = on_failure
        # Store.propose deduplicates rows atomically, but sending the corresponding card
        # is an external side effect. Keep row creation, card delivery and message-id
        # recording in one in-process critical section so two front-end requests cannot
        # both observe the same NULL message_id and publish duplicate actionable cards.
        self._proposal_card_lock = threading.Lock()

    # ── propose ─────────────────────────────────────────────────────────────
    def propose(
        self, action: str, stack: str, service: str | None = None, *, requested_via: str,
        requested_by: str | None,
        timeout=actions.DOCKER_TIMEOUT_SECONDS,
        incident_id: str | None = None, incident_scan_id: str | None = None,
        expected_container: str | None = None,
        deadline: float | None = None,
        defer_card: bool = False,
    ) -> ProposeResult:
        deadline = time.monotonic() + timeout if deadline is None else deadline
        target = None
        target_error = None
        plan = None
        if action in actions.REGISTRY:
            try:
                target = self._resolve_for_action(
                    action, stack, service, timeout=self._remaining(deadline)
                )
                if expected_container is not None and target.container != expected_container:
                    target_error = "resolved container does not match the incident resource"
                elif action in engine.LEGACY_STEP_TYPES:
                    plan = self._build_runbook(action, target, timeout=self._remaining(deadline))
            except actions.TargetTimeout:
                return ProposeResult(False, None, False, "host slow, retry")
            except actions.TargetError as e:
                target_error = str(e)
        decision = policy.decide(action, target_error)
        if decision.allowed and decision.needs_approval:
            # The stored document gets the runbook policy too; for one-step plans it agrees with
            # policy.decide, and it is the gate planner runbooks will go through (slice 5b).
            runbook_decision = policy.decide_runbook(plan, requested_via) if plan else None
            if runbook_decision is None or not runbook_decision.allowed:
                reason = runbook_decision.reason if runbook_decision else "no runbook for this action"
                decision = policy.PolicyDecision(False, False, decision.risk, reason)
        if not decision.allowed:
            try:
                self._store.record_event(
                    "proposal.refused", action=action, stack=stack, service=service,
                    reason=decision.reason, requested_via=requested_via,
                    requested_by=requested_by, timeout=self._remaining(deadline),
                )
            except (actions.TargetTimeout, sqlite3.OperationalError):
                return ProposeResult(False, None, False, "host slow, retry")
            return ProposeResult(False, None, False, decision.reason)
        if not decision.needs_approval:
            # Slice 1 registers no automatic mutations; an R0 action is a read, not a proposal.
            return ProposeResult(False, None, False, f"{action} runs automatically and is not proposed")

        if requested_via not in policy.OPERATOR_ORIGINS:
            now = self._clock()
            try:
                attempts = self._store.recent_attempts(
                    action, target.key, now - policy.limit_lookback_seconds(),
                    timeout=self._remaining(deadline),
                )
            except (actions.TargetTimeout, sqlite3.OperationalError):
                return ProposeResult(False, None, False, "host slow, retry")
            reason = policy.limit_refusal(attempts, now)
            if reason is not None:
                try:
                    self._store.record_event(
                        "proposal.refused", action=action, stack=stack, service=service,
                        reason=reason, requested_via=requested_via, requested_by=requested_by,
                        timeout=self._remaining(deadline),
                    )
                except (actions.TargetTimeout, sqlite3.OperationalError):
                    return ProposeResult(False, None, False, "host slow, retry")
                return ProposeResult(False, None, False, reason)

        try:
            lock_timeout = self._remaining(deadline)
        except actions.TargetTimeout:
            return ProposeResult(False, None, False, "host slow, retry")
        if not self._proposal_card_lock.acquire(timeout=lock_timeout):
            return ProposeResult(False, None, False, "host slow, retry")
        card_handed_off = False
        try:
            kwargs = {}
            if incident_id is not None:
                kwargs = {"incident_id": incident_id, "incident_scan_id": incident_scan_id}
            try:
                row, created = self._store.propose_runbook(
                    action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                    requested_via=requested_via, requested_by=requested_by, ttl_seconds=self._ttl,
                    timeout=self._remaining(deadline),
                    plan_json=runbooks.canonical_json(plan), plan_sha256=runbooks.plan_sha256(plan),
                    origin=requested_via,
                    **kwargs,
                )
            except PendingPlanConflict as conflict:
                # The pending card was proposed against an earlier binding (compose edited, container
                # recreated) and would be refused for drift if approved. The newer request replaces
                # it instead of pointing the operator at a dead card (T39 review, T40).
                superseded = self._store.supersede_pending(
                    conflict.approval_id, reason="target changed; replaced by a newer request",
                )
                if superseded is not None and superseded.get("message_id") is not None:
                    text = self._policy_refusal_text(
                        superseded, "superseded: its target changed, and a newer request replaces it"
                    )
                    self._in_background(lambda: self._update_card_quietly(superseded["message_id"], text))
                try:
                    row, created = self._store.propose_runbook(
                        action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                        requested_via=requested_via, requested_by=requested_by, ttl_seconds=self._ttl,
                        timeout=self._remaining(deadline),
                        plan_json=runbooks.canonical_json(plan), plan_sha256=runbooks.plan_sha256(plan),
                        origin=requested_via, **kwargs,
                    )
                except PendingPlanConflict as again:
                    return ProposeResult(True, again.approval_id, False, "already awaiting approval")
                except IncidentSourceError as exc:
                    return ProposeResult(False, None, False, str(exc))
                except (actions.TargetTimeout, sqlite3.OperationalError):
                    return ProposeResult(False, None, False, "host slow, retry")
            except IncidentSourceError as exc:
                return ProposeResult(False, None, False, str(exc))
            except (actions.TargetTimeout, sqlite3.OperationalError):
                return ProposeResult(False, None, False, "host slow, retry")
            if created or row["message_id"] is None:
                # A pending row without a card (its send failed last time) gets one now. Otherwise a
                # transient Telegram error would leave an approval nobody can see or act on, and
                # every retry would dedup against it until it expired (Codex review, landing 1b).
                def deliver_card():
                    try:
                        message_id = self._notifier.request_approval(
                            self._card_text(row, target, requested_by), row["id"], "action"
                        )
                        self._store.set_message_id(row["id"], message_id)
                    except Exception:  # Telegram errors can carry the bot token
                        log.warning(f"Failed to send the approval card for {row['id']}")
                        self._store.record_event("proposal.card_failed", approval_id=row["id"])
                        if not defer_card:
                            raise
                    finally:
                        if defer_card:
                            self._proposal_card_lock.release()

                if defer_card:
                    card_handed_off = True
                    try:
                        self._background(deliver_card)
                    except Exception:  # noqa: BLE001 -- scheduling failure is a typed refusal
                        card_handed_off = False
                        self._store.record_event("proposal.card_failed", approval_id=row["id"])
                        return ProposeResult(False, row["id"], created, "could not schedule approval card")
                else:
                    try:
                        deliver_card()
                    except Exception:  # noqa: BLE001 -- already recorded without secret-bearing text
                        return ProposeResult(
                            False, row["id"], created,
                            "could not send the approval card; send the request again to retry",
                        )
                return ProposeResult(True, row["id"], created, "awaiting approval")
            return ProposeResult(True, row["id"], False, "already awaiting approval")
        finally:
            if not card_handed_off:
                self._proposal_card_lock.release()

    # ── planner runbooks (slice 5b-2) ───────────────────────────────────────
    @property
    def binder(self) -> Binder:
        """The same binder the service uses, so a caller's runbook carries bindings the engine
        will recognise (slice 5b-2)."""
        return self._binder

    def propose_plan(
        self, runbook: runbooks.Runbook, *, requested_by: str | None = None,
        finding_ids: list | None = None, origin: str = "planner",
    ) -> ProposeResult:
        """Propose a multi-step plan the planner (or Amy) chose from the catalogue. The document is
        already validated with server-built bindings; policy, T24 limits, dedup, the card and the
        approval path are the same ones every typed action uses."""
        decision = policy.decide_runbook(runbook, origin)
        plan_sha = runbooks.plan_sha256(runbook)
        if decision.allowed and not decision.needs_approval:
            # Every step is R0: there is nothing to approve, and a diagnosis is not a card (D37).
            reason = "diagnostic only: no step to approve"
            self._store.record_event("proposal.refused", action="runbook", reason=reason,
                                     requested_via=origin, requested_by=requested_by,
                                     title=runbook.title)
            return ProposeResult(False, None, False, reason)
        if not decision.allowed:
            self._store.record_event("proposal.refused", action="runbook", reason=decision.reason,
                                     requested_via=origin, requested_by=requested_by,
                                     title=runbook.title)
            return ProposeResult(False, None, False, decision.reason)
        if origin not in policy.OPERATOR_ORIGINS:
            now = self._clock()
            since = now - policy.limit_lookback_seconds()
            for pair, requested in Counter(decision.pairs).items():
                step_type, target_key = pair
                reason = policy.limit_refusal(
                    self._store.recent_pair_attempts(step_type, target_key, since)
                    + self._legacy_attempts(step_type, target_key, since), now,
                    requested=requested,
                )
                if reason is not None:
                    reason = f"{step_type} {target_key}: {reason}"
                    self._store.record_event(
                        "proposal.refused", action="runbook", reason=reason, requested_via=origin,
                        requested_by=requested_by, title=runbook.title,
                    )
                    return ProposeResult(False, None, False, reason)
        with self._proposal_card_lock:
            try:
                row, created = self._store.propose_runbook(
                    action="runbook", target_key=f"plan:{plan_sha[:16]}",
                    target={"title": runbook.title, "finding_ids": list(finding_ids or [])},
                    risk=decision.risk, requested_via=origin, requested_by=requested_by,
                    ttl_seconds=self._ttl, plan_json=runbooks.canonical_json(runbook),
                    plan_sha256=plan_sha, origin=origin,
                )
            except PendingPlanConflict as conflict:
                # target_key carries this plan's own hash, so a conflict means the identical plan is
                # already pending under a truncated-hash collision — point at that card, never
                # supersede an operator's pending approval with a different plan.
                return ProposeResult(True, conflict.approval_id, False, "already awaiting approval")
            if created or row["message_id"] is None:
                try:
                    message_id = self._notifier.request_approval(
                        self._plan_card_text(row, runbook, requested_by), row["id"], "action")
                    self._store.set_message_id(row["id"], message_id)
                except Exception:  # noqa: BLE001 -- Telegram errors can carry the bot token
                    log.warning(f"Failed to send the plan card for {row['id']}")
                    self._store.record_event("proposal.card_failed", approval_id=row["id"])
                    return ProposeResult(False, row["id"], created,
                                         "could not send the approval card; propose it again")
                return ProposeResult(True, row["id"], created, "awaiting approval")
            return ProposeResult(True, row["id"], False, "already awaiting approval")

    def _legacy_attempts(self, step_type: str, target_key: str, since: float) -> list:
        """Pre-5b executions counted for the same target, so the upgrade does not reset a cooldown.

        Only executions the attempts ledger does not already hold: a post-v5 restart is recorded in
        both places and would otherwise count twice (Codex, T41)."""
        if step_type != "service.restart":
            return []
        return self._store.recent_attempts(actions.RESTART_SERVICE, target_key, since,
                                           without_attempt_rows=True)

    def _plan_card_text(self, row: dict, runbook: runbooks.Runbook, requested_by: str | None) -> str:
        minutes = int(self._ttl // 60)
        lines = [f"🛠 <b>{_s(runbook.title)}</b> <code>{_s(row['id'])}</code>",
                 f"Risk: {_s(row['risk'])}, {len(runbook.steps)} step(s):"]
        for n, step in enumerate(runbook.steps, start=1):
            row_like = {"type": step.type, "params": step.params, "binding": step.binding}
            lines.append(f"  {n}. {_s(self._step_label(row_like, runbook.title))}")
        reversible = [str(n) for n, step in enumerate(runbook.steps, start=1)
                      if runbooks.rollback_kind(step) == "conditional"]
        lines.append("Reversible steps: " + (", ".join(reversible) if reversible else "none"))
        lines.append(f"Proposed by {_s(requested_by or 'Farnsworth')}. Expires in {minutes} min.")
        return "\n".join(lines)

    def _build_runbook(self, action: str, target, *, timeout: float) -> runbooks.Runbook:
        """Every typed action is a one-step runbook (slice 5b-1): the approved document, with its
        proposal-time binding, is what the engine runs. Raises TargetError/TargetTimeout."""
        step_type = engine.LEGACY_STEP_TYPES[action]
        if isinstance(target, actions.AllStacksTarget):
            params, binding = {}, self._binder.stack_set(list(target.stacks), timeout=timeout)
        elif isinstance(target, actions.StackTarget):
            params, binding = {"stack": target.stack}, self._binder.stack(target.stack, timeout=timeout)
        else:
            params = {"stack": target.stack, "service": target.service}
            binding = self._binder.service(target, timeout=timeout)
        return runbooks.Runbook.model_validate({
            "title": actions.action_summary(action, target.as_dict()),
            "steps": [{"type": step_type, "params": params, "binding": binding}],
            "artifacts": {},
        })

    def _resolve_for_action(
        self, action: str, stack: str, service: str | None, *, timeout: float,
        approved_target: dict | None = None,
    ):
        if action not in actions.STACK_ACTIONS:
            return self._resolve(stack, service, for_mutation=True, timeout=timeout)
        return self._resolve_stack(action, stack, approved_target=approved_target)

    # ── incidents ───────────────────────────────────────────────────────────
    @staticmethod
    def _remaining(deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise actions.TargetTimeout("host slow, retry")
        return left

    def _current_incident_scan(self, *, deadline: float | None = None) -> str | None:
        try:
            monitor = MonitorSnapshot.model_validate_json(self._monitor_path.read_text())
        except Exception:  # noqa: BLE001 -- malformed/missing state fails closed
            return None
        if monitor.mode != "full":
            return None
        current = scan_id(monitor.model_dump(mode="json"))
        receipt = self._store.latest_incident_reconciliation(
            timeout=self._remaining(deadline) if deadline is not None else 5
        )
        return current if receipt is not None and receipt["scan_id"] == current else None

    def _source_current(
        self, incident: dict, expected_scan_id: str | None = None, *, deadline: float | None = None
    ) -> tuple[bool, str | None]:
        current = self._current_incident_scan(deadline=deadline)
        valid = (
            current is not None
            and (expected_scan_id is None or current == expected_scan_id)
            and incident["status"] == "open"
            and incident["condition"] == "failing"
            and incident["last_observed_scan_id"] == current
        )
        return valid, current

    @staticmethod
    def _proposal_summary(row: dict) -> dict:
        return {key: row[key] for key in (
            "id", "action", "risk", "status", "requested_via", "requested_by",
            "created_at", "expires_at", "decided_by", "decided_at",
        )}

    def _decorate_incidents(self, rows: list[dict], *, deadline: float) -> list[dict]:
        try:
            current_scan = self._current_incident_scan(deadline=deadline)
        except sqlite3.OperationalError:
            raise actions.TargetTimeout("host slow, retry") from None
        try:
            proposal_rows = self._store.list_incident_proposals_batch(
                [row["id"] for row in rows], timeout=self._remaining(deadline)
            )
        except sqlite3.OperationalError:
            raise actions.TargetTimeout("host slow, retry") from None
        eligible = [row["resource"] for row in rows if (
            row["status"] == "open" and row["condition"] == "failing"
            and row["last_observed_scan_id"] == current_scan and row["kind"] == "container_health"
        )]
        identities = {}
        lookup_failed = False
        if eligible:
            try:
                identities = self._compose_identities(eligible, timeout=self._remaining(deadline))
            except (actions.TargetError, actions.TargetTimeout):
                lookup_failed = True
        result = []
        for row in rows:
            item = dict(row)
            proposals = [self._proposal_summary(proposal)
                         for proposal in proposal_rows[row["id"]]]
            item["source_current"] = bool(
                current_scan is not None and row["last_observed_scan_id"] == current_scan
            )
            if row["status"] == "resolved":
                hint = {"agent": "Leela", "state": "resolved", "message": "This incident is resolved."}
            elif not item["source_current"]:
                hint = {"agent": "Leela", "state": "fresh_scan_required",
                        "message": "Run a full scan before proposing remediation."}
            elif row["condition"] == "unknown":
                hint = {"agent": "Amy", "state": "investigation_required",
                        "message": "The observation is uncertain and needs investigation."}
            elif row["kind"] != "container_health":
                hint = {"agent": "Amy", "state": "no_typed_remediation",
                        "message": "No typed remediation is registered for this incident."}
            elif any(proposal["status"] == "pending" for proposal in proposals):
                hint = {"agent": "Farnsworth", "state": "awaiting_approval",
                        "message": "A typed restart proposal is awaiting approval."}
            elif lookup_failed or row["resource"] not in identities:
                hint = {"agent": "Farnsworth", "state": "target_unavailable",
                        "message": "A current Compose target could not be established."}
            elif not policy.decide(actions.RESTART_SERVICE).allowed:
                hint = {"agent": "Farnsworth", "state": "no_typed_remediation",
                        "message": "Current policy does not allow the registered remediation."}
            else:
                stack, service = identities[row["resource"]]
                item["proposed_target"] = {"stack": stack, "service": service}
                hint = {"agent": "Farnsworth", "state": "proposal_available",
                        "message": "A typed restart is available and requires approval."}
            item["hint"] = hint
            item["proposals"] = proposals
            result.append(item)
        return result

    def list_incidents(self, *, status: str, limit: int, timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        try:
            rows = self._store.list_incidents(
                None if status == "all" else status, limit, timeout=self._remaining(deadline)
            )
        except sqlite3.OperationalError:
            raise actions.TargetTimeout("host slow, retry") from None
        return self._decorate_incidents(rows, deadline=deadline)

    def get_incident_context(self, incident_id: str, *, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        try:
            incident = self._store.get_incident(incident_id, timeout=self._remaining(deadline))
        except sqlite3.OperationalError:
            raise actions.TargetTimeout("host slow, retry") from None
        if incident is None:
            return None
        item = self._decorate_incidents([incident], deadline=deadline)[0]
        try:
            item["events"] = self._store.list_incident_events(
                incident_id, timeout=self._remaining(deadline)
            )
        except sqlite3.OperationalError:
            raise actions.TargetTimeout("host slow, retry") from None
        return item

    def propose_incident(
        self, incident_id: str, *, operator: str, timeout: float | None = None,
        deadline: float | None = None,
    ) -> ProposeResult:
        if deadline is None:
            budget = actions.RPC_DOCKER_TIMEOUT_SECONDS if timeout is None else timeout
            deadline = time.monotonic() + budget
        owner = f"incident:{incident_id}"
        if not self._state.try_begin_mutation(owner):
            return ProposeResult(False, None, False, f"Busy right now ({self._state.busy_reason}).")
        try:
            try:
                incident = self._store.get_incident(
                    incident_id, timeout=self._remaining(deadline)
                )
            except sqlite3.OperationalError:
                return ProposeResult(False, None, False, "host slow, retry")
            if incident is None:
                return ProposeResult(False, None, False, "incident not found")
            try:
                current, current_scan = self._source_current(incident, deadline=deadline)
            except (actions.TargetTimeout, sqlite3.OperationalError):
                return ProposeResult(False, None, False, "host slow, retry")
            if not current:
                return ProposeResult(False, None, False, "incident source is no longer current")
            if incident["kind"] != "container_health":
                return ProposeResult(False, None, False, "no typed remediation is registered")
            left = deadline - time.monotonic()
            if left <= 0:
                return ProposeResult(False, None, False, "host slow, retry")
            try:
                identities = self._compose_identities([incident["resource"]], timeout=left)
            except actions.TargetTimeout:
                return ProposeResult(False, None, False, "host slow, retry")
            except actions.TargetError as exc:
                return ProposeResult(False, None, False, str(exc))
            identity = identities.get(incident["resource"])
            if identity is None:
                return ProposeResult(False, None, False, "current Compose target is unavailable")
            left = deadline - time.monotonic()
            if left <= 0:
                return ProposeResult(False, None, False, "host slow, retry")
            try:
                return self.propose(
                    actions.RESTART_SERVICE, *identity, requested_via="incident", requested_by=operator,
                    timeout=left, incident_id=incident_id, incident_scan_id=current_scan,
                    expected_container=incident["resource"], deadline=deadline, defer_card=True,
                )
            except (actions.TargetTimeout, sqlite3.OperationalError):
                return ProposeResult(False, None, False, "host slow, retry")
        finally:
            self._state.end_mutation(owner)

    # ── decide ──────────────────────────────────────────────────────────────
    def decide(
        self, approval_id: str, *, approve: bool, decided_by: str, decision: Decision | None = None
    ) -> DecideResult:
        arrived = self._clock()
        row = self._store.get_approval(approval_id, as_of=arrived)
        if row is None:
            return self._reply(decision, DecideResult("unknown", "Unknown or already removed request."))
        if row["status"] != "pending":
            return self._reply(decision, self._not_pending(row))

        if not approve:
            if not self._store.consume(approval_id, decision="denied", decided_by=decided_by, arrived_at=arrived):
                return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))
            text = self._decision_text(row, "denied", decided_by)
            self._safe_finalize_card(row, decision, ack="Denied.", text=text)
            return DecideResult("denied", text)

        # The approved document must be intact before anything else: after the v5 cutover every
        # approval carries a plan, so a missing or tampered one is refused for good (slice 5b-1).
        if self._verified_plan(row) is None:
            if self._store.consume(approval_id, decision="denied", decided_by="policy", arrived_at=arrived):
                self._store.record_event(
                    "approval.refused_no_plan", approval_id=approval_id, attempted_by=decided_by,
                )
                text = self._policy_refusal_text(
                    row, "refused: its approved plan is missing or does not match its fingerprint"
                )
                self._safe_finalize_card(row, decision, ack="Refused: invalid plan.", text=text)
                return DecideResult("refused", text)
            return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))

        provenance = self._store.get_incident_proposal(approval_id)

        # Generic approvals retain the original early policy check. Incident approvals first take
        # the scan/mutation lock and validate their evidence below, so stale evidence always gets
        # the incident-specific durable refusal required by T33.
        if provenance is None:
            current = self._current_policy(row)
            if not current.allowed:
                reason = f"refused by current policy: {current.reason}"
                if self._store.consume(
                    approval_id, decision="denied", decided_by="policy", arrived_at=arrived
                ):
                    self._store.record_event(
                        "approval.refused_by_policy", approval_id=approval_id,
                        reason=current.reason, attempted_by=decided_by,
                    )
                    text = self._policy_refusal_text(row, reason)
                    self._safe_finalize_card(row, decision, ack="Refused by policy.", text=text)
                    return DecideResult("refused", text)
                return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))

        owner = f"act:{approval_id}"
        if not self._state.try_begin_mutation(owner):
            minutes = max(0, int((row["expires_at"] - arrived) // 60))
            message = (f"⏳ Busy right now ({self._state.busy_reason}). "
                       f"This request stays valid for {minutes} more min.")
            return self._reply(decision, DecideResult("busy", message))

        with self._execution_handoff(owner) as start:
            if provenance is not None:
                incident = self._store.get_incident(provenance["incident_id"])
                current_source = incident is not None and self._source_current(
                    incident, provenance["scan_id"]
                )[0]
                if not current_source:
                    reason = "incident source is no longer current"
                    if self._store.refuse_stale_incident_approval(
                        approval_id, attempted_by=decided_by, arrived_at=arrived, reason=reason,
                    ):
                        text = self._policy_refusal_text(row, f"refused: {reason}")
                        self._safe_finalize_card(row, decision, ack="Refused: stale incident.", text=text)
                        return DecideResult("refused", text)
                    return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))
                current = self._current_policy(row)
                if not current.allowed:
                    reason = f"refused by current policy: {current.reason}"
                    if self._store.consume(
                        approval_id, decision="denied", decided_by="policy", arrived_at=arrived
                    ):
                        self._store.record_event(
                            "approval.refused_by_policy", approval_id=approval_id,
                            reason=current.reason, attempted_by=decided_by,
                        )
                        text = self._policy_refusal_text(row, reason)
                        self._safe_finalize_card(row, decision, ack="Refused by policy.", text=text)
                        return DecideResult("refused", text)
                    return self._reply(
                        decision, self._not_pending(self._store.get_approval(approval_id))
                    )
            execution = self._store.approve_and_create_execution(
                approval_id, decided_by=decided_by, arrived_at=arrived
            )
            if execution is None:
                return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))
            execution_id = execution["id"]
            text = self._decision_text(row, "approved", decided_by)
            ack = "Approved. Restarting…" if row["action"] == actions.RESTART_SERVICE else "Approved. Starting…"
            self._safe_finalize_card(row, decision, ack=ack, text=text)
            start(row, execution_id, decided_by)
            return DecideResult("started", text, execution_id)

    @contextmanager
    def _execution_handoff(self, owner):
        handed_off = False

        def start(row, execution_id, decided_by):
            nonlocal handed_off
            try:
                self._spawn(self._run_execution, row, execution_id, owner, decided_by)
            except Exception:
                self._store.set_execution_status(execution_id, "failed", reason="failed to start")
                if row["action"] == actions.RESTART_SERVICE:
                    text = (f"🔴 <b>Restart failed</b> for <code>{_s(row['target_key'])}</code>: "
                            "failed to start")
                else:
                    direction = "Up" if row["action"] in actions.UP_ACTIONS else (
                        "Down" if row["action"] in actions.DOWN_ACTIONS else "Run")
                    text = (f"🔴 <b>{direction} failed</b> for <code>{_s(self._row_label(row))}</code>: "
                            "failed to start")
                self._in_background(lambda: self._notify_quietly(
                    text))
                raise
            handed_off = True

        try:
            yield start
        finally:
            if not handed_off:
                self._state.end_mutation(owner)

    def request_action(
        self, action: str, stack: str, service: str | None = None, *, operator: str,
        origin: str = "dashboard-direct", timeout=actions.DOCKER_TIMEOUT_SECONDS,
    ) -> RequestResult:
        arrived = self._clock()
        spec = actions.REGISTRY.get(action)
        capabilities = spec.capabilities() if spec else {}
        target = None
        target_error = None
        plan = None
        if spec is not None:
            try:
                target = self._resolve_for_action(action, stack, service, timeout=timeout)
                if action in engine.LEGACY_STEP_TYPES:
                    left = timeout - (self._clock() - arrived)
                    if left <= 0:
                        raise actions.TargetTimeout("host slow, retry")
                    plan = self._build_runbook(action, target, timeout=left)
            except actions.TargetTimeout:
                return RequestResult("timeout", "host slow, retry", None, None, capabilities)
            except actions.TargetError as e:
                target_error = str(e)
        decision = policy.decide(action, target_error)
        if not decision.allowed:
            self._store.record_event("proposal.refused", action=action, stack=stack, service=service,
                                     reason=decision.reason, requested_via=origin, requested_by=operator)
            return RequestResult("refused", decision.reason, None, None, capabilities)
        if not decision.needs_approval:
            return RequestResult("refused", f"{action} is a read, not an action", None, None, capabilities)
        if not policy.allows_direct_request(decision.risk):
            return RequestResult("refused", f"direct requests are not allowed for {decision.risk}",
                                 None, None, capabilities)
        runbook_decision = policy.decide_runbook(plan, origin) if plan is not None else None
        if runbook_decision is None or not runbook_decision.allowed:
            reason = runbook_decision.reason if runbook_decision else "no runbook for this action"
            return RequestResult("refused", reason, None, None, capabilities)

        owner = f"act:direct:{secrets.token_hex(6)}"
        if not self._state.try_begin_mutation(owner):
            return RequestResult("busy", f"Busy right now ({self._state.busy_reason}).",
                                 None, None, capabilities)
        with self._execution_handoff(owner) as start:
            # Wait for any pending card delivery to finish before adopting its row, but only for what
            # is left of the request budget: propose() holds this lock across a Telegram send (up to
            # 35s) and the dashboard's caller gives up after 5s. Nothing is created on timeout, so a
            # restart never starts after its caller was told it failed (Codex review, T14).
            left = timeout - (self._clock() - arrived)
            if left <= 0 or not self._proposal_card_lock.acquire(timeout=left):
                return RequestResult("timeout", "host slow, retry", None, None, capabilities)
            try:
                result = self._store.create_direct_runbook_execution(
                    action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                    operator=operator, arrived_at=arrived, deadline=arrived + timeout,
                    plan_json=runbooks.canonical_json(plan), plan_sha256=runbooks.plan_sha256(plan),
                    origin=origin,
                )
                if result is None:
                    return RequestResult("timeout", "host slow, retry", None, None, capabilities)
                row, execution = result["approval"], result["execution"]
                if result["adopted"] and row["message_id"] is not None:
                    self._safe_finalize_card(
                        row, None,
                        ack="Approved. Restarting…" if action == actions.RESTART_SERVICE else "Approved. Starting…",
                        text=self._direct_adopted_text(row, target, operator, origin),
                    )
            finally:
                self._proposal_card_lock.release()
            if action == actions.RESTART_SERVICE:
                text = f"🛠 {_s(operator)} restarted <code>{_s(target.key)}</code> from the dashboard."
            else:
                text = f"🛠 {_s(operator)} requested {_s(actions.action_summary(action, target.as_dict()))}."
            # Queued before the worker starts, so the outcome message can never land first. A spawn
            # failure queues its own "failed to start" message after it.
            self._in_background(lambda: self._notify_quietly(text))
            start(row, execution["id"], operator)
            return RequestResult("started", text, row["id"], execution["id"], capabilities)

    # ── abort / rollback (slice 5b-1, T40; design §4.4) ─────────────────────
    def abort(self, execution_id: str, *, operator: str) -> ControlResult:
        """Stop a running execution before its next step (and during a `wait`). A command already
        running is never killed: a half-applied compose change is worse than finishing it."""
        outcome = self._store.request_abort(execution_id, operator=operator)
        messages = {
            "requested": "Abort requested: the run stops before its next step; the current step finishes.",
            "already": "Abort was already requested.",
            "not_running": "That run has already finished.",
            "unknown": "Unknown execution.",
        }
        return ControlResult(outcome, messages[outcome], execution_id)

    def rollback(self, execution_id: str, *, operator: str) -> ControlResult:
        """Reverse, newest first, every step of a finished run that changed the host and has a true
        inverse. Unknown outcomes are re-checked and otherwise left alone; steps with no inverse are
        listed. Runs as a child execution of the same approval, under the mutation lock."""
        parent = self._store.get_execution(execution_id)
        if parent is None:
            return ControlResult("unknown", "Unknown execution.")
        if parent["kind"] != "run":
            return ControlResult("refused", "A rollback cannot itself be rolled back.")
        if parent["status"] not in engine.TERMINAL_RUN_STATUSES:
            return ControlResult("refused", "That run is still going; abort it first, then roll it back.")
        children = self._store.rollbacks_of(execution_id)
        if any(child["status"] in ("running", "verifying") for child in children):
            return ControlResult("already", "A rollback of this run is already in progress.",
                                 children[-1]["id"])
        if any(child["status"] == "passed" for child in children):
            return ControlResult("already", "This run was already rolled back.", children[-1]["id"])

        approval = self._store.get_approval(parent["approval_id"])
        owner = f"rollback:{execution_id}"
        if not self._state.try_begin_mutation(owner):
            return ControlResult("busy", f"Busy right now ({self._state.busy_reason}).")
        handed_off = False
        try:
            try:
                plan = engine.plan_rollback(self, self._row_summary(approval),
                                            self._store.list_steps(execution_id))
            except actions.TargetError as exc:
                return ControlResult("refused", f"Cannot roll back: {redact(str(exc))[:200]}")
            report = {"undo": plan.undo, "not_reversible": plan.not_reversible, "unknown": plan.unknown}
            if plan.runbook is None:
                return ControlResult("nothing_to_undo", self._rollback_summary("Nothing to undo", report),
                                     execution_id, report)
            # The original approval authorises undoing what it did, but a risk the operator has since
            # forbidden must still not run (own review, T40).
            risk = runbooks.risk(plan.runbook)
            if risk in config.AUTONOMY.forbidden_risks:
                return ControlResult(
                    "refused", f"Rolling this back needs {risk}, which current policy forbids.",
                    execution_id, report,
                )
            child = self._store.create_rollback_execution(execution_id, operator=operator)
            if child is None:
                return ControlResult("already", "A rollback of this run is already in progress.")
            text = self._rollback_summary(
                f"↩️ {_s(operator)} is rolling back {_s(self._row_summary(approval))}", report)
            self._in_background(lambda: self._notify_quietly(text))
            try:
                self._spawn(self._run_rollback, approval, parent, child["id"], plan.runbook, owner)
            except Exception:
                self._store.set_execution_status(child["id"], "failed", reason="failed to start")
                self._in_background(lambda: self._notify_quietly(
                    f"🔴 <b>Rollback failed to start</b>: {_s(self._row_summary(approval))}"))
                raise
            handed_off = True
            return ControlResult("started", text, child["id"], report)
        finally:
            if not handed_off:
                self._state.end_mutation(owner)

    def _run_rollback(self, approval: dict, parent: dict, child_id: str, runbook, owner: str) -> None:
        """Worker thread: the rollback child holds the lock until it finishes."""
        try:
            result = engine.RunbookEngine(
                self,
                is_abort_requested=lambda: bool(
                    (self._store.get_execution(child_id) or {}).get("abort_requested_at")
                ),
            ).run(child_id, runbook, origin="rollback")
            self._store.set_execution_status(child_id, result.status, reason=result.reason)
            self._store.set_execution_status(
                parent["id"], "rolled_back" if result.status == "passed" else "rollback_failed",
                reason=parent["reason"],
            )
            done = ("🟢 <b>Rolled back</b>" if result.status == "passed"
                    else "🔴 <b>Rollback did not finish</b>")
            self._in_background(lambda: self._notify_quietly(
                f"{done}: {_s(self._row_summary(approval))}. {_s(result.reason)}"))
        except Exception as exc:
            log.exception(f"Rollback {child_id} crashed")
            try:
                engine.settle_crashed_execution(self._store, child_id)
                self._store.set_execution_status(
                    child_id, "failed", reason=f"crashed: {redact(str(exc))[:200]}")
                self._store.set_execution_status(parent["id"], "rollback_failed", reason=parent["reason"])
            except Exception:
                log.exception(f"Could not record the rollback crash of {child_id}")
        finally:
            self._state.end_mutation(owner)

    @staticmethod
    def _rollback_summary(head: str, report: dict) -> str:
        parts = [head + "."]
        if report["undo"]:
            parts.append("Undoing: " + ", ".join(report["undo"]) + ".")
        if report["not_reversible"]:
            parts.append("Not reversible (left as is): " + ", ".join(report["not_reversible"]) + ".")
        if report["unknown"]:
            parts.append("Outcome still unknown — check by hand: " + ", ".join(report["unknown"]) + ".")
        return " ".join(parts)

    def get_status(self, execution_id: str) -> dict | None:
        execution = self._store.get_execution(execution_id)
        if execution is not None:
            approval = self._store.get_approval(execution["approval_id"])
            spec = actions.REGISTRY.get(approval["action"])
            steps = self._store.list_steps(execution_id)
            preview = engine.rollback_preview(steps)
            children = self._store.rollbacks_of(execution_id) if execution["kind"] == "run" else []
            rolled_back = [c for c in children if c["status"] in ("running", "verifying", "passed")]
            # Controls come from what this plan can actually do, not from fixed per-action flags:
            # ABORT only while steps remain, ROLL BACK only when something is genuinely undoable.
            abortable = (
                execution["status"] in ("running", "verifying")
                and execution["abort_requested_at"] is None
                and any(step["status"] in ("pending", "dispatched") for step in steps)
            )
            rollbackable = (
                execution["kind"] == "run"
                and execution["status"] in engine.TERMINAL_RUN_STATUSES
                and bool(preview["undo"] or preview["unknown"])
                and not rolled_back
            )
            execution["capabilities"] = (spec.capabilities() if spec else {}) | {
                "abortable": abortable, "rollbackable": rollbackable, "resumable": False,
            }
            execution["rollback_preview"] = preview
            execution["rollbacks"] = [
                {"id": c["id"], "status": c["status"], "reason": c["reason"]} for c in children
            ]
            execution["approval"] = {
                key: approval[key] for key in (
                    "id", "action", "risk", "requested_via", "requested_by", "decided_by", "decided_at"
                )
            }
            execution["approval"]["target"] = json.loads(approval["target_json"])
            execution["action"] = approval["action"]
            execution["target"] = execution["approval"]["target"]
            execution["summary"] = self._row_summary(approval)
            execution["steps"] = [
                {key: step[key] for key in (
                    "n", "type", "status", "effect", "reason", "started_at", "finished_at",
                )} | {"label": self._step_label(step, execution["summary"])}
                for step in steps
            ]
        return execution

    @staticmethod
    def _step_label(step: dict, summary: str) -> str:
        params, kind = step["params"] or {}, step["type"]
        target = "/".join(params[k] for k in ("stack", "service") if params.get(k))
        labels = {
            "service.restart": f"Restart {target}", "service.start": f"Start {target}",
            "service.stop": f"Stop {target}", "wait": f"Wait {params.get('seconds')}s",
            "check.container": f"Check {(step['binding'] or {}).get('container', '')} is {params.get('expect')}",
            "check.log_since_start": "Check the log since start",
            "read.qbittorrent_session_port": "Read qBittorrent's port",
            "unit.action": f"{str(params.get('action', '')).capitalize()} {params.get('unit', '')}",
            "prune.safe": "Prune unused images and networks",
        }
        return labels.get(kind) or summary

    # ── execution (worker thread) ───────────────────────────────────────────
    def _current_policy(self, row: dict):
        """Re-check today's policy before consuming an approval (T24). A runbook approval is judged
        as a runbook — its action name ("runbook") is not in the legacy registry (slice 5b-2)."""
        plan = self._verified_plan(row)
        if plan is not None and row["action"] not in actions.REGISTRY:
            decision = policy.decide_runbook(plan, row.get("origin") or row["requested_via"])
            return policy.PolicyDecision(decision.allowed, decision.needs_approval, decision.risk,
                                         decision.reason)
        return policy.decide(row["action"])

    @staticmethod
    def _row_summary(row: dict) -> str:
        """A human summary of a stored approval that never raises: startup reconciliation and the
        dashboard read rows whose action may not be a legacy typed action (planner runbooks, a
        rehearsal) — an assumption here crashed core at startup on the VM (T39 rehearsal)."""
        if row.get("action") in engine.LEGACY_STEP_TYPES:
            try:
                return actions.action_summary(row["action"], json.loads(row["target_json"]))
            except Exception:  # noqa: BLE001 -- fall through to the plan's own title
                log.debug("Legacy summary unavailable for %s", row.get("id"))
        try:
            return str(json.loads(row["plan_json"])["title"])[:200]
        except Exception:  # noqa: BLE001
            return str(row.get("action") or "typed action")

    @classmethod
    def _row_label(cls, row: dict) -> str:
        try:
            target = json.loads(row["target_json"]) or {}
        except Exception:  # noqa: BLE001 -- a label must never raise
            target = {}
        if target.get("scope") == "all":
            return "every stack"
        return target.get("stack") or cls._row_summary(row)

    @staticmethod
    def _verified_plan(row: dict | None) -> runbooks.Runbook | None:
        if not row or not row.get("plan_json") or not row.get("plan_sha256"):
            return None
        try:
            return runbooks.load_stored_plan(row["plan_json"], row["plan_sha256"])
        except Exception:  # noqa: BLE001 -- any invalid/tampered document is refused
            return None

    def _run_execution(self, row: dict, execution_id: str, owner: str, decided_by: str) -> None:
        try:
            plan = self._verified_plan(self._store.get_approval(row["id"]))
            if plan is None:
                self._finish(row, execution_id, "failed",
                             "refused: its approved plan is missing or does not match its fingerprint")
                return
            runner = engine.RunbookEngine(
                self,
                is_abort_requested=lambda: bool(
                    (self._store.get_execution(execution_id) or {}).get("abort_requested_at")
                ),
                on_failure=self._on_failure,
            )
            result = runner.run(execution_id, plan, origin=row.get("origin") or row["requested_via"])
            self._finish(row, execution_id, result.status, result.reason)
        except Exception as e:
            log.exception(f"Typed action {execution_id} crashed")
            try:
                engine.settle_crashed_execution(self._store, execution_id)
            except Exception:
                log.exception(f"Could not settle the steps of {execution_id}")
            self._finish(row, execution_id, "failed", f"crashed: {redact(str(e))[:200]}")
        finally:
            self._state.end_mutation(owner)

    @staticmethod
    def _stack_report(passed: list[str], failed: str | None, reason: str | None,
                      not_attempted: list[str]) -> str:
        parts = ["passed: " + (", ".join(passed) if passed else "none")]
        if failed is not None:
            parts.append(f"failed: {failed} ({reason})")
        parts.append("not attempted: " + (", ".join(not_attempted) if not_attempted else "none"))
        return "; ".join(parts)

    def _finish(self, row: dict, execution_id: str, status: str, reason: str) -> None:
        self._store.set_execution_status(execution_id, status, reason=reason)
        key = _s(row["target_key"])
        if row["action"] == actions.RESTART_SERVICE and status == "passed":
            text = f"🟢 <b>Verified good</b>: <code>{key}</code> restarted. {_s(reason)}"
        elif row["action"] == actions.RESTART_SERVICE:
            text = f"🔴 <b>Restart failed</b> for <code>{key}</code>: {_s(reason)}"
        else:
            direction = "Up" if row["action"] in actions.UP_ACTIONS else (
                "Down" if row["action"] in actions.DOWN_ACTIONS else "Run")
            label = self._row_label(row)
            if status == "passed":
                text = f"🟢 <b>Verified good</b>: <code>{_s(label)} {direction.lower()}</code>. {_s(reason)}"
            else:
                text = f"🔴 <b>{direction} failed</b> for <code>{_s(label)}</code>: {_s(reason)}"

        def deliver():
            try:
                self._notifier.notify(text)
            except Exception:  # noqa: BLE001 -- notification delivery cannot change action outcome
                log.warning(f"Failed to send outcome notification for {execution_id}")
            try:
                current = self._store.get_approval(row["id"])
                message_id = (current or row)["message_id"]
                if message_id is not None:
                    self._notifier.update_request(message_id, text)
            except Exception:  # noqa: BLE001 -- best-effort UI update; action is already terminal
                log.warning(f"Failed to update approval card for {execution_id}")

        # Same serial worker as the card's earlier edit, so the final text lands last.
        self._in_background(deliver)

    # ── startup reconciliation ──────────────────────────────────────────────
    def reconcile_on_startup(self) -> list[dict]:
        """Before polling starts: an execution still running/verifying means core died
        mid-action. Mark it interrupted and say so; the consumed approval is not revived."""
        try:
            engine.startup_reconcile(self._store)
        except Exception:
            log.exception("Step reconciliation at startup failed")
        interrupted = self._store.interrupt_unfinished("core restarted")
        for r in interrupted:
            try:
                text = self._interrupted_text(r)
            except Exception:
                log.exception(f"Could not describe interrupted execution {r['id']}")
                text = f"⏻ Execution <code>{_s(r['id'])}</code> was interrupted: Planet Express restarted mid-run."
            try:
                self._notifier.notify(text)
            except Exception:  # noqa: BLE001 -- reconciliation must not block core startup
                log.warning(f"Failed to notify about interrupted execution {r['id']}")
            try:
                if r["message_id"] is not None:
                    self._notifier.update_request(r["message_id"], text)
            except Exception:  # noqa: BLE001 -- reconciliation must not block core startup
                log.warning(f"Failed to update card for interrupted execution {r['id']}")
        return interrupted


    def _interrupted_text(self, r: dict) -> str:
        target = r["target"]
        if r["action"] == actions.RESTART_SERVICE and {"stack", "service", "container"} <= set(target):
            text = (f"⏻ Restart of <code>{_s(target['stack'])}/{_s(target['service'])}</code> was "
                    f"<b>interrupted</b>: Planet Express restarted mid-run, so its outcome is unknown. "
                    f"Check <code>{_s(target['container'])}</code>, then propose it again if still needed.")
        else:
            summary = self._row_summary(r)
            text = (f"⏻ <b>{_s(summary)}</b> was interrupted: Planet Express restarted mid-run, "
                    "so its outcome is unknown. Check the stack state, then propose it again if needed.")
        return text

    # ── helpers ─────────────────────────────────────────────────────────────
    def _card_text(self, row: dict, target, requested_by: str | None) -> str:
        minutes = int(self._ttl // 60)
        if row["action"] != actions.RESTART_SERVICE:
            value = target.as_dict()
            radius = (f"every stack ({len(value['stacks'])})" if value.get("scope") == "all"
                      else value["stack"])
            return (
                f"🛠 <b>{_s(actions.action_summary(row['action'], value))}</b> "
                f"<code>{_s(row['id'])}</code>\n"
                f"Target: <code>{_s(radius)}</code>\n"
                f"Risk: {_s(row['risk'])}, stack-impacting operation.\n"
                f"Requested by {_s(requested_by or 'unknown')}. Expires in {minutes} min."
            )
        return (
            f"🔁 <b>Restart request</b> <code>{_s(row['id'])}</code>\n"
            f"Target: <code>{_s(target.key)}</code> (container <code>{_s(target.container)}</code>)\n"
            f"Risk: {_s(row['risk'])}, service-impacting: the service is unavailable while it restarts.\n"
            f"Requested by {_s(requested_by or 'unknown')}. Expires in {minutes} min."
        )

    @staticmethod
    def _decision_text(row: dict, outcome: str, actor: str) -> str:
        key = _s(row["target_key"])
        if row["action"] == actions.RESTART_SERVICE:
            if outcome == "denied":
                return f"❌ Restart of <code>{key}</code> denied by {_s(actor)}."
            return f"✅ Restart of <code>{key}</code> approved by {_s(actor)}. Restarting…"
        summary = _s(CommandService._row_summary(row))
        if outcome == "denied":
            return f"❌ {summary} denied by {_s(actor)}."
        return f"✅ {summary} approved by {_s(actor)}. Starting…"

    @staticmethod
    def _policy_refusal_text(row: dict, reason: str) -> str:
        if row["action"] == actions.RESTART_SERVICE:
            return f"🚫 Restart of <code>{_s(row['target_key'])}</code> {_s(reason)}."
        summary = CommandService._row_summary(row)
        return f"🚫 {_s(summary)} {_s(reason)}."

    @staticmethod
    def _direct_adopted_text(row: dict, target, operator: str, origin: str) -> str:
        if row["action"] == actions.RESTART_SERVICE:
            return (f"✅ Restart of <code>{_s(target.key)}</code> approved by {_s(operator)} "
                    "from the dashboard. Restarting…")
        source = "Telegram" if origin == "telegram-direct" else "the dashboard"
        summary = actions.action_summary(row["action"], target.as_dict())
        return f"✅ {_s(summary)} approved by {_s(operator)} from {source}. Starting…"

    @staticmethod
    def _not_pending(row: dict | None) -> DecideResult:
        if row is None:
            return DecideResult("unknown", "Unknown or already removed request.")
        if row["status"] == "expired":
            return DecideResult("expired", "⌛ This request expired. Propose it again if it's still needed.")
        return DecideResult("already_decided", f"Already {row['status']} by {row['decided_by']}.")

    def _reply(self, decision: Decision | None, result: DecideResult) -> DecideResult:
        if decision is not None:
            plain = _TAG_RE.sub("", result.message)[:_CALLBACK_TEXT_LIMIT]
            self._notifier.acknowledge(decision, plain)
        return result

    def _finalize_card(self, row: dict, decision: Decision | None, *, ack: str, text: str) -> None:
        if decision is not None:
            self._notifier.resolve(decision, ack, text)
        else:
            self._notifier.update_request(row["message_id"], text)

    def _safe_finalize_card(
        self, row: dict, decision: Decision | None, *, ack: str, text: str
    ) -> None:
        """Best-effort presentation after the durable decision is recorded.

        A Telegram outage must not strand a consumed approval or suppress the host action.
        The database remains the source of truth and later front ends can display its state.
        A Telegram tap is answered inline (its callback needs the ack); a dashboard decision
        (decision=None) edits the card in the background so the RPC reply never waits on Telegram.
        """
        def finalize():
            try:
                current = self._store.get_approval(row["id"]) if decision is None else row
                self._finalize_card(current or row, decision, ack=ack, text=text)
            except Exception:  # noqa: BLE001 -- never log exception text; it can include the bot token
                log.warning(f"Failed to finalize approval card for {row['id']}")

        if decision is None:
            self._in_background(finalize)
        else:
            finalize()

    def _notify_quietly(self, text: str) -> None:
        try:
            self._notifier.notify(text)
        except Exception:  # noqa: BLE001 -- notification errors can contain credentials
            log.warning("Failed to send a dashboard action notification")

    def _update_card_quietly(self, message_id: int, text: str) -> None:
        try:
            self._notifier.update_request(message_id, text)
        except Exception:  # noqa: BLE001 -- never log the error text: it can carry the bot token
            log.warning(f"Failed to update approval card {message_id}")

    def _in_background(self, fn: Callable[[], None]) -> None:
        try:
            self._background(fn)
        except Exception:  # noqa: BLE001 -- a presentation side effect must never undo an accepted action
            log.warning("Failed to schedule a Telegram update")
