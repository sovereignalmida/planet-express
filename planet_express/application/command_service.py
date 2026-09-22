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
from planet_express.core.store import DEFAULT_TTL_SECONDS, IncidentSourceError, Store
from planet_express.execution import actions, policy
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
    ):
        self._store = store
        self._notifier = notifier
        self._state = state  # casa_farnsworth.PipelineState (duck-typed to avoid an import cycle)
        self._run_argv = run_argv or bender.run_argv
        self._resolve = resolve_target or actions.resolve_target
        self._verify = verify or actions.verify_after_restart
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
        if action in actions.REGISTRY:
            try:
                target = self._resolve_for_action(
                    action, stack, service, timeout=self._remaining(deadline)
                )
            except actions.TargetTimeout:
                return ProposeResult(False, None, False, "host slow, retry")
            except actions.TargetError as e:
                target_error = str(e)
            if target is not None and expected_container is not None and target.container != expected_container:
                target_error = "resolved container does not match the incident resource"
        decision = policy.decide(action, target_error)
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
                row, created = self._store.propose(
                    action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                    requested_via=requested_via, requested_by=requested_by, ttl_seconds=self._ttl,
                    timeout=self._remaining(deadline),
                    **kwargs,
                )
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

        provenance = self._store.get_incident_proposal(approval_id)

        # Generic approvals retain the original early policy check. Incident approvals first take
        # the scan/mutation lock and validate their evidence below, so stale evidence always gets
        # the incident-specific durable refusal required by T33.
        if provenance is None:
            current = policy.decide(row["action"])
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
                current = policy.decide(row["action"])
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
                    target = json.loads(row["target_json"])
                    direction = "Up" if row["action"] in actions.UP_ACTIONS else "Down"
                    label = "every stack" if target.get("scope") == "all" else target["stack"]
                    text = (f"🔴 <b>{direction} failed</b> for <code>{_s(label)}</code>: "
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
        if spec is not None:
            try:
                target = self._resolve_for_action(action, stack, service, timeout=timeout)
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
                result = self._store.create_direct_execution(
                    action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                    operator=operator, origin=origin, arrived_at=arrived, deadline=arrived + timeout,
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

    def get_status(self, execution_id: str) -> dict | None:
        execution = self._store.get_execution(execution_id)
        if execution is not None:
            approval = self._store.get_approval(execution["approval_id"])
            spec = actions.REGISTRY.get(approval["action"])
            execution["capabilities"] = spec.capabilities() if spec else {}
            execution["approval"] = {
                key: approval[key] for key in (
                    "id", "action", "risk", "requested_via", "requested_by", "decided_by", "decided_at"
                )
            }
            execution["approval"]["target"] = json.loads(approval["target_json"])
            execution["action"] = approval["action"]
            execution["target"] = execution["approval"]["target"]
            execution["summary"] = actions.action_summary(approval["action"], execution["target"])
        return execution

    # ── execution (worker thread) ───────────────────────────────────────────
    def _run_execution(self, row: dict, execution_id: str, owner: str, decided_by: str) -> None:
        approved = json.loads(row["target_json"])
        try:
            if row["action"] in actions.STACK_ACTIONS:
                self._run_stack_execution(row, execution_id, approved)
                return
            try:
                target = self._resolve(approved["stack"], approved["service"], for_mutation=True)
            except actions.TargetError as e:
                self._finish(row, execution_id, "failed", f"target no longer valid: {e}")
                return
            if target.container != approved["container"]:
                self._finish(row, execution_id, "failed",
                             f"container changed since approval ({approved['container']} → {target.container})")
                return

            baseline = self._restart_count(target.container)
            rc, out, err = self._run_argv(actions.restart_argv(target), timeout=actions.DOCKER_TIMEOUT_SECONDS)
            if rc != 0:
                self._finish(row, execution_id, "failed",
                             f"restart command failed (exit {rc}): {(err or out)[:300]}")
                return

            self._store.set_execution_status(execution_id, "verifying")
            ok, reason = self._verify(target.container, baseline)
            self._finish(row, execution_id, "passed" if ok else "failed", reason)
        except Exception as e:
            log.exception(f"Typed action {execution_id} crashed")
            self._finish(row, execution_id, "failed", f"crashed: {str(e)[:200]}")
        finally:
            self._state.end_mutation(owner)

    def _run_stack_execution(self, row: dict, execution_id: str, approved: dict) -> None:
        action = row["action"]
        selector = "all" if action in actions.ALL_STACK_ACTIONS else approved.get("stack")
        try:
            target = self._resolve_for_action(
                action, selector, None, timeout=actions.DOCKER_TIMEOUT_SECONDS,
                approved_target=approved,
            )
        except actions.TargetError as exc:
            self._finish(row, execution_id, "failed", f"target no longer valid: {exc}")
            return
        stacks = list(target.stacks) if isinstance(target, actions.AllStacksTarget) else [target.stack]
        passed = []
        for index, stack in enumerate(stacks):
            rc, out, err = self._run_argv(
                actions.stack_argv(action, stack), timeout=actions.STACK_VERIFY_TIMEOUT_SECONDS
            )
            if rc != 0:
                # Fail fast with compose's own error, as the restart path does, instead of waiting
                # out the verifier and reporting only a timeout (own review, T37). Redacted: this
                # text reaches Telegram and the dashboard.
                detail = redact((err or out or "").strip())[:300]
                report = self._stack_report(
                    passed, stack, f"compose exited {rc}: {detail}", stacks[index + 1:]
                )
                self._finish(row, execution_id, "failed", report)
                return
            self._store.set_execution_status(execution_id, "verifying")
            verifier = self._verify_stack_up if action in actions.UP_ACTIONS else self._verify_stack_down
            ok, reason = verifier(stack)
            if not ok:
                not_attempted = stacks[index + 1:]
                report = self._stack_report(passed, stack, reason, not_attempted)
                self._finish(row, execution_id, "failed", report)
                return
            passed.append(stack)
            if index + 1 < len(stacks):
                self._store.set_execution_status(execution_id, "running")
        self._finish(row, execution_id, "passed", self._stack_report(passed, None, None, []))

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
            target = json.loads(row["target_json"])
            direction = "Up" if row["action"] in actions.UP_ACTIONS else "Down"
            label = "every stack" if target.get("scope") == "all" else target["stack"]
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
        interrupted = self._store.interrupt_unfinished("core restarted")
        for r in interrupted:
            target = r["target"]
            if r["action"] == actions.RESTART_SERVICE:
                text = (f"⏻ Restart of <code>{_s(target['stack'])}/{_s(target['service'])}</code> was "
                        f"<b>interrupted</b>: Planet Express restarted mid-run, so its outcome is unknown. "
                        f"Check <code>{_s(target['container'])}</code>, then propose it again if still needed.")
            else:
                summary = actions.action_summary(r["action"], target)
                text = (f"⏻ <b>{_s(summary)}</b> was interrupted: Planet Express restarted mid-run, "
                        "so its outcome is unknown. Check the stack state, then propose it again if needed.")
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
        target = json.loads(row["target_json"])
        summary = _s(actions.action_summary(row["action"], target))
        if outcome == "denied":
            return f"❌ {summary} denied by {_s(actor)}."
        return f"✅ {summary} approved by {_s(actor)}. Starting…"

    @staticmethod
    def _policy_refusal_text(row: dict, reason: str) -> str:
        if row["action"] == actions.RESTART_SERVICE:
            return f"🚫 Restart of <code>{_s(row['target_key'])}</code> {_s(reason)}."
        summary = actions.action_summary(row["action"], json.loads(row["target_json"]))
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

    def _in_background(self, fn: Callable[[], None]) -> None:
        try:
            self._background(fn)
        except Exception:  # noqa: BLE001 -- a presentation side effect must never undo an accepted action
            log.warning("Failed to schedule a Telegram update")
