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
import re
import shlex
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import casa_amy as amy
import casa_bender as bender
import casa_fry as fry
import casa_hermes as hermes
import casa_leela as leela
import casa_llm as llm
import casa_stackctl as stackctl
import casa_zoidberg as zoidberg
import config
from notifier import Notifier, TelegramNotifier
from state_models import MonitorSnapshot, PlanSet, RunStatus
from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.farnsworth")

# ── Config ────────────────────────────────────────────────────────────────────
PIPELINE_INTERVAL_HOURS = 6
PLAN_EXPIRY_HOURS = 24
MAX_TOKENS = 8192  # 4096 truncates mid-JSON with 33 findings (~13k chars output)

# /install only ever writes a LAN-only Traefik router (no auth of its own) — restricted to
# this deployment's own LAN-only domain convention (config.LAN_ONLY_DOMAIN, defaulting to
# this host's "casalan.com") so a mistyped or malicious domain can't silently expose a
# brand-new, unreviewed container to the public internet.
LAN_ONLY_DOMAIN = config.LAN_ONLY_DOMAIN

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
- For a vpn_port_forwarding finding: CASA_GSP and CASA_QBIT both run with
  `network_mode: container:CASA_GLUETON` (CASA_GSP in stacks/network/docker-compose.yml,
  CASA_QBIT in stacks/media/docker-compose.yml — two different compose files) — they share
  gluetun's network namespace rather than having their own. Restarting CASA_GLUETON gives
  it a brand new namespace; CASA_GSP and CASA_QBIT are NOT automatically attached to the
  new one and stay silently orphaned on the old, torn-down namespace until they are
  themselves restarted.
  If the issue is "gluetun reports no forwarded port" (the forward itself is dead, e.g.
  gluetun_port is 0/missing), a plan that restarts only CASA_GLUETON does not fix this
  finding — it must restart all three, in this order, with a wait between each: (1) restart
  CASA_GLUETON, (2) sleep ~20s for the wireguard tunnel and port-forward RPC to
  re-establish, (3) restart CASA_GSP, (4) restart CASA_QBIT.
  If instead gluetun reports a valid nonzero forwarded port but it just doesn't match
  qBittorrent's configured port (a GSP sync mismatch, not a dead forward), do NOT restart
  CASA_GLUETON — its own forward is fine and restarting it is needless disruption. Only
  restart CASA_GSP, then CASA_QBIT.
  Either way, the final verification step must be a single command that actually FAILS
  (non-zero exit) if the sync didn't take — Bender only checks each step's exit code, it
  does not read or judge `expected_output`, so a verification step that merely prints
  something and exits 0 regardless proves nothing. Each step also runs as its own separate
  shell invocation (Bender does not keep a shell alive across steps), so a variable set in
  one step is not visible in the next — the whole check has to be one self-contained
  command. Use exactly this pattern:
  `SINCE=$(docker inspect --format '{{.State.StartedAt}}' CASA_GSP) && PORT=$(docker exec
  CASA_QBIT grep -F 'Session\\Port=' /config/qBittorrent/qBittorrent.conf | cut -d= -f2 |
  tr -d '\\r') && [ -n "$PORT" ] && docker logs CASA_GSP --since "$SINCE" | grep -F
  "New : $PORT"`
  — never `cat` the whole qBittorrent config file, it also contains qBittorrent's WebUI
  username and password hash, which Bender would otherwise persist into its plan execution
  log. Two details matter here and must not be dropped if you rephrase this: (a) the
  explicit `[ -n "$PORT" ]` check — without it, a failed `docker exec`/grep/cut just
  produces an empty $PORT rather than a non-zero exit from that whole pipe segment, and
  `grep -F "New : "` (empty port) would then match almost any GSP log line and falsely
  report success; (b) `--since "$SINCE"` (GSP's own last-start time from `docker inspect`),
  not `--tail N` — `--tail` can still match a stale "New : <old port>" line from before this
  restart even though the sync never actually happened this time. Never construct your own
  command to query gluetun's control API directly (e.g. curl against :8000/v1/portforward)
  — it requires the X-Api-Key header read from network/.env, and any command containing
  that key would get persisted into Bender's plan execution log and posted to Telegram for
  approval. GSP's own log already reports the comparison gluetun made internally, so
  there's no need to re-query gluetun directly.
  If instead the issue indicates the *check itself* couldn't run: a missing
  GSP_GTN_API_KEY is read from the host's network/.env before any container is even
  contacted, so no restart can ever fix it — always make a diagnostic-only plan (or no
  plan) noting in the title that it needs human investigation into that .env file.
  Exception: if the error text itself shows an auth failure talking to gluetun's control
  server (401, 403, "Unauthorized", "Forbidden") rather than a connection failure, that's
  a bad/stale GSP_GTN_API_KEY, not a dead container — a restart cannot fix it and you're
  NEVER allowed to touch the .env file yourself, so make a diagnostic-only plan (or no
  plan) instead, same as the missing-key case above.
  For gluetun's control server being otherwise unreachable (connection refused/timeout,
  not an auth error) or qBittorrent's config being unreadable: Bender executes a plan as
  a fixed, linear list of steps and stops on the first failure — it has no way to run one
  step conditionally on another step's output, so a plan itself can never branch at
  execution time. But if a "Diagnostic findings gathered before planning" section appears
  below, that state was already checked *before* you write this plan, so use it: if it
  shows CASA_GLUETON is actually healthy and only CASA_GSP/CASA_QBIT are stale, restart
  just those two. If no diagnostic section appears, or it doesn't clarify container health,
  fall back to the full three-container restart sequence as the dead-port case above
  (restart CASA_GLUETON, sleep ~20s, restart CASA_GSP, restart CASA_QBIT) — `docker
  restart` is safe to run against a container whether it's already stopped or already
  running, so the full sequence is always a safe default when you don't have real state
  to narrow it from.
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
  `systemctl restart/start/stop casa-stacks.service` (must be prefixed with `sudo`); and
  `systemctl start/stop` on units matching `*.mount` (must be prefixed with `sudo`).
  Any other privileged command — a different systemd service, a `*.automount` unit
  (note: this is a different unit type than `*.mount` and is NOT covered by the mount
  grant), raw `mount`/`umount`/`mount -a`, `systemctl reset-failed`, fstab edits, disk/
  partition tools, anything else needing root — will fail with "Interactive
  authentication required" or "a password is required" since Bender has no other
  passwordless grant and cannot type one interactively. Do NOT propose any such action.
  Make a diagnostic-only plan (or no plan) instead, and note in the title that it needs
  human action.
- Read-only inspection commands (`systemctl status`, `systemctl is-active`, `journalctl`) never
  need `sudo` and must NEVER be given it — the sudo allowlist above only ever covers
  `start|stop|restart`, so a `sudo systemctl status ...` step is not a valid grant, gets blocked
  by Bender's sudo-scope check every time, and stalls the whole plan on step 1 before the actual
  restart step ever runs. Only prefix `sudo` on the actual start/stop/restart action step.
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

# ── Diagnostic pre-check (runs before PLAN_SYSTEM_PROMPT) ─────────────────────
# Farnsworth's plan is a single-shot, tool-free completion (casa_llm.complete) —
# it never gets to look at anything beyond what's already in the findings JSON,
# which is exactly why PLAN_SYSTEM_PROMPT above has to tell it to guess
# worst-case (e.g. always restart all three VPN containers) rather than check
# state first. This pre-check gives it a bounded, read-only tool loop (routed
# through bender.run_diagnostic — never a direct shell) to actually check real
# state before that prompt runs, so the plan it writes can be the minimal fix
# instead of the safe-but-padded guess. Never blocks planning: any failure here
# just means planning proceeds without the extra context, same as before this
# existed.
MAX_DIAGNOSTIC_ROUNDS = 5
DIAGNOSTIC_MAX_TOKENS = 1024

DIAGNOSTIC_SYSTEM_PROMPT = """You are Professor Farnsworth's diagnostic pre-check, run before any plan is written.

Given the findings below, decide whether checking real system state would change what a
plan should do (e.g. "is the port-forward actually dead, or just unsynced" — restarting
three containers is very different from restarting one). If so, call run_diagnostic with
ONE read-only shell command per call: docker inspect, docker logs, docker ps, journalctl,
systemctl status/is-active/is-enabled, or df. `docker inspect` MUST use --format targeting
one of a small set of known-safe fields (State.*, Name, Id, Created, RestartCount, Image,
or NetworkSettings.Networks.<net>.IPAddress/Gateway/MacAddress, e.g. --format
'{{.State.Status}}') — bare `docker inspect` and any other field (including anything under
Config, which holds Env and can contain secrets) are rejected. `docker logs` MUST use
--tail <N> and journalctl MUST use -n/--lines <N>, with N a positive integer (not "all"),
to bound output; neither may use a follow mode (-f/--follow), since it never terminates.
You may call it up to 5 times total across this conversation. Skip diagnostics entirely for findings where the right fix is already
unambiguous from the finding text alone (e.g. "root disk >90% full" needs no state check
to know what to look at next) — most findings need none.

When you're done gathering state (or decide none is needed), reply with plain text only —
a short summary (2-5 sentences) of what you found and how it should narrow the plan, or
exactly "No diagnostics needed." Do not write the plan itself here; a separate step does
that."""

DIAGNOSTIC_TOOL_SCHEMA = {
    "name": "run_diagnostic",
    "description": (
        "Run one read-only diagnostic command to check real system state before a plan "
        "is written. Never mutates anything — rejected if it isn't docker inspect/logs/ps, "
        "journalctl, systemctl status/is-active/is-enabled, or df. docker inspect must use "
        "--format targeting one of a small set of known-safe fields (bare inspect and "
        "anything under Config, including Env, are rejected). docker logs must use --tail "
        "and journalctl must use -n/--lines; neither may use a follow mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    'The exact shell command to run, e.g. \'docker inspect --format '
                    "\"{{.State.Status}}\" CASA_GLUETON'"
                ),
            },
        },
        "required": ["command"],
    },
}


def _run_diagnostic_tool(command: str) -> str:
    """Execute one diagnostic tool call via Bender's read-only allowlist. Never
    raises — a rejected or failed command becomes visible tool output (so the
    model can adjust) instead of crashing the planning pipeline."""
    try:
        exit_code, stdout, stderr = bender.run_diagnostic(command)
    except bender.SafetyError as e:
        return f"REJECTED: {e}"
    return json.dumps({"exit_code": exit_code, "stdout": stdout[:2000], "stderr": stderr[:1000]})


def _diagnostic_tool_output(command: str, calls_made: int) -> tuple[str, int]:
    """Run one diagnostic tool call unless the shared MAX_DIAGNOSTIC_ROUNDS budget
    is already spent. A single model turn can request several tool calls at once
    (Anthropic can emit multiple tool_use blocks, OpenAI multiple function_calls in
    one response), so the budget has to be enforced per call actually executed --
    not per round -- or a single over-eager turn could run far more than 5
    commands. Returns (tool_output, new_calls_made)."""
    if calls_made >= MAX_DIAGNOSTIC_ROUNDS:
        return (
            f"REJECTED: diagnostic call budget exhausted "
            f"(max {MAX_DIAGNOSTIC_ROUNDS} calls per plan)",
            calls_made,
        )
    return _run_diagnostic_tool(command), calls_made + 1


def _gather_diagnostics_anthropic(findings_json: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key())
    messages = [{"role": "user", "content": f"Findings:\n{findings_json}"}]
    calls_made = 0
    for _ in range(MAX_DIAGNOSTIC_ROUNDS):
        response = client.messages.create(
            model=config.model_for("small"),
            max_tokens=DIAGNOSTIC_MAX_TOKENS,
            system=DIAGNOSTIC_SYSTEM_PROMPT,
            tools=[DIAGNOSTIC_TOOL_SCHEMA],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next((b.text for b in response.content if b.type == "text"), "").strip()
        tool_results = []
        for tu in tool_uses:
            output, calls_made = _diagnostic_tool_output(tu.input.get("command", ""), calls_made)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})
        messages.append({"role": "user", "content": tool_results})
        if calls_made >= MAX_DIAGNOSTIC_ROUNDS:
            # Budget's spent, but the last round's results are still sitting in
            # `messages` unread -- one more completion with no tools attached (so
            # it can't ask for more) lets the model actually summarize what it
            # gathered, instead of throwing that state away.
            final = client.messages.create(
                model=config.model_for("small"),
                max_tokens=DIAGNOSTIC_MAX_TOKENS,
                system=DIAGNOSTIC_SYSTEM_PROMPT,
                messages=messages,
            )
            return next((b.text for b in final.content if b.type == "text"), "").strip()
    return "Diagnostic round limit reached before the model finished."


def _gather_diagnostics_openai(findings_json: str) -> str:
    import openai

    client = openai.OpenAI(api_key=config.openai_api_key())
    tool_schema = {
        "type": "function",
        "name": "run_diagnostic",
        "description": DIAGNOSTIC_TOOL_SCHEMA["description"],
        "parameters": DIAGNOSTIC_TOOL_SCHEMA["input_schema"],
    }
    input_items: list = [{"role": "user", "content": f"Findings:\n{findings_json}"}]
    calls_made = 0
    for _ in range(MAX_DIAGNOSTIC_ROUNDS):
        response = client.responses.create(
            model=config.model_for("small"),
            reasoning={"effort": "low"},
            max_output_tokens=DIAGNOSTIC_MAX_TOKENS,
            tools=[tool_schema],
            input=[{"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT}] + input_items,
        )
        function_calls = [item for item in response.output if item.type == "function_call"]
        if not function_calls:
            return (response.output_text or "").strip()
        input_items.extend(response.output)
        for call in function_calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            output, calls_made = _diagnostic_tool_output(args.get("command", ""), calls_made)
            input_items.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": output,
            })
        if calls_made >= MAX_DIAGNOSTIC_ROUNDS:
            # Budget's spent, but the last round's results are still sitting in
            # `input_items` unread -- one more completion with no tools attached
            # (so it can't ask for more) lets the model actually summarize what it
            # gathered, instead of throwing that state away.
            final = client.responses.create(
                model=config.model_for("small"),
                reasoning={"effort": "low"},
                max_output_tokens=DIAGNOSTIC_MAX_TOKENS,
                input=[{"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT}] + input_items,
            )
            return (final.output_text or "").strip()
    return "Diagnostic round limit reached before the model finished."


def _gather_diagnostics(findings_json: str) -> str:
    """Best-effort pre-planning diagnostic pass. Any failure (API error, bad tool
    call, unsupported provider) just means planning proceeds without the extra
    context — same behavior as before this existed."""
    try:
        if config.LLM_PROVIDER == "anthropic":
            return _gather_diagnostics_anthropic(findings_json)
        if config.LLM_PROVIDER == "openai":
            return _gather_diagnostics_openai(findings_json)
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning(f"Diagnostic pre-check failed, planning without it: {e}")
        return ""


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
            if new_state == self.IDLE:
                # Returning to idle always means no plan is pending anymore -- callers
                # that forget to clear these (e.g. /skip, error paths) previously left
                # a stale pending_plan_id behind, which showed up as a dashboard pill
                # for a plan that was no longer awaiting approval.
                self._pending_plan_id = None
                self._pending_msg_id = None
            else:
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
            status = RunStatus(
                state=self._state,
                pending_plan_id=self._pending_plan_id,
                pending_msg_id=self._pending_msg_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            config.STATE_STATUS.write_text(status.model_dump_json(indent=2))
        except Exception as e:  # noqa: BLE001
            log.warning(f"State persist failed: {e}")


# ── Planning logic ────────────────────────────────────────────────────────────
def _plan_batch(
    finding_list: list,
    parse_errors: list,
    update_candidates: list,
    diagnostics: str | None = None,
) -> list:
    """Ask the LLM for plans covering finding_list. A large or unusually complex
    batch of findings (e.g. a whole-host outage reported as one CRITICAL finding
    per stack) can produce a plan response that overruns MAX_TOKENS and gets cut
    off mid-JSON — confirmed on 2026-07-31, where 9 findings truncated even at
    8192 tokens and Farnsworth silently devised 0 plans right when the incident
    most needed one. Rather than keep raising the token ceiling for whatever the
    next incident's finding count turns out to be, split the batch in half and
    retry each half on a parse failure — this scales to any finding count instead
    of failing again the next time a bigger incident exceeds the last ceiling.

    update_candidates is passed through unsplit on every sub-batch call — PLAN_SYSTEM_PROMPT
    tells the LLM to skip anything in update_candidates (Zoidberg's territory), and it needs
    that list every time to honor the rule, not just on the first, unsplit attempt.

    diagnostics is gathered once for the whole (unsplit) batch and threaded through
    every recursive sub-batch call unchanged, rather than re-gathered per sub-batch --
    otherwise a single big-incident parse failure could fan out into a fresh
    MAX_DIAGNOSTIC_ROUNDS budget (and MAX_DIAGNOSTIC_ROUNDS more diagnostic commands
    run against the host) for every half-split, recursively.
    """
    findings_json = json.dumps(
        {"findings": finding_list, "update_candidates": update_candidates},
        separators=(",", ":"),
    )
    log.info(f"Farnsworth devising plan for {len(finding_list)} finding(s)...")

    if diagnostics is None:
        diagnostics = _gather_diagnostics(findings_json)

    user_content = f"Devise action plans for these findings:\n{findings_json}"
    if diagnostics and diagnostics.strip().lower() != "no diagnostics needed.":
        log.info(f"Farnsworth's diagnostic pre-check: {diagnostics}")
        user_content += f"\n\nDiagnostic findings gathered before planning:\n{diagnostics}"

    raw = llm.complete(
        PLAN_SYSTEM_PROMPT,
        user_content,
        MAX_TOKENS,
        tier="small",
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw).get("plans", [])
    except json.JSONDecodeError as e:
        if len(finding_list) > 1:
            mid = len(finding_list) // 2
            log.warning(
                f"Farnsworth's plan response for {len(finding_list)} finding(s) didn't "
                f"parse ({e}); splitting into two batches of {mid} and {len(finding_list) - mid} and retrying."
            )
            return _plan_batch(
                finding_list[:mid], parse_errors, update_candidates, diagnostics
            ) + _plan_batch(finding_list[mid:], parse_errors, update_candidates, diagnostics)
        log.error(f"Farnsworth got invalid JSON from the LLM for a single finding: {e}")
        parse_errors.append(str(e))
        return []


def plan(findings: dict) -> dict:
    """Good news, everyone — Farnsworth has a plan."""
    result = {
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "plans": [],
    }
    if not findings.get("findings"):
        return result

    parse_errors: list = []
    plans = _plan_batch(
        findings["findings"], parse_errors, findings.get("update_candidates", [])
    )
    # Each split retry is an independent LLM call, so a plan's "id" (e.g. "p1") is
    # only unique within its own sub-batch — concatenating sub-batches can produce
    # duplicate ids, which would break the id-based lookups downstream
    # (load_pending_plan(), /rollback <id>, dashboard_data.summarize_pending_plan()).
    # Renumber once, here, after all sub-batches are merged.
    for i, p in enumerate(plans, start=1):
        p["id"] = f"p{i}"
    result["plans"] = plans
    if parse_errors:
        result["_parse_errors"] = parse_errors
    log.info(f"Farnsworth devised {len(result['plans'])} plan(s)")
    return result


def save_plans(plans: dict) -> None:
    config.ensure_dirs()
    # Add expiry timestamp
    plans["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=PLAN_EXPIRY_HOURS)
    ).isoformat()
    config.STATE_PLAN.write_text(PlanSet(**plans).model_dump_json(indent=2))


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
    except Exception as e:  # noqa: BLE001
        log.warning(f"Failed to load pending plan: {e}")
    return None


# ── Safe prune (space pressure + stack health gated) ──────────────────────────
# Root disk filling up from Docker image/layer buildup was a recurring real problem
# (monthly or worse) before this existed. Auto-runs, never needs approval — by
# construction it only removes images/networks Docker itself considers unused by any
# container, running or stopped, so nothing currently in service is ever at risk.
DISK_PRUNE_THRESHOLD_PCT = 80
ROLLBACK_CANDIDATES_FILE = config.ROLLBACK_CANDIDATES_FILE


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
    return not status.startswith("Exited (0)")


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
    except Exception:  # noqa: BLE001
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
        config.STATE_MONITOR.write_text(MonitorSnapshot(**snapshot).model_dump_json(indent=2))

        if mode == "full":
            try:
                maybe_run_safe_prune(snapshot, notifier)
            except Exception:
                log.exception("Safe-prune check failed (non-fatal)")

        if mode in ("status", "updates"):
            # Short-circuit — just report, no planning needed
            _send_status_report(notifier, snapshot, mode)
            state.transition(PipelineState.IDLE)
            return

        # ── Step 2: Hermes analyzes ──────────────────────────────────────────
        notifier.notify("📋 *Hermes filing the report...*")
        findings = hermes.analyze(snapshot)
        hermes.save_findings(findings)

        date_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
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
        log.exception("Pipeline error")
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

    elif cmd == "/install":
        parts = text.split()
        url = parts[1] if len(parts) > 1 else None
        domain = parts[2] if len(parts) > 2 else None
        if not url or not domain:
            notifier.notify("Usage: `/install <url> <domain>`")
        elif (
            len(domain) > 253
            or not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", domain, re.IGNORECASE)
        ):
            # domain gets interpolated directly into a Traefik Host(`...`) label — reject
            # anything that isn't a plain, single well-formed hostname up front (per-label
            # length capped at 63 per DNS, overall length capped at 253), so a crafted
            # value (backticks/parens/pipes, or a too-long label that'd never resolve)
            # can't inject extra Traefik rule syntax or produce an unreachable router.
            notifier.notify("⚠️ That doesn't look like a single valid hostname.")
        elif not (domain.lower() == LAN_ONLY_DOMAIN or domain.lower().endswith("." + LAN_ONLY_DOMAIN)):
            # This installer only ever writes LAN-only Traefik routers (no auth/hardening
            # of its own) — restricting it to this host's established LAN-only domain
            # convention prevents a typo'd or malicious domain from silently exposing a
            # brand-new, unreviewed container to the public internet through Traefik.
            notifier.notify(
                f"⚠️ `/install` only supports `*.{LAN_ONLY_DOMAIN}` (this host's LAN-only "
                f"domain convention). Public-facing installs need to be done by hand."
            )
        else:
            # Capped at 20 chars: stack_name ends up embedded in a Telegram inline-button
            # callback_data ("approve_diff:diff-<stack_name>-<timestamp>"), which Telegram
            # rejects outright past 64 bytes (BUTTON_DATA_INVALID) — a long DNS label would
            # silently make the resulting diff impossible to approve.
            stack_name = (re.sub(r"[^a-z0-9-]", "", domain.split(".")[0].lower()) or "newstack")[:20]
            threading.Thread(
                target=_run_install, args=(notifier, stack_name, url, domain), daemon=True
            ).start()

    elif cmd == "/grant":
        # Fixed how-to steps are sent as their own message, ahead of the
        # variable-length allowlist/block dump below — Telegram silently
        # truncates at 4096 chars (TelegramClient.send()), and a long
        # allowlist or blocked command must never be able to push the actual
        # instructions out of the message.
        install_dir = Path(__file__).resolve().parent
        notifier.notify(
            f"*How to widen sudo scope*\n"
            f"Bender only ever runs `sudo systemctl start|stop|restart <unit>` "
            f"and only if it's declared in the allowlist below — nothing else, "
            f"and nothing wider can be granted from chat.\n"
            f"1. Edit `{config.CONFIG_FILE}` → `sudo_allowlist:` — add a `units:` "
            f"entry (`unit:`, `actions: [start, stop, restart]`) or a `globs:` "
            f"entry (e.g. `glob: \"*.timer\"`).\n"
            f"2. On the host, from any directory:\n"
            f"`cd {shlex.quote(str(install_dir))} && "
            f"CASA_CONFIG={shlex.quote(str(config.CONFIG_FILE))} "
            f"venv/bin/python scripts/setup_wizard.py`\n"
            f"(the `cd` and CASA_CONFIG pin matter so it reconciles against the "
            f"same install and same file I'm actually running with, not whatever "
            f"defaults apply from your login shell's cwd — mine are set via the "
            f"systemd unit, not your shell). It reuses the existing config and always "
            f"calls `reconcile_sudoers()`, which generates the matching sudoers.d "
            f"grant (expanding any glob to exact discovered units, not a literal "
            f"wildcard) and validates it with `visudo` before installing. Don't "
            f"hand-edit `/etc/sudoers.d/planetexpress` directly — a literal glob "
            f"there can match more than intended since sudoers wildcards cross "
            f"whitespace.\n"
            f"3. `sudo systemctl restart casa-planetexpress.service` — I only read "
            f"sudo_allowlist at process startup, so I'll keep blocking any newly "
            f"granted command until restarted, even after steps 1–2."
        )

        lines = ["*Current sudo allowlist scope*"]
        for u in config.SUDO_ALLOWLIST.units:
            lines.append(f"• `{u.unit}` — {', '.join(u.actions)}")
        for g in config.SUDO_ALLOWLIST.globs:
            lines.append(f"• `{g.glob}` (glob) — {', '.join(g.actions)}")
        if not config.SUDO_ALLOWLIST.units and not config.SUDO_ALLOWLIST.globs:
            lines.append("_nothing declared_")

        if config.LAST_SUDO_BLOCK_FILE.exists():
            try:
                block = json.loads(config.LAST_SUDO_BLOCK_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                block = None
            if block and block.get("unit") and block.get("action") and bender._sudo_action_allowed(
                block["unit"], block["action"]
            ):
                # Already granted (config edited + service restarted since the
                # block) — the recorded block is stale, so drop it rather than
                # keep telling the operator to add something that's now already
                # in scope.
                config.LAST_SUDO_BLOCK_FILE.unlink(missing_ok=True)
                block = None
            if block:
                lines.append(f"\n*Most recent block* (plan #{block['plan_id']} step {block['step']}):")
                lines.append(f"`{TelegramClient.s(block['command'])}`")
                if block.get("unit") and block.get("action"):
                    lines.append(
                        "To grant exactly this, add to config.yaml's "
                        "`sudo_allowlist.units`:\n"
                        f"```\n- unit: {block['unit']}\n  actions: [{block['action']}]\n```"
                    )
                else:
                    lines.append(
                        "This wasn't a `sudo systemctl start|stop|restart <unit>` "
                        "call, so it can never be granted through this project's sudo "
                        "mechanism — that's the only shape it will ever run as root, "
                        "no config change can widen that."
                    )

        notifier.notify("\n".join(lines))

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
            "/down `<stack>`|`all` — Bring a stack (or everything) down\n"
            "/install `<url>` `<domain>` — Fry resolves a project URL, proposes a new stack (diff-approve)\n"
            "/grant — Show current sudo allowlist scope + how to widen it"
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
            if result["backup_path"]:
                backup_line = f"Backup saved at <code>{TelegramClient.s(result['backup_path'])}</code>."
            else:
                backup_line = "New file — no prior version to back up."
            notifier.notify(
                f"📝 Applied. {backup_line}\n"
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


def _investigate_failure(
    notifier: Notifier, container: str, reason: str, failed_step_detail: str | None = None
) -> None:
    """Escalate to Amy after a plan step or Zoidberg update has already failed once.
    Never executes anything — sends a diagnosis, and a separate diff proposal with
    its own approval if a compose-file edit looks necessary.

    failed_step_detail, when the caller has it, is the actual failing plan step's own
    command + stdout/stderr — a generic `docker logs` tail of the container's app
    process often shows nothing wrong (e.g. a health check probe vs. an exec-based
    mount check), so without this Amy is stuck diagnosing blind."""
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

        _, container_logs, _ = bender._run_command(f"docker logs {container} --tail 100")
        logs_tail = container_logs
        if failed_step_detail:
            # amy.diagnose() bounds the prompt with logs_tail[-4000:], so the failing
            # step's own output — the most load-bearing evidence here — goes last and
            # the (less reliable) container logs are capped up front, or a long docker
            # logs tail would push the failed-step detail out of the window entirely.
            logs_tail = (
                f"[Container's own docker logs tail — may look clean if the failure "
                f"was in an exec/probe step rather than the app process]\n{container_logs[-2000:]}\n\n"
                f"[Output of the failing plan step itself]\n{failed_step_detail}"
            )
        diagnosis = amy.diagnose(
            stack=stack_guess,
            service=service_guess,
            container_name=container,
            reason=reason,
            logs_tail=logs_tail,
            current_service_yaml=current_service_yaml,
        )
    except Exception as e:
        log.exception(f"Amy investigation crashed for {container}")
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


def _run_install(notifier: Notifier, stack_name: str, url: str, domain: str) -> None:
    """Onboard a new stack from a URL. Fry resolves the project's real deployment
    requirements; this function synthesizes a standalone compose file matching the
    Navidrome precedent (container_name CASA_<NAME>, casaproxy network, LAN-only
    Traefik router) and proposes it through Bender's existing diff-approve flow.
    Never writes anything itself — same "human approves the diff" contract as
    _investigate_failure."""
    s = TelegramClient.s
    try:
        req = fry.onboard(url, stack_name, domain)
    except Exception as e:
        log.exception(f"Fry's resolution crashed for {url}")
        notifier.notify(f"🛑 Fry's resolution of {s(url)} crashed: `{str(e)[:200]}`")
        return

    try:
        _process_fry_resolution(notifier, stack_name, url, domain, req)
    except Exception as e:
        # Fry's output is model-generated JSON — even though it matched the schema well
        # enough to parse, a field can still be the wrong shape (e.g. a string where a
        # list of dicts was expected). This runs in a daemon thread with no other
        # handler above it, so without this the install would fail completely silently.
        log.exception(f"Processing Fry's resolution crashed for {stack_name}")
        notifier.notify(
            f"🛑 Processing Fry's resolution for {s(stack_name)} crashed: `{str(e)[:200]}`. "
            f"This needs to be onboarded by hand."
        )


def _process_fry_resolution(notifier: Notifier, stack_name: str, url: str, domain: str, req: dict) -> None:
    s = TelegramClient.s

    # Fry's JSON is model-generated, not schema-enforced. Validate the shape of every
    # collection field up front, before anything below assumes it — a wrong-but-truthy
    # type (e.g. a string where a list of dicts is expected) would otherwise crash
    # outright (iterating characters, calling .get() on a non-dict) or, further down,
    # silently produce a malformed/unsafe compose proposal.
    ports = req.get("ports") or []
    volumes = req.get("volumes") or []
    env_vars = req.get("required_env") or []
    if (
        not isinstance(ports, list) or not all(isinstance(p, dict) for p in ports)
        or not isinstance(volumes, list)
        or not isinstance(env_vars, list) or not all(isinstance(e, dict) for e in env_vars)
    ):
        notifier.notify(
            "⚠️ Fry's response has the wrong shape for ports/volumes/required_env (expected "
            "lists, with ports/required_env entries as objects) — refusing to treat this as "
            "usable structured data. This needs to be onboarded by hand."
        )
        return

    # Checked before the summary is built below: every guardrail field interpolated
    # there is a raw model-generated value with no HTML-escaping (they're supposed to be
    # plain booleans, not user-facing text) — a wrong type could otherwise break
    # Telegram's HTML parser for the summary message itself. Fail closed instead: require
    # each boolean guardrail to actually be present as a real bool, or refuse rather than
    # let incomplete/malformed safety metadata reach the summary at all.
    bool_guardrail_fields = [
        "needs_docker_socket", "has_own_reverse_proxy", "requires_companion_services",
        "has_extra_directives", "named_volumes_need_special_config",
    ]
    missing_or_bad = [
        field for field in bool_guardrail_fields if not isinstance(req.get(field), bool)
    ]
    if missing_or_bad or not isinstance(req.get("required_env"), list):
        notifier.notify(
            f"⚠️ Fry's response is missing or has the wrong type for required safety-guardrail "
            f"field(s) ({s(', '.join(missing_or_bad) or 'required_env')}) — refusing to treat an "
            f"incomplete response as a clean bill of health. This needs to be onboarded by hand."
        )
        return

    port_lines = "\n".join(f"• {p.get('container_port')} — {s(p.get('purpose', ''))}" for p in ports) or "none"
    volumes_text = ", ".join(s(v) for v in volumes) or "none"
    env_text = "\n".join(f"• {s(e.get('name'))} — {s(e.get('purpose', ''))}" for e in env_vars) or "none"

    summary = (
        f"🚀 <b>Fry's resolution: {s(req.get('project_name', stack_name))}</b>\n"
        f"Repo: {s(req.get('repo_url', 'unknown'))}\n"
        f"Image: <code>{s(req.get('image', 'unknown'))}</code>\n"
        f"Ports:\n{port_lines}\n"
        f"Volumes: {volumes_text}\n"
        f"Required env:\n{env_text}\n"
        f"Docker socket needed: {req.get('needs_docker_socket')}\n"
        f"Bundles its own reverse proxy: {req.get('has_own_reverse_proxy')}\n"
        f"Needs companion services: {req.get('requires_companion_services')}\n"
        f"Has extra directives (command/entrypoint/etc): {req.get('has_extra_directives')}\n"
        f"Named volumes need special config: {req.get('named_volumes_need_special_config')}\n"
        f"Notes: {s(req.get('notes', ''))}"
    )
    notifier.notify(summary)

    if (
        not req.get("sufficient_context")
        or not isinstance(req.get("image"), str)
        or not req["image"].strip()
    ):
        notifier.notify(
            "⚠️ Fry didn't find an authoritative compose block to work from — refusing to "
            "guess. This needs to be onboarded by hand."
        )
        return

    if req.get("needs_docker_socket"):
        notifier.notify(
            "⚠️ This project mounts the Docker socket directly (no socket-proxy sidecar) — "
            "refusing to auto-propose. That's a real blast-radius decision for a human to make "
            "by hand, not something to wave through."
        )
        return

    if req.get("has_own_reverse_proxy"):
        notifier.notify(
            "⚠️ This project bundles its own reverse proxy — that conflicts with Traefik "
            "already fronting everything here. Refusing to auto-propose; needs a human decision "
            "on which one wins."
        )
        return

    if env_vars:
        notifier.notify(
            "⚠️ This project requires environment variables/secrets Fry can't generate on its "
            "own (see list above). Refusing to auto-propose until those are supplied — generate "
            "them, write a `.env` with `664` perms, then onboard by hand."
        )
        return

    if req.get("requires_companion_services"):
        notifier.notify(
            "⚠️ This project needs its own companion service (its own database/cache/worker, "
            "not just the main app) — this installer only ever builds a single-service stack "
            "file. Refusing to auto-propose; needs to be onboarded by hand."
        )
        return

    if req.get("has_extra_directives"):
        notifier.notify(
            "⚠️ This project's own service block relies on directives (command/entrypoint/"
            "depends_on/devices/capabilities/etc) this installer doesn't carry through — only "
            "image/ports/volumes/healthcheck/labels are synthesized. Refusing to auto-propose; "
            "needs to be onboarded by hand."
        )
        return

    if req.get("named_volumes_need_special_config"):
        notifier.notify(
            "⚠️ This project's named volume(s) need special top-level config (external/driver/"
            "driver_opts) — this installer only ever emits plain default volumes, which would "
            "give the app the wrong (empty) storage. Refusing to auto-propose; needs to be "
            "onboarded by hand."
        )
        return

    appdata_dir = Path.home() / "apps" / stack_name
    if appdata_dir.exists():
        notifier.notify(
            f"⚠️ `{s(str(appdata_dir))}` already has content — this doesn't look like a clean "
            f"install. Refusing to auto-propose; check it by hand first."
        )
        return

    existing_compose = config.STACKS_ROOT / stack_name / "docker-compose.yml"
    if existing_compose.is_file():
        notifier.notify(
            f"⚠️ `{s(str(existing_compose))}` already exists — this installer only ever "
            f"proposes brand-new stacks, never a wholesale replacement of an existing one. "
            f"Refusing to auto-propose; if this stack needs editing, do it through the normal "
            f"diff-approval flow by hand instead."
        )
        return

    port = req.get("primary_port")
    # port gets spliced directly into a Traefik label below
    # ("...loadbalancer.server.port={port}") — must be a genuine int in the valid TCP
    # range, not just present, or a malformed/malicious string (e.g. containing a
    # newline) could inject arbitrary extra Traefik labels. Also require `ports` to be
    # non-empty: Fry's JSON isn't schema-enforced, so an empty ports list with a
    # non-null primary_port is inconsistent output, not a confirmed real port.
    if (
        not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535)
        or not ports or port not in {p.get("container_port") for p in ports}
    ):
        notifier.notify(
            "⚠️ Fry didn't resolve a single unambiguous, valid web-UI port to route to — "
            "refusing to auto-propose rather than guess which of the reported ports is correct."
        )
        return

    # Upstream bind-mount host paths (e.g. "./config:/data", "/opt/app/data:/data", or
    # "${HOME}/data:/data") are meaningful relative to the UPSTREAM project's own
    # checkout/host, not this one — copying them in verbatim would write to the wrong
    # location (or a path that doesn't exist here at all) rather than this host's
    # `~/apps/<stack>` convention. Remapping them correctly needs a human decision on
    # where the data should actually live, so refuse rather than guess. Long-syntax
    # (dict-form) volume entries and any other non-string entry are treated as
    # disqualifying too, rather than silently passed through unchecked.
    bind_mounts = [
        v for v in volumes
        if not isinstance(v, str) or v.split(":", 1)[0].startswith(("/", "./", "../", "~", "$"))
    ]
    if bind_mounts:
        notifier.notify(
            f"⚠️ This project uses host bind-mount paths or non-standard volume entries "
            f"({s(', '.join(str(v) for v in bind_mounts))}) rather than plain named volumes — "
            f"those paths are specific to the upstream project's own deployment, not this "
            f"host's `~/apps/<stack>` convention. Refusing to auto-propose; needs to be "
            f"onboarded by hand."
        )
        return

    # Defense in depth: these fields get spliced onto a single line each below (e.g.
    # "    image: {req['image']}"). An embedded line break followed by unindented text
    # would be reinterpreted by the YAML parser as a sibling key rather than part of
    # this scalar's value — refuse rather than let that reach the synthesized document
    # at all. PyYAML treats more than just "\n" as a line break: "\r", NEL (U+0085), and
    # the Unicode line/paragraph separators (U+2028/U+2029) all split a scalar the same
    # way, so a check for "\n" alone can be silently bypassed by any of those.
    yaml_linebreak_chars = "\n\r\x85  "
    if any(ch in (req.get("image") or "") for ch in yaml_linebreak_chars):
        notifier.notify("⚠️ Fry's resolved image contains embedded line breaks — refusing to propose.")
        return
    if any(isinstance(v, str) and any(ch in v for ch in yaml_linebreak_chars) for v in volumes):
        notifier.notify("⚠️ Fry's resolved volumes contain embedded line breaks — refusing to propose.")
        return

    # Built entirely from Fry's structured fields, not the raw upstream_service_yaml —
    # that block still declares its own host port bind and container_name, which this
    # host's standalone-stack convention (Traefik-routed, CASA_<NAME>, casaproxy) must
    # override rather than inherit. Only volumes/healthcheck are trusted verbatim.
    lines = [
        "networks:",
        "  casaproxy:",
        "    external: true",
        "",
        "services:",
        f"  {stack_name}:",
        f"    image: {req['image']}",
        f"    container_name: CASA_{stack_name.upper()}",
        "    restart: unless-stopped",
        "    networks:",
        "      - casaproxy",
    ]
    if volumes:
        lines.append("    volumes:")
        lines += [f"      - {v}" for v in volumes]

    healthcheck_yaml = req.get("healthcheck_yaml")
    if healthcheck_yaml:
        # Independently parse+validate this fragment before splicing it in — it must
        # contain exactly a "healthcheck:" key and nothing else. Without this, a
        # malicious/malformed fragment could smuggle in a sibling key (e.g. its own
        # "volumes:") that bypasses both the service-level allowed_keys check (volumes
        # is itself an allowed key) and the bind_mounts guardrail above, which only
        # inspects req["volumes"] — not whatever actually ends up in the final YAML.
        try:
            parsed_hc = yaml.safe_load(healthcheck_yaml)
        except yaml.YAMLError:
            parsed_hc = None
        if not isinstance(parsed_hc, dict) or set(parsed_hc.keys()) != {"healthcheck"}:
            notifier.notify(
                "⚠️ Fry's healthcheck_yaml didn't parse as a single, standalone `healthcheck:` "
                "block — refusing to propose."
            )
            return
        lines += healthcheck_yaml.rstrip("\n").split("\n")

    lines += [
        "    labels:",
        "      - traefik.enable=true",
        f"      - traefik.http.routers.{stack_name}-lan.rule=Host(`{domain}`)",
        f"      - traefik.http.routers.{stack_name}-lan.entrypoints=websecure",
        f"      - traefik.http.routers.{stack_name}-lan.tls=true",
        f"      - traefik.http.services.{stack_name}.loadbalancer.server.port={port}",
        f"      - traefik.http.routers.{stack_name}-lan.service={stack_name}",
    ]

    # Derived directly from `volumes` (already confirmed above to contain only
    # plain named-volume strings, no bind mounts) rather than trusting Fry's separately
    # model-supplied top_level_volumes field — that field could disagree with volumes
    # (omit or misspell an entry), producing a compose file that references an
    # undeclared volume. Deriving it from the same source volumes were built from
    # guarantees the two stay consistent by construction.
    top_level_volumes = list(dict.fromkeys(v.split(":", 1)[0] for v in volumes))
    if top_level_volumes:
        lines.append("")
        lines.append("volumes:")
        lines += [f"  {v}:" for v in top_level_volumes]

    new_content = "\n".join(lines) + "\n"

    # Defense in depth: req's string fields (healthcheck_yaml, volumes, image) are
    # model-generated and only lightly shaped by the prompt, not schema-enforced — a
    # malformed or adversarial response could smuggle extra YAML (another service,
    # embedded newlines reinterpreted as new keys) or a docker.sock mount that
    # contradicts a needs_docker_socket:false the guardrail above already trusted.
    # Parse the actual synthesized document independently and re-check it before
    # proposing, rather than relying solely on a human reading a (possibly truncated,
    # see fmt_diff) diff message.
    try:
        parsed = yaml.safe_load(new_content)
    except yaml.YAMLError as e:
        notifier.notify(f"⚠️ Synthesized compose failed to parse as YAML ({s(str(e))}) — refusing to propose.")
        return

    # Allowlist the whole document's top-level keys too, not just the service block's —
    # the newline guards above should already prevent it, but this is the actual backstop:
    # an injected sibling top-level key (e.g. "include:") would parse as legitimate YAML
    # without ever touching the services block the check below inspects.
    top_level_allowed = {"networks", "services", "volumes"}
    if not isinstance(parsed, dict) or set(parsed.keys()) - top_level_allowed:
        notifier.notify(
            "⚠️ Synthesized compose contains unexpected top-level keys beyond networks/services/"
            "volumes — refusing to propose."
        )
        return

    services = parsed.get("services") if isinstance(parsed, dict) else None
    if not isinstance(services, dict) or set(services.keys()) != {stack_name}:
        notifier.notify(
            "⚠️ Synthesized compose doesn't have exactly the expected single service block — "
            "refusing to propose."
        )
        return

    service_block = services[stack_name]

    # Allowlist rather than blocklist: this installer only ever writes these keys itself
    # (see the `lines` build above) — anything else appearing here means one of Fry's
    # model-generated string fields (image/healthcheck_yaml/volumes) smuggled extra YAML
    # in via embedded newlines (e.g. a sibling "privileged: true" or "network_mode: host"
    # line), not that a legitimate upstream requirement was missed.
    allowed_keys = {"image", "container_name", "restart", "networks", "volumes", "healthcheck", "labels"}
    extra_keys = set(service_block.keys()) - allowed_keys
    if extra_keys:
        notifier.notify(
            f"⚠️ Synthesized compose contains unexpected directives ({s(', '.join(sorted(extra_keys)))}) "
            f"beyond what this installer generates — refusing to propose."
        )
        return

    mount_strings = [str(v) for v in (service_block.get("volumes") or [])]
    if any("docker.sock" in v for v in mount_strings):
        notifier.notify(
            "⚠️ Synthesized compose would mount the Docker socket despite the guard above — "
            "refusing to propose."
        )
        return

    try:
        diff = bender.propose_compose_diff(
            stack_name, new_content,
            reason=f"Fry onboarding {url} as {domain}",
            is_new_stack=True,
        )
        notifier.request_approval(
            TelegramClient.fmt_diff(stack_name, f"New stack onboarded from {url}", diff["diff_text"]),
            diff["diff_id"], "diff",
        )
    except bender.SafetyError as e:
        notifier.notify(f"⚠️ Could not turn Fry's resolution into a diff: {s(str(e))}")


def _execute_plan(tg: TelegramClient, notifier: Notifier, state: PipelineState, plan_data: dict) -> None:
    try:
        result = bender.execute(plan_data, tg)
        if result["final_status"] == "success":
            notifier.notify(TelegramClient.fmt_complete(
                plan_data["id"],
                result["steps_completed"],
                result.get("errors", []),
            ))
        elif result["final_status"] == "blocked_sudo" and result["steps_completed"] == 0:
            # Bender already sent a detailed message with the literal blocked
            # command, and nothing ran before the block — not a real runtime
            # failure, so skip the generic notify and don't wake up Amy over it.
            # If earlier steps DID run first, fall through to the normal failure
            # path below: the plan is partially applied and needs the same
            # rollback-guidance/investigation treatment as any other failure.
            pass
        else:
            notifier.notify(
                f"⚠️ Plan #{plan_data['id']} finished with status: `{result['final_status']}`"
            )
            container = plan_data.get("container")
            if container:
                failed_step_detail = None
                steps = result.get("results") or []
                if steps:
                    failed_step = steps[-1]
                    # bender's "error" is already stderr-with-a-stdout-fallback (see
                    # error_summary in casa_bender.py execute()), so it isn't reliably
                    # "the stderr" — label it generically. Only tack on stdout_summary
                    # separately when it has content "error" doesn't already cover, to
                    # avoid duplicating the same text under two mislabeled headers.
                    step_error = failed_step.get("error") or ""
                    step_stdout = failed_step.get("stdout_summary") or ""
                    lines = []
                    if step_error:
                        lines.append(f"error_summary: {step_error}")
                    if step_stdout and step_stdout not in step_error:
                        lines.append(f"stdout: {step_stdout}")
                    output = "\n".join(lines) or "(empty)"
                    failed_step_detail = (
                        f"command: {failed_step.get('command', '')}\n"
                        f"exit_code: {failed_step.get('exit_code')}\n"
                        f"output:\n{output}"
                    )
                threading.Thread(
                    target=_investigate_failure,
                    args=(notifier, container, f"plan {plan_data['id']} failed: {result.get('errors')}"),
                    kwargs={"failed_step_detail": failed_step_detail},
                    daemon=True,
                ).start()
    except Exception as e:
        log.exception("Bender execution error")
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
        except Exception:
            log.exception("Scheduled pipeline error")
        time.sleep(PIPELINE_INTERVAL_HOURS * 3600)


def _run_update_pass(tg: TelegramClient, notifier: Notifier) -> None:
    """Zoidberg's canary update pass. Deliberately doesn't touch PipelineState's own
    machinery — it's silent-on-success by design, Telegram only speaks up on rollback,
    so there's no "plan awaiting approval" step for the routine case."""
    try:
        zoidberg.run_update_pass(tg=tg)
    except Exception as e:
        log.exception("Zoidberg update pass crashed")
        notifier.notify(f"🛑 *Zoidberg update pass crashed:* `{str(e)[:200]}`")


def _seconds_until_next_update_window() -> float:
    # naive on purpose: only ever compared against other naive values computed
    # right here, never leaves the process -- tz-awareness would buy nothing
    now = datetime.now()  # noqa: DTZ005
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
            except Exception:
                log.exception("Scheduled update pass error")
        time.sleep(3600)  # clear the target window before recomputing next week's delay


def _seconds_until_next_digest() -> float:
    # naive on purpose, see _seconds_until_next_update_window above
    now = datetime.now()  # noqa: DTZ005
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
        except Exception:
            log.exception("Digest error")
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
                except Exception:
                    log.exception("Update handler error")
        except KeyboardInterrupt:
            log.info("Farnsworth shutting down. Goodbye!")
            notifier.notify("🛑 <b>Planet Express going offline.</b>")
            break
        except Exception:
            log.exception("Poll loop error")
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
                config.LOG_DIR / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.log"
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
