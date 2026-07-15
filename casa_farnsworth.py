"""
casa_farnsworth.py — Farnsworth: Orchestrator, Planner & Telegram Bot
"Good news, everyone! I've devised a plan that's only 12% likely to destroy the server."

Runs as a persistent service. Does three things:
  1. Long-polls Telegram for commands (/check, /status, /updates, /rollback, /skip)
  2. Handles ✅/❌ inline button callbacks — triggers Bender on approval
  3. Runs a background scheduler (every 6h by default) for the full pipeline

Pipeline: Leela → Hermes → Farnsworth (plan) → Telegram → [approval] → Bender

Usage:
    python casa_farnsworth.py          # start the bot service
    python casa_farnsworth.py --plan   # plan-only mode: reads STATE_FINDINGS, prints plans
"""

import argparse
import json
import logging
import shlex
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import casa_llm as llm
import casa_leela as leela
import casa_hermes as hermes
import casa_bender as bender
import casa_zoidberg as zoidberg
import casa_amy as amy
import casa_stackctl as stackctl
from telegram_client import TelegramClient
from notifier import Notifier, TelegramNotifier

log = logging.getLogger("planetexpress.farnsworth")

# ── Config ────────────────────────────────────────────────────────────────────
PIPELINE_INTERVAL_HOURS = 6
PLAN_EXPIRY_HOURS = 24
MAX_TOKENS = 8192  # 4096 truncates mid-JSON with 33 findings (~13k chars output)

# Canary auto-update cadence — deliberately separate from the 6h monitor cycle. Weekly,
# Sunday 05:00 local, spaced away from the existing Sunday 03:30 Lidarr cron and the
# 03:10/02:30 borg backup timers.
UPDATE_DAY_OF_WEEK = 6  # Python weekday(): Monday=0 ... Sunday=6
UPDATE_HOUR = 5

# Daily morning backup-status digest -- pure reporting, no approval gate.
DIGEST_HOUR = 8

# ── Farnsworth planning prompt ────────────────────────────────────────────────
PLAN_SYSTEM_PROMPT = """You are Professor Hubert J. Farnsworth, chief scientist and planner for Planet Express home lab (CasaMediaServer).
You receive structured findings from Hermes and produce concrete, safe, reversible action plans.
Return ONLY valid JSON. No prose, no markdown fences.

PLANNING RULES:
- Group related findings into ONE plan per group (not one per finding)
- Every plan must include a rollback
- Prefer: docker compose pull && docker compose up -d <service>   over full stack restarts
- Individual container restarts only; never docker compose down/up unless truly required
- For unhealthy postgres/db containers: ALWAYS check logs as step 1 before any restart
- For ANY step that starts, restarts, or recreates a container, ALWAYS append one more step
  after it that verifies the container is STILL running a bit later — not just that the
  start/restart command itself returned success. Use:
  sleep 30 && docker inspect --format '{{.State.Status}}' CONTAINER_NAME | grep -q running
  A container that starts fine and crashes 10 seconds later is not fixed — this step is what
  catches that. Use a longer sleep (e.g. 60-90) for containers with slow startup (databases,
  anything with a healthcheck start_period).
- When chaining multiple read-only diagnostic commands in one step (e.g. `systemctl status X`
  then `journalctl -u X`), join them with `;` not `&&`. `systemctl status` returns non-zero for
  a stopped/failed unit even when the command itself worked correctly — with `&&` the second
  diagnostic silently never runs in exactly the case you're investigating (the unit being down).
  Only use `&&` for action chains where the second command genuinely should be skipped if the
  first one failed (e.g. `docker compose pull && docker compose up -d`).
- Bender runs as an unprivileged user (casaroot) with passwordless sudo for EXACTLY these,
  and nothing else: docker commands (no sudo needed, direct socket access);
  `systemctl restart/start/stop casa-startup.service` (must be prefixed with `sudo`); and
  `systemctl start/stop` on units matching `*.mount` (must be prefixed with `sudo`).
  Any other privileged command — a different systemd service, a `*.automount` unit
  (note: this is a different unit type than `*.mount` and is NOT covered by the mount
  grant), raw `mount`/`umount`/`mount -a`, `systemctl reset-failed`, fstab edits, disk/
  partition tools, anything else needing root — will fail with "Interactive
  authentication required" or "a password is required" since Bender has no other
  passwordless grant and cannot type one interactively. Do NOT propose any such action.
  Make a diagnostic-only plan (or no plan) instead, and note in the title that it needs
  human action.
- NEVER modify .env files
- NEVER touch clawbot or ai stacks
- Set requires_network_confirm: true for any plan touching CASA_TRAEFIK or CASA_ADGUARD
- Commands must be concrete shell commands (docker, systemctl, df, journalctl, etc.)
- estimated_downtime: be conservative (round up)
- Do NOT create a plan for LOW severity image-update findings or for anything in
  update_candidates — Zoidberg already handles these on its own weekly canary-update schedule
  (with auto-rollback), one container at a time. You do not know the real stack-directory layout
  (many containers that look independent actually share one docker-compose.yml under
  ~/stacks/services/, not a per-app directory), so a plan step like
  "cd /home/casaroot/stacks/<app-name> && docker compose pull" is likely to reference a directory
  that doesn't exist. Leave image updates to Zoidberg entirely.
- Set "container" to the single container name this plan targets (e.g. "CASA_PLANKA"), if the
  plan is about one specific container. Use null for plans that don't target one container (a
  mount/backup/cert finding). This lets a failed plan escalate to a deeper investigation of the
  right container — don't skip it when a container name applies.

OUTPUT SCHEMA — return exactly this, nothing else:
{
  "planned_at": "<ISO timestamp>",
  "plans": [
    {
      "id": "p1",
      "priority": "critical|high|medium|low",
      "title": "short descriptive title",
      "finding_ids": ["f1"],
      "container": "CASA_PLANKA",
      "steps": [
        {
          "n": 1,
          "description": "Check postgres logs",
          "command": "docker logs CASA_PLANKA_POSTGRES --tail 50",
          "expected_output": "error messages indicating root cause"
        }
      ],
      "rollback": [
        {
          "n": 1,
          "description": "Restore container to previous state",
          "command": "docker start CONTAINER_NAME"
        }
      ],
      "estimated_downtime": "~2 minutes",
      "requires_confirmation": true,
      "requires_network_confirm": false
    }
  ]
}

If no findings require action, return plans as an empty array."""


# ── State management ──────────────────────────────────────────────────────────
class PipelineState:
    """Thread-safe pipeline state. Persisted to STATE_STATUS."""

    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self.IDLE
        self._pending_plan_id: str | None = None
        self._pending_msg_id: int | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def transition(self, new_state: str, plan_id: str | None = None, msg_id: int | None = None):
        with self._lock:
            log.info(f"State: {self._state} → {new_state}")
            self._state = new_state
            if plan_id is not None:
                self._pending_plan_id = plan_id
            if msg_id is not None:
                self._pending_msg_id = msg_id
            self._persist()

    def get_pending(self) -> tuple[str | None, int | None]:
        with self._lock:
            return self._pending_plan_id, self._pending_msg_id

    def _persist(self):
        try:
            config.ensure_dirs()
            config.STATE_STATUS.write_text(json.dumps({
                "state": self._state,
                "pending_plan_id": self._pending_plan_id,
                "pending_msg_id": self._pending_msg_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        except Exception as e:
            log.warning(f"State persist failed: {e}")


# ── Planning logic ────────────────────────────────────────────────────────────
def plan(findings: dict) -> dict:
    """Good news, everyone — Farnsworth has a plan."""
    if not findings.get("findings"):
        return {
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "plans": [],
        }

    findings_json = json.dumps(findings, separators=(",", ":"))

    log.info(f"Farnsworth devising plan for {len(findings['findings'])} finding(s)...")

    raw = llm.complete(
        PLAN_SYSTEM_PROMPT,
        f"Devise action plans for these findings:\n{findings_json}",
        MAX_TOKENS,
        tier="small",
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        plans = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Farnsworth got invalid JSON from the LLM: {e}")
        plans = {
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "plans": [],
            "_parse_error": str(e),
        }

    plans.setdefault("planned_at", datetime.now(timezone.utc).isoformat())
    log.info(f"Farnsworth devised {len(plans.get('plans', []))} plan(s)")
    return plans


def save_plans(plans: dict) -> None:
    config.ensure_dirs()
    # Add expiry timestamp
    plans["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=PLAN_EXPIRY_HOURS)
    ).isoformat()
    config.STATE_PLAN.write_text(json.dumps(plans, indent=2))


def load_pending_plan(plan_id: str) -> dict | None:
    if not config.STATE_PLAN.exists():
        return None
    try:
        data = json.loads(config.STATE_PLAN.read_text())
        # Check expiry
        expires_at = data.get("expires_at")
        if expires_at:
            exp = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) > exp:
                log.info("Pending plan has expired")
                return None
        for p in data.get("plans", []):
            if p["id"] == plan_id:
                return p
    except Exception as e:
        log.warning(f"Failed to load pending plan: {e}")
    return None


# ── Safe prune (space pressure + stack health gated) ──────────────────────────
# Root disk filling up from Docker image/layer buildup was a recurring real problem
# (monthly or worse) before this existed. Auto-runs, never needs approval — by
# construction it only removes images/networks Docker itself considers unused by any
# container, running or stopped, so nothing currently in service is ever at risk.
DISK_PRUNE_THRESHOLD_PCT = 80
ROLLBACK_CANDIDATES_FILE = config.STATE_DIR / "rollback_candidates.json"


def _root_disk_alert(snapshot: dict) -> dict | None:
    for d in snapshot.get("disk", []):
        if d.get("mount") == "/" and d.get("used_pct", 0) >= DISK_PRUNE_THRESHOLD_PCT:
            return d
    return None


def _container_blocks_prune(c: dict) -> bool:
    """True if this container's state means pruning is NOT safe right now.

    Conservative by default: only a container that's running with no unhealthy
    healthcheck, or one that exited cleanly (Exited (0) — e.g. a cron one-shot job like
    airbnb-notify), counts as safe. Crash-loops, non-zero exits, Created/Restarting/Paused,
    or anything unrecognized blocks pruning until resolved — better to skip a prune cycle
    than remove an image something currently broken might still need."""
    if c.get("crash_looping"):
        return True
    status = c.get("status", "")
    if status.startswith("Up"):
        return c.get("health") == "unhealthy"
    if status.startswith("Exited (0)"):
        return False
    return True


def _has_incomplete_stacks(snapshot: dict) -> bool:
    """True if any active stack has *urgently* missing containers (CRITICAL/HIGH — an
    incident, not a long-known-dormant stack like an intentionally-unstarted pinepods,
    which Leela downgrades to LOW). The 2026-07-03 blind spot: a stack with ZERO
    containers produces no per-container 'not running' findings — there's nothing there
    to flag — so this has to be its own explicit check. A whole-stack outage is exactly
    the situation where pruning is most dangerous: the missing containers' images may be
    the only copies left, undeletable-from-registry custom builds included (see the
    casa/lidarr:local incident)."""
    return any(
        s.get("alert") in ("CRITICAL", "HIGH")
        for s in snapshot.get("stack_completeness", [])
    )


def _safe_to_prune(snapshot: dict) -> bool:
    containers = snapshot.get("containers", [])
    if not containers:
        return False  # no data — don't risk it
    if _has_incomplete_stacks(snapshot):
        return False
    return not any(_container_blocks_prune(c) for c in containers)


def _has_active_rollback_candidates() -> bool:
    """Reserved for the canary auto-update rollout: when an update pulls a new image, the
    previous image ID gets recorded here until its grace period passes, so safe-prune
    won't remove the one thing a rollback would need. File may not exist yet — that's
    fine, it just means nothing is currently pending."""
    if not ROLLBACK_CANDIDATES_FILE.exists():
        return False
    try:
        data = json.loads(ROLLBACK_CANDIDATES_FILE.read_text())
        now = datetime.now(timezone.utc)
        return any(
            datetime.fromisoformat(c["expires_at"]) > now
            for c in data.get("candidates", [])
        )
    except Exception:
        return False


def maybe_run_safe_prune(snapshot: dict, notifier: Notifier) -> None:
    """Prune Docker images/networks when root disk pressure is real AND every container
    is in a known-safe state. Skips entirely (logs why, no Telegram noise) if disk is
    fine, if anything is unhealthy/crash-looping/unrecognized, if a whole stack is
    missing containers, or if an update rollback window is open."""
    disk_alert = _root_disk_alert(snapshot)
    if not disk_alert:
        return
    if _has_incomplete_stacks(snapshot):
        log.warning(
            "Safe-prune skipped: at least one stack is missing containers entirely — "
            "this is more urgent than the disk pressure that would have triggered pruning"
        )
        return
    if not _safe_to_prune(snapshot):
        log.info("Safe-prune skipped: at least one container isn't in a known-safe state")
        return
    if _has_active_rollback_candidates():
        log.info("Safe-prune skipped: an update rollback window is still open")
        return

    log.info(
        f"Root disk at {disk_alert['used_pct']}% and all containers healthy — running safe prune"
    )
    result = bender.run_safe_prune()
    notifier.notify(
        f"🧹 *Safe prune ran automatically*\n"
        f"Root disk was at {disk_alert['used_pct']}% ({disk_alert.get('alert', '')}). "
        f"Every container was running cleanly or a known one-shot job, so this only "
        f"removed images/networks not attached to anything.\n"
        f"{TelegramClient.s(result.get('summary', ''))}"
    )


# ── Pipeline runner ───────────────────────────────────────────────────────────
def run_pipeline(notifier: Notifier, state: PipelineState, mode: str = "full") -> None:
    """
    Full pipeline: Leela → Hermes → Farnsworth → Telegram notification.
    mode: 'full' | 'status' | 'updates'
    """
    if state.state not in (PipelineState.IDLE,):
        notifier.notify("⚠️ Pipeline already running or awaiting approval. Please wait.")
        return

    state.transition(PipelineState.RUNNING)
    try:
        # ── Step 1: Leela scans ──────────────────────────────────────────────
        notifier.notify("👁️ *Leela scanning...*")
        if mode == "status":
            snapshot = leela.run_status()
        elif mode == "updates":
            snapshot = leela.run_updates()
        else:
            snapshot = leela.run_full()
        config.ensure_dirs()
        config.STATE_MONITOR.write_text(json.dumps(snapshot, indent=2))

        if mode == "full":
            try:
                maybe_run_safe_prune(snapshot, notifier)
            except Exception as e:
                log.exception(f"Safe-prune check failed (non-fatal): {e}")

        if mode in ("status", "updates"):
            # Short-circuit — just report, no planning needed
            _send_status_report(notifier, snapshot, mode)
            state.transition(PipelineState.IDLE)
            return

        # ── Step 2: Hermes analyzes ──────────────────────────────────────────
        notifier.notify("📋 *Hermes filing the report...*")
        findings = hermes.analyze(snapshot)
        hermes.save_findings(findings)

        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        report_msg = TelegramClient.fmt_report(date_str, findings.get("findings", []))
        notifier.notify(report_msg)

        if not findings.get("findings"):
            notifier.notify("_No findings. Nothing to plan. Go team!_")
            state.transition(PipelineState.IDLE)
            return

        # ── Step 3: Farnsworth plans ─────────────────────────────────────────
        notifier.notify("🧠 *Good news, everyone! Devising plans...*")
        plans_data = plan(findings)
        save_plans(plans_data)

        if not plans_data.get("plans"):
            notifier.notify("_No actionable plans generated._")
            state.transition(PipelineState.IDLE)
            return

        # ── Step 4: Send plans to Telegram for approval ──────────────────────
        for p in plans_data["plans"]:
            plan_msg = TelegramClient.fmt_plan(p)
            msg_id = notifier.request_approval(plan_msg, p["id"], "plan")
            state.transition(
                PipelineState.AWAITING_APPROVAL,
                plan_id=p["id"],
                msg_id=msg_id,
            )
            # One plan at a time — pause after first, handle others after execution
            break

    except Exception as e:
        log.exception(f"Pipeline error: {e}")
        state.transition(PipelineState.IDLE)
        notifier.notify(f"🛑 *Pipeline error:* `{str(e)[:200]}`")


def _send_status_report(notifier: Notifier, snapshot: dict, mode: str) -> None:
    if mode == "status":
        containers = snapshot.get("containers", [])
        issues = [c for c in containers if c.get("issue")]
        disks   = [d for d in snapshot.get("disk", []) if d.get("alert")]
        services = snapshot.get("services", {})
        down_svcs = [k for k, v in services.items() if v != "active"]

        s = TelegramClient.s
        lines = [f"📊 *Quick Status — {snapshot['timestamp'][:16]}*"]
        lines.append(f"Containers: {len(containers)} total, {len(issues)} issues")
        for c in issues:
            lines.append(f"  \u274c `{c['name']}` - {s(c.get('issue', '?'))}")
        lines.append(f"Disk: {len(disks)} alerts")
        for d in disks:
            lines.append(f"  {d['alert']} `{d['mount']}` {d['used_pct']}%")
        if down_svcs:
            lines.append(f"Services down: {s(', '.join(down_svcs))}")
        notifier.notify("\n".join(lines))

    elif mode == "updates":
        candidates = snapshot.get("image_candidates", [])
        if not candidates:
            notifier.notify("🔵 No stale `:latest` images found.")
        else:
            lines = [f"🔵 *{len(candidates)} stale image(s) found:*"]
            for img in candidates:
                lines.append(
                    f"  `{img['repo']}:{img['tag']}` — {img.get('stale_days', '?')}d old"
                )
            notifier.notify("\n".join(lines))


# ── Telegram command handlers ─────────────────────────────────────────────────
def handle_message(update: dict, tg: TelegramClient, notifier: Notifier, state: PipelineState) -> None:
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != tg.chat_id:
        log.warning(f"Message from unknown chat {chat_id} — ignoring")
        return

    cmd = text.split()[0].lower() if text else ""

    if cmd == "/check":
        threading.Thread(
            target=run_pipeline, args=(notifier, state, "full"), daemon=True
        ).start()

    elif cmd == "/status":
        threading.Thread(
            target=run_pipeline, args=(notifier, state, "status"), daemon=True
        ).start()

    elif cmd == "/updates":
        threading.Thread(
            target=run_pipeline, args=(notifier, state, "updates"), daemon=True
        ).start()

    elif cmd == "/rollback":
        parts = text.split()
        plan_id = parts[1] if len(parts) > 1 else None
        if plan_id:
            _do_rollback(tg, notifier, state, plan_id)
        else:
            notifier.notify("Usage: `/rollback <plan_id>`")

    elif cmd == "/skip":
        parts = text.split()
        plan_id = parts[1] if len(parts) > 1 else None
        notifier.notify(f"⏭️ Skip noted for plan `{plan_id or '?'}`. Manual follow-up required.")
        state.transition(PipelineState.IDLE)

    elif cmd == "/state":
        notifier.notify(f"Current state: `{state.state}`")

    elif cmd == "/patchnow":
        if state.state != PipelineState.IDLE:
            notifier.notify("⚠️ Pipeline busy right now. Please wait and try again.")
        else:
            notifier.notify(
                "🩺 *Zoidberg starting a canary update pass now...*\n"
                "Silent per-service unless something needs a rollback — that'll page you."
            )
            threading.Thread(target=_run_update_pass, args=(tg, notifier), daemon=True).start()

    elif cmd == "/stacks":
        threading.Thread(target=_run_stacks_list, args=(notifier,), daemon=True).start()

    elif cmd == "/mounts":
        threading.Thread(target=_run_mounts_check, args=(notifier,), daemon=True).start()

    elif cmd == "/backups":
        threading.Thread(target=_run_backups_check, args=(notifier,), daemon=True).start()

    elif cmd == "/up":
        parts = text.split()
        target = parts[1].lower() if len(parts) > 1 else None
        if not target:
            notifier.notify("Usage: `/up <stack>` or `/up all`")
        else:
            threading.Thread(target=_run_stack_op, args=(notifier, "up", target), daemon=True).start()

    elif cmd == "/down":
        parts = text.split()
        target = parts[1].lower() if len(parts) > 1 else None
        if not target:
            notifier.notify("Usage: `/down <stack>` or `/down all`")
        else:
            threading.Thread(target=_run_stack_op, args=(notifier, "down", target), daemon=True).start()

    elif cmd == "/help":
        notifier.notify(
            "*Planet Express — Available Commands*\n"
            "/check — Full scan + plan (no execution)\n"
            "/status — Quick health snapshot\n"
            "/updates — Image staleness check\n"
            "/patchnow — Run a canary auto-update pass now (normally weekly)\n"
            "/rollback `<id>` — Roll back a plan\n"
            "/skip `<id>` — Mark plan skipped\n"
            "/state — Current pipeline state\n"
            "/stacks — List stacks\n"
            "/mounts — Verify NAS mounts are reachable\n"
            "/backups — Borg daily/weekly backup status\n"
            "/up `<stack>`|`all` — Bring a stack (or everything) up\n"
            "/down `<stack>`|`all` — Bring a stack (or everything) down"
        )


def handle_callback(update: dict, tg: TelegramClient, notifier: Notifier, state: PipelineState) -> None:
    decision = notifier.interpret_decision(update)
    if decision is None:
        return

    if decision.kind == "plan" and decision.approved:
        notifier.resolve(
            decision, "Good news, everyone! Executing...",
            f"✅ Plan #{decision.request_id} *approved*. Bender is on it.",
        )
        p = load_pending_plan(decision.request_id)
        if not p:
            notifier.notify(f"⚠️ Plan `{decision.request_id}` not found or expired.")
            state.transition(PipelineState.IDLE)
            return
        state.transition(PipelineState.EXECUTING, plan_id=decision.request_id)
        threading.Thread(
            target=_execute_plan, args=(tg, notifier, state, p), daemon=True
        ).start()

    elif decision.kind == "plan" and not decision.approved:
        notifier.resolve(
            decision, "Plan cancelled.",
            f"❌ Plan #{decision.request_id} *cancelled*.",
        )
        state.transition(PipelineState.IDLE)
        log.info(f"Plan {decision.request_id} cancelled by user")

    elif decision.kind == "diff" and decision.approved:
        notifier.resolve(
            decision, "Applying diff...",
            f"✅ Diff `{decision.request_id}` <b>applied</b>.",
        )
        try:
            result = bender.apply_pending_diff(decision.request_id)
            notifier.notify(
                f"📝 Applied. Backup saved at <code>{TelegramClient.s(result['backup_path'])}</code>.\n"
                f"This only wrote the file — nothing has restarted. Run the normal update/restart "
                f"plan (or /check) to pick up the change."
            )
        except bender.SafetyError as e:
            notifier.notify(f"⚠️ Could not apply diff `{decision.request_id}`: {TelegramClient.s(str(e))}")

    elif decision.kind == "diff" and not decision.approved:
        notifier.resolve(
            decision, "Diff discarded.",
            f"❌ Diff `{decision.request_id}` <b>discarded</b>.",
        )
        bender.discard_pending_diff(decision.request_id)
        log.info(f"Diff {decision.request_id} discarded by user")


def _run_stacks_list(notifier: Notifier) -> None:
    forbidden = set(config.FORBIDDEN_STACKS)
    lines = []
    for stack_dir in stackctl.all_stack_dirs():
        tag = " (forbidden)" if stack_dir.name in forbidden else ""
        lines.append(f"{TelegramClient.s(stack_dir.name)}{tag}")
    notifier.notify("*Stacks:*\n" + "\n".join(lines))


def _run_mounts_check(notifier: Notifier) -> None:
    results = stackctl.check_mounts()
    lines = [
        f"{'✅' if ok else '❌'} {TelegramClient.s(unit)} → {TelegramClient.s(path)}"
        for unit, path, ok in results
    ]
    header = "✅ All mounts reachable." if all(ok for _, _, ok in results) else "⚠️ Some mounts unreachable."
    notifier.notify(f"{header}\n" + "\n".join(lines))


def _fmt_backups_message(results: list[dict]) -> str:
    ok = all(r["result"] == "success" for r in results)
    header = "✅ Backups healthy." if ok else "⚠️ A backup job's last run did not succeed."
    lines = []
    for r in results:
        icon = "✅" if r["result"] == "success" else "❌"
        lines.append(
            f"{icon} *{TelegramClient.s(r['label'])}* — {TelegramClient.s(r['result'])} "
            f"(exit {TelegramClient.s(r['exit_status'])})\n"
            f"   last: {TelegramClient.s(r['last_run_at'])}\n"
            f"   next: {TelegramClient.s(r['next_run_at'])}"
        )
    return f"{header}\n" + "\n".join(lines)


def _run_backups_check(notifier: Notifier) -> None:
    results = stackctl.check_backups()
    notifier.notify(_fmt_backups_message(results))


def _run_stack_op(notifier: Notifier, verb: str, target: str) -> None:
    notifier.notify(f"⏳ `{verb}` `{TelegramClient.s(target)}`...")
    fn = stackctl.stack_up if verb == "up" else stackctl.stack_down
    result = fn(target)

    if result.get("refused"):
        notifier.notify(f"🚫 `{TelegramClient.s(target)}` is in FORBIDDEN_STACKS — refusing to start it.")
        return
    if result.get("not_found"):
        notifier.notify(f"⚠️ No stack named `{TelegramClient.s(target)}` found.")
        return

    lines = []
    for name, ok, tail in result["results"]:
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} {TelegramClient.s(name)}")
        if not ok and tail:
            lines.append(f"<code>{TelegramClient.s(tail[:300])}</code>")
    header = "✅ Done." if result["ok"] else "⚠️ One or more stacks failed."
    notifier.notify(f"{header}\n" + "\n".join(lines))


def _investigate_failure(notifier: Notifier, container: str, reason: str) -> None:
    """Escalate to Amy after a plan step or Zoidberg update has already failed once.
    Never executes anything — sends a diagnosis, and a separate diff proposal with
    its own approval if a compose-file edit looks necessary."""
    try:
        label_fmt = shlex.quote(
            '{{index .Config.Labels "com.docker.compose.project"}}\t'
            '{{index .Config.Labels "com.docker.compose.service"}}'
        )
        _, label_out, _ = bender._run_command(f"docker inspect --format {label_fmt} {container}")
        stack_guess, _, service_guess = label_out.strip().partition("\t")
        stack_guess = stack_guess.strip() or "unknown"
        service_guess = service_guess.strip() or container

        current_service_yaml = None
        block = bender.read_service_block(stack_guess, service_guess)
        if block:
            _, current_service_yaml = block

        _, logs_tail, _ = bender._run_command(f"docker logs {container} --tail 100")
        diagnosis = amy.diagnose(
            stack=stack_guess,
            service=service_guess,
            container_name=container,
            reason=reason,
            logs_tail=logs_tail,
            current_service_yaml=current_service_yaml,
        )
    except Exception as e:
        log.exception(f"Amy investigation crashed for {container}: {e}")
        notifier.notify(f"🛑 Amy's investigation of {container} crashed: `{str(e)[:200]}`")
        return

    notifier.notify(TelegramClient.fmt_diagnosis(stack_guess, container, diagnosis))

    remediation = diagnosis.get("proposed_remediation", {})
    if not remediation.get("requires_compose_edit"):
        return

    proposed_yaml = remediation.get("proposed_service_yaml")
    if proposed_yaml and block:
        full_content, current_block = block
        new_content = full_content.replace(current_block, proposed_yaml, 1)
        try:
            diff = bender.propose_compose_diff(
                stack_guess, new_content,
                reason=f"Amy's diagnosis for {container}: {remediation.get('summary', '')}",
            )
            notifier.request_approval(
                TelegramClient.fmt_diff(stack_guess, remediation.get("summary", ""), diff["diff_text"]),
                diff["diff_id"], "diff",
            )
        except bender.SafetyError as e:
            notifier.notify(f"⚠️ Amy proposed a compose edit but it couldn't be turned into a diff: {TelegramClient.s(str(e))}")
    else:
        # No concrete YAML (Amy wasn't given the block, or chose not to propose one) —
        # fall back to the human-readable description only.
        notifier.notify(
            f"📝 Amy says this needs a compose-file edit: "
            f"{TelegramClient.s(remediation.get('compose_edit_description', '(no description given)'))}\n\n"
            f"She didn't have enough to propose an exact diff — that edit still needs to be made "
            f"by hand and proposed through the normal diff-approval flow."
        )


def _execute_plan(tg: TelegramClient, notifier: Notifier, state: PipelineState, plan_data: dict) -> None:
    try:
        result = bender.execute(plan_data, tg)
        if result["final_status"] == "success":
            notifier.notify(TelegramClient.fmt_complete(
                plan_data["id"],
                result["steps_completed"],
                result.get("errors", []),
            ))
        else:
            notifier.notify(
                f"⚠️ Plan #{plan_data['id']} finished with status: `{result['final_status']}`"
            )
            container = plan_data.get("container")
            if container:
                threading.Thread(
                    target=_investigate_failure,
                    args=(notifier, container, f"plan {plan_data['id']} failed: {result.get('errors')}"),
                    daemon=True,
                ).start()
    except Exception as e:
        log.exception(f"Bender execution error: {e}")
        notifier.notify(f"🛑 Bender crashed: `{str(e)[:200]}`")
    finally:
        state.transition(PipelineState.IDLE)


def _do_rollback(tg: TelegramClient, notifier: Notifier, state: PipelineState, plan_id: str) -> None:
    p = load_pending_plan(plan_id)
    if not p or not p.get("rollback"):
        notifier.notify(f"⚠️ No rollback steps found for plan `{plan_id}`.")
        return
    notifier.notify(f"↩️ *Rolling back plan #{plan_id}...*")
    result = bender.execute_rollback(p, tg)
    notifier.notify(
        f"Rollback complete. Steps executed: {result['steps_completed']}. "
        f"Errors: {result.get('errors', [])}"
    )
    state.transition(PipelineState.IDLE)


# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler_loop(notifier: Notifier, state: PipelineState) -> None:
    """Background thread — runs full pipeline every PIPELINE_INTERVAL_HOURS hours."""
    log.info(f"Scheduler started — pipeline runs every {PIPELINE_INTERVAL_HOURS}h")
    time.sleep(60)  # brief delay on startup before first scheduled run
    while True:
        try:
            log.info("Scheduled pipeline run starting")
            run_pipeline(notifier, state, mode="full")
        except Exception as e:
            log.exception(f"Scheduled pipeline error: {e}")
        time.sleep(PIPELINE_INTERVAL_HOURS * 3600)


def _run_update_pass(tg: TelegramClient, notifier: Notifier) -> None:
    """Zoidberg's canary update pass. Deliberately doesn't touch PipelineState's own
    machinery — it's silent-on-success by design, Telegram only speaks up on rollback,
    so there's no "plan awaiting approval" step for the routine case."""
    try:
        zoidberg.run_update_pass(tg=tg)
    except Exception as e:
        log.exception(f"Zoidberg update pass crashed: {e}")
        notifier.notify(f"🛑 *Zoidberg update pass crashed:* `{str(e)[:200]}`")


def _seconds_until_next_update_window() -> float:
    now = datetime.now()
    days_ahead = (UPDATE_DAY_OF_WEEK - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=UPDATE_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def update_scheduler_loop(tg: TelegramClient, state: PipelineState) -> None:
    """Background thread — runs Zoidberg's canary update pass weekly. Separate cadence
    from the 6h monitor cycle on purpose: pulling/restarting every service every 6h would
    be excessive churn for something that's supposed to be routine maintenance."""
    log.info(
        f"Update scheduler started — canary updates run weekly "
        f"(day {UPDATE_DAY_OF_WEEK}, {UPDATE_HOUR}:00)"
    )
    while True:
        delay = _seconds_until_next_update_window()
        log.info(f"Next canary update pass in {delay / 3600:.1f}h")
        time.sleep(delay)
        if state.state != PipelineState.IDLE:
            log.warning("Skipping scheduled update pass: pipeline busy, will retry next week")
        else:
            try:
                log.info("Scheduled canary update pass starting")
                zoidberg.run_update_pass(tg=tg)
            except Exception as e:
                log.exception(f"Scheduled update pass error: {e}")
        time.sleep(3600)  # clear the target window before recomputing next week's delay


def _seconds_until_next_digest() -> float:
    now = datetime.now()
    target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def digest_scheduler_loop(notifier: Notifier) -> None:
    """Background thread — sends a daily backup-status digest every morning at
    DIGEST_HOUR. Pure reporting, no approval gate and no PipelineState interaction: same
    read-only status /backups returns on demand, just delivered proactively so a failed
    backup doesn't sit silent until someone thinks to ask."""
    log.info(f"Digest scheduler started — daily backup report at {DIGEST_HOUR}:00")
    while True:
        delay = _seconds_until_next_digest()
        log.info(f"Next backup digest in {delay / 3600:.1f}h")
        time.sleep(delay)
        try:
            results = stackctl.check_backups()
            notifier.notify("*Morning backup report*\n" + _fmt_backups_message(results))
        except Exception as e:
            log.exception(f"Digest error: {e}")
        time.sleep(60)  # clear the target minute before recomputing next day's delay


# ── Main bot loop ─────────────────────────────────────────────────────────────
def run_bot() -> None:
    config.ensure_dirs()
    token, chat_id = config.telegram_credentials()
    tg = TelegramClient(token, chat_id)
    notifier: Notifier = TelegramNotifier(tg)
    state = PipelineState()

    log.info("Good news, everyone! Professor Farnsworth is online.")
    notifier.notify("🚀 <b>Planet Express is online!</b>\nFarnsworth reporting for duty. Send /help for commands.")

    # Start background schedulers
    sched = threading.Thread(
        target=scheduler_loop, args=(notifier, state), daemon=True, name="scheduler"
    )
    sched.start()

    update_sched = threading.Thread(
        target=update_scheduler_loop, args=(tg, state), daemon=True, name="update-scheduler"
    )
    update_sched.start()

    digest_sched = threading.Thread(
        target=digest_scheduler_loop, args=(notifier,), daemon=True, name="digest-scheduler"
    )
    digest_sched.start()

    # Main Telegram poll loop
    log.info("Farnsworth entering Telegram poll loop...")
    while True:
        try:
            updates = tg.poll_updates(timeout=30)
            for update in updates:
                try:
                    if "message" in update:
                        handle_message(update, tg, notifier, state)
                    elif "callback_query" in update:
                        handle_callback(update, tg, notifier, state)
                except Exception as e:
                    log.exception(f"Update handler error: {e}")
        except KeyboardInterrupt:
            log.info("Farnsworth shutting down. Goodbye!")
            notifier.notify("🛑 <b>Planet Express going offline.</b>")
            break
        except Exception as e:
            log.exception(f"Poll loop error: {e}")
            time.sleep(5)  # brief backoff on unexpected errors


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log_level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                config.LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
            ) if config.LOG_DIR.exists() else logging.StreamHandler(sys.stdout),
        ],
    )

    parser = argparse.ArgumentParser(description="Farnsworth — Planet Express orchestrator")
    parser.add_argument("--plan", action="store_true", help="Plan-only: read findings, print plans, exit")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.plan:
        # Standalone planning mode for testing
        if not config.STATE_FINDINGS.exists():
            print("No findings file found. Run casa_hermes.py first.", file=sys.stderr)
            sys.exit(1)
        findings = json.loads(config.STATE_FINDINGS.read_text())
        plans = plan(findings)
        print(json.dumps(plans, indent=2))
    else:
        config.ensure_dirs()
        run_bot()
