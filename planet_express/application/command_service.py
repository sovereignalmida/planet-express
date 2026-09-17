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
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import casa_bender as bender
from notifier import Decision, Notifier
from planet_express.core.store import DEFAULT_TTL_SECONDS, Store
from planet_express.execution import actions, policy
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
        # Store.propose deduplicates rows atomically, but sending the corresponding card
        # is an external side effect. Keep row creation, card delivery and message-id
        # recording in one in-process critical section so two front-end requests cannot
        # both observe the same NULL message_id and publish duplicate actionable cards.
        self._proposal_card_lock = threading.Lock()

    # ── propose ─────────────────────────────────────────────────────────────
    def propose(
        self, action: str, stack: str, service: str, *, requested_via: str, requested_by: str | None,
        timeout=actions.DOCKER_TIMEOUT_SECONDS,
    ) -> ProposeResult:
        target = None
        target_error = None
        if action in actions.REGISTRY:
            try:
                target = self._resolve(stack, service, for_mutation=True, timeout=timeout)
            except actions.TargetTimeout:
                return ProposeResult(False, None, False, "host slow, retry")
            except actions.TargetError as e:
                target_error = str(e)
        decision = policy.decide(action, target_error)
        if not decision.allowed:
            self._store.record_event("proposal.refused", action=action, stack=stack, service=service,
                                     reason=decision.reason, requested_via=requested_via,
                                     requested_by=requested_by)
            return ProposeResult(False, None, False, decision.reason)
        if not decision.needs_approval:
            # Slice 1 registers no automatic mutations; an R0 action is a read, not a proposal.
            return ProposeResult(False, None, False, f"{action} runs automatically and is not proposed")

        if requested_via not in policy.OPERATOR_ORIGINS:
            now = self._clock()
            reason = policy.limit_refusal(
                self._store.recent_attempts(action, target.key, now - policy.limit_lookback_seconds()), now,
            )
            if reason is not None:
                self._store.record_event(
                    "proposal.refused", action=action, stack=stack, service=service,
                    reason=reason, requested_via=requested_via, requested_by=requested_by,
                )
                return ProposeResult(False, None, False, reason)

        with self._proposal_card_lock:
            row, created = self._store.propose(
                action=action, target_key=target.key, target=target.as_dict(), risk=decision.risk,
                requested_via=requested_via, requested_by=requested_by, ttl_seconds=self._ttl,
            )
            if created or row["message_id"] is None:
                # A pending row without a card (its send failed last time) gets one now. Otherwise a
                # transient Telegram error would leave an approval nobody can see or act on, and
                # every retry would dedup against it until it expired (Codex review, landing 1b).
                try:
                    message_id = self._notifier.request_approval(
                        self._card_text(row, target, requested_by), row["id"], "action"
                    )
                except Exception:  # noqa: BLE001 -- never log the error text: Telegram errors can carry the bot token
                    log.warning(f"Failed to send the approval card for {row['id']}")
                    self._store.record_event("proposal.card_failed", approval_id=row["id"])
                    return ProposeResult(
                        False, row["id"], created, "could not send the approval card; send the request again to retry"
                    )
                self._store.set_message_id(row["id"], message_id)
                return ProposeResult(True, row["id"], created, "awaiting approval")
            return ProposeResult(True, row["id"], False, "already awaiting approval")

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

        key = row["target_key"]
        if not approve:
            if not self._store.consume(approval_id, decision="denied", decided_by=decided_by, arrived_at=arrived):
                return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))
            text = f"❌ Restart of <code>{_s(key)}</code> denied by {_s(decided_by)}."
            self._safe_finalize_card(row, decision, ack="Denied.", text=text)
            return DecideResult("denied", text)

        # Re-check CURRENT policy before consuming an approval. A card created before the operator
        # forbade its risk class (config change + core restart) must not still execute: pending
        # approvals survive restarts, and propose() only checked the policy of its own time
        # (Codex review, T24). The approval is closed as denied so the card can't be retried.
        current = policy.decide(row["action"])
        if not current.allowed:
            reason = f"refused by current policy: {current.reason}"
            if self._store.consume(approval_id, decision="denied", decided_by="policy", arrived_at=arrived):
                self._store.record_event("approval.refused_by_policy", approval_id=approval_id,
                                         reason=current.reason, attempted_by=decided_by)
                text = f"🚫 Restart of <code>{_s(key)}</code> {_s(reason)}."
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
            execution = self._store.approve_and_create_execution(
                approval_id, decided_by=decided_by, arrived_at=arrived
            )
            if execution is None:
                return self._reply(decision, self._not_pending(self._store.get_approval(approval_id)))
            execution_id = execution["id"]
            text = f"✅ Restart of <code>{_s(key)}</code> approved by {_s(decided_by)}. Restarting…"
            self._safe_finalize_card(row, decision, ack="Approved. Restarting…", text=text)
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
                key = _s(row["target_key"])
                self._in_background(lambda: self._notify_quietly(
                    f"🔴 <b>Restart failed</b> for <code>{key}</code>: failed to start"))
                raise
            handed_off = True

        try:
            yield start
        finally:
            if not handed_off:
                self._state.end_mutation(owner)

    def request_action(
        self, action: str, stack: str, service: str, *, operator: str,
        timeout=actions.DOCKER_TIMEOUT_SECONDS,
    ) -> RequestResult:
        arrived = self._clock()
        spec = actions.REGISTRY.get(action)
        capabilities = spec.capabilities() if spec else {}
        target = None
        target_error = None
        if spec is not None:
            try:
                target = self._resolve(stack, service, for_mutation=True, timeout=timeout)
            except actions.TargetTimeout:
                return RequestResult("timeout", "host slow, retry", None, None, capabilities)
            except actions.TargetError as e:
                target_error = str(e)
        decision = policy.decide(action, target_error)
        if not decision.allowed:
            self._store.record_event("proposal.refused", action=action, stack=stack, service=service,
                                     reason=decision.reason, requested_via="dashboard-direct", requested_by=operator)
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
                    operator=operator, arrived_at=arrived, deadline=arrived + timeout,
                )
                if result is None:
                    return RequestResult("timeout", "host slow, retry", None, None, capabilities)
                row, execution = result["approval"], result["execution"]
                if result["adopted"] and row["message_id"] is not None:
                    self._safe_finalize_card(
                        row, None, ack="Approved. Restarting…",
                        text=f"✅ Restart of <code>{_s(target.key)}</code> approved by {_s(operator)} "
                             "from the dashboard. Restarting…",
                    )
            finally:
                self._proposal_card_lock.release()
            text = f"🛠 {_s(operator)} restarted <code>{_s(target.key)}</code> from the dashboard."
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
        return execution

    # ── execution (worker thread) ───────────────────────────────────────────
    def _run_execution(self, row: dict, execution_id: str, owner: str, decided_by: str) -> None:
        approved = json.loads(row["target_json"])
        try:
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

    def _finish(self, row: dict, execution_id: str, status: str, reason: str) -> None:
        self._store.set_execution_status(execution_id, status, reason=reason)
        key = _s(row["target_key"])
        if status == "passed":
            text = f"🟢 <b>Verified good</b>: <code>{key}</code> restarted. {_s(reason)}"
        else:
            text = f"🔴 <b>Restart failed</b> for <code>{key}</code>: {_s(reason)}"

        def deliver():
            try:
                self._notifier.notify(text)
            except Exception:  # noqa: BLE001 -- notification delivery cannot change action outcome
                log.warning(f"Failed to send outcome notification for {execution_id}")
            try:
                if row["message_id"] is not None:
                    self._notifier.update_request(row["message_id"], text)
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
            text = (f"⏻ Restart of <code>{_s(target['stack'])}/{_s(target['service'])}</code> was "
                    f"<b>interrupted</b>: Planet Express restarted mid-run, so its outcome is unknown. "
                    f"Check <code>{_s(target['container'])}</code>, then propose it again if still needed.")
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
    def _card_text(self, row: dict, target: actions.Target, requested_by: str | None) -> str:
        minutes = int(self._ttl // 60)
        return (
            f"🔁 <b>Restart request</b> <code>{_s(row['id'])}</code>\n"
            f"Target: <code>{_s(target.key)}</code> (container <code>{_s(target.container)}</code>)\n"
            f"Risk: {_s(row['risk'])}, service-impacting: the service is unavailable while it restarts.\n"
            f"Requested by {_s(requested_by or 'unknown')}. Expires in {minutes} min."
        )

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
                self._finalize_card(row, decision, ack=ack, text=text)
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
