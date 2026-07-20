"""
casa_bender.py — Bender: Action Executor
"I'm Bender, baby! Please insert girder. ...I mean, please insert command."

Executes approved plans from Farnsworth, step by step.
Streams per-step status to Telegram. Stops immediately on failure.
Never executes unapproved commands. Never runs in parallel.

Usage:
    python casa_bender.py <plan.json>     # execute a plan JSON file
    python casa_bender.py --rollback <plan.json>  # run rollback steps
"""

import argparse
import difflib
import fnmatch
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.bender")

# ── Safety constants ──────────────────────────────────────────────────────────
# Commands containing these strings require network_confirm flag in the plan
NETWORK_GUARD_TOKENS = ["CASA_TRAEFIK", "CASA_ADGUARD", "adguard", "traefik"]

# These stack names must NEVER be touched — single source of truth in config.py,
# imported here rather than kept as a local copy that could drift.
FORBIDDEN_STACKS = config.FORBIDDEN_STACKS

# These commands are never allowed regardless of plan content
# NOTE: bare "format" was removed 2026-07-04 — it's matched as a plain substring, so it
# also blocked every `docker inspect --format '...'` call (a harmless, read-only Go-template
# flag that Farnsworth's own plan template requires in every restart-verification step —
# meaning it blocked essentially every plan). mkfs/fdisk/parted/dd/shred below already cover
# real disk-destruction risk.
FORBIDDEN_COMMANDS = [
    "docker system prune",
    "rm -rf",
    "dd if=",
    "> /dev/",
    "mkfs",
    "fdisk",
    "parted",
    "shred",
]

COMMAND_TIMEOUT_SECONDS = 120

# Config-declared allowlist of sudo-scoped systemctl actions — single source of truth
# in config.py, same pattern as FORBIDDEN_STACKS above. Empty by default; a fresh
# install grants nothing until the operator declares it (and grants it at the OS
# level via sudoers.d) explicitly.
SUDO_ALLOWLIST = config.SUDO_ALLOWLIST

# Anything other than `sudo systemctl <action> <unit>` was never a legitimate use of
# the sudo grant this project asks for (docker needs no sudo — direct socket access).
# Deliberately requires a literal, bare `sudo` -- an earlier version of this regex
# tolerated an arbitrary-path prefix (`\S*/`) to allow /usr/bin/sudo, but `\S` also
# matches shell metacharacters: `$(sudo mount -a)/sudo systemctl restart
# casa-startup.service` satisfied that prefix, letting the whole string through
# _check_sudo_allowlist while the shell (shell=True) still executed the embedded
# `sudo mount -a` via command substitution -- an independent Codex review caught
# this before it shipped. Real plans only ever generate bare `sudo`; there is no
# real need to tolerate a path-prefixed spelling, so it's simply not supported.
#
# The unit-name group is deliberately a strict systemd-unit-name character class
# (letters, digits, `_.@:-`), NOT `\S+` -- a second Codex-caught bug: `\S+` has no
# literal whitespace but still matches e.g. `$(sudo${IFS}mount${IFS}-a)data.mount`,
# which both satisfies this regex AND passes fnmatch("*.mount") since it happens to
# end in ".mount" -- while the shell still executes the embedded command
# substitution. A strict character class rejects `$`, `(`, `)`, `{`, `}`, backticks,
# etc. outright, so no disguised-as-a-unit-name payload can ever reach fnmatch/the
# exact-match check at all.
_SUDO_SYSTEMCTL_RE = re.compile(
    r"^sudo\s+systemctl\s+(start|stop|restart)\s+([A-Za-z0-9_.@:-]+)$", re.IGNORECASE
)


# ── Safety checks ─────────────────────────────────────────────────────────────
class SafetyError(Exception):
    pass


def _split_command_segments(command: str) -> list[str]:
    """Split a compound shell command on control operators so each piece can be
    checked independently — otherwise a legitimate `sudo systemctl start x.mount &&
    sudo rm -rf /` could smuggle a forbidden second command past a whole-string check.
    Newlines split too: `_run_command()` runs everything with shell=True, and bash
    treats a newline as a statement separator exactly like `;`."""
    return [seg.strip() for seg in re.split(r"&&|\|\||;|\||\n", command) if seg.strip()]


def _sudo_action_allowed(unit: str, action: str) -> bool:
    action = action.lower()
    for grant in SUDO_ALLOWLIST.units:
        if grant.unit == unit and action in grant.actions:
            return True
    for grant in SUDO_ALLOWLIST.globs:
        if fnmatch.fnmatch(unit, grant.glob) and action in grant.actions:
            return True
    return False


def _check_sudo_allowlist(command: str) -> None:
    """Raise SafetyError for any segment that invokes `sudo` anywhere and isn't an
    explicitly declared (unit-or-glob, action) grant in config.yaml's sudo_allowlist
    -- fail closed on anything not declared, rather than trying to blocklist every bad
    sudo invocation individually.

    Deliberately checks for the word `sudo` *anywhere* in the segment, not just at the
    start -- a shell wrapper like `env sudo mount -a` or `sh -c 'sudo mount -a'` still
    invokes real sudo (subprocess.run uses shell=True), and would silently bypass a
    prefix-only check by never technically "starting with sudo" (a real gap an
    independent Codex review caught before this shipped)."""
    for segment in _split_command_segments(command):
        if not re.search(r"\bsudo\b", segment, re.IGNORECASE):
            continue
        m = _SUDO_SYSTEMCTL_RE.match(segment)
        if not m:
            raise SafetyError(
                f"Sudo command not in the declared allowlist (only 'sudo systemctl "
                f"start|stop|restart <unit>' can ever be permitted): '{segment}'"
            )
        action, unit = m.group(1), m.group(2)
        if not _sudo_action_allowed(unit, action):
            raise SafetyError(
                f"Sudo action '{action}' on '{unit}' is not declared in "
                f"config.yaml's sudo_allowlist: '{segment}'"
            )


def _safety_check(command: str, plan: dict) -> None:
    """Raise SafetyError if the command violates any constraint."""
    cmd_lower = command.lower()

    # Absolute forbidden commands
    for bad in FORBIDDEN_COMMANDS:
        if bad.lower() in cmd_lower:
            raise SafetyError(f"Forbidden command pattern detected: '{bad}'")

    # Forbidden stacks — use word boundary matching to avoid false positives
    # e.g. 'ai' must NOT match '--tail', 'clawbot' must NOT match 'clawbot-adjacent'
    for stack in FORBIDDEN_STACKS:
        if re.search(rf"\b{re.escape(stack)}\b", command, re.IGNORECASE):
            raise SafetyError(f"Forbidden stack referenced: '{stack}'")

    # Sudo scope — code-enforced, independent of whatever the plan's LLM-generated
    # command claims to need.
    _check_sudo_allowlist(command)

    # Network stack guard
    needs_net_confirm = any(tok in command for tok in NETWORK_GUARD_TOKENS)
    if needs_net_confirm and not plan.get("requires_network_confirm", False):
        raise SafetyError(
            "Command touches network stack (Traefik/AdGuard) but plan does not have "
            "requires_network_confirm: true. Refusing to execute."
        )


# ── Step success evaluation ───────────────────────────────────────────────────
def _step_succeeded(command: str, exit_code: int) -> bool:
    """Whether a step's exit code counts as success. `systemctl status` follows the
    LSB init-script convention where the exit code encodes the unit's *state*
    (0=running, 1/2=dead, 3=not running, 4=unknown) rather than whether the command
    itself ran correctly — so a status check on an already-known-inactive unit (exactly
    what Hermes asks Bender to investigate) would always report "step failed" even
    though the diagnostic worked perfectly. `systemctl is-active`/`is-enabled` are
    deliberately excluded from this — plans use those as real boolean success checks
    (e.g. verifying a restart worked), where exit 0 genuinely means success."""
    if re.search(r"\bsystemctl\s+status\b", command):
        return 0 <= exit_code <= 4
    return exit_code == 0


# ── Command runner ────────────────────────────────────────────────────────────
def _run_command(command: str) -> tuple[int, str, str]:
    """Run a shell command, return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            command,
            shell=True,       # needed for compound commands (&&, pipes)
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=None,         # inherit environment
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s"
    except Exception as e:
        return 1, "", str(e)


# ── Log step to file ──────────────────────────────────────────────────────────
def _log_step(plan_id: str, step: dict, exit_code: int, stdout: str, stderr: str) -> None:
    try:
        config.ensure_dirs()
        log_file = config.LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "plan_id": plan_id,
            "step_n": step.get("n"),
            "command": step.get("command"),
            "exit_code": exit_code,
            "stdout": stdout[:2000],
            "stderr": stderr[:1000],
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Failed to write step log: {e}")


# ── Safe prune ─────────────────────────────────────────────────────────────────
# Called only by Farnsworth's maybe_run_safe_prune(), which has already verified disk
# pressure is real and every container is in a known-safe state. Deliberately narrow:
# only image/network prune, never "docker system prune" (that string stays in
# FORBIDDEN_COMMANDS above and this still runs it through the same _safety_check).
SAFE_PRUNE_STEPS = [
    ("image prune", "docker image prune -a -f"),
    ("network prune", "docker network prune -f"),
]


def run_safe_prune() -> dict:
    """Run the whitelisted prune commands, logging each like a normal plan step.
    Does not itself decide whether pruning is safe — that's the caller's job."""
    results = []
    for label, cmd in SAFE_PRUNE_STEPS:
        _safety_check(cmd, plan={})
        exit_code, stdout, stderr = _run_command(cmd)
        _log_step("safe-prune", {"n": label, "command": cmd}, exit_code, stdout, stderr)
        results.append({
            "step": label, "command": cmd, "exit_code": exit_code,
            "stdout": stdout[:500], "stderr": stderr[:300],
        })
        log.info(f"Safe-prune {label}: exit {exit_code} — {stdout[:200]}")

    summary = "\n".join(
        f"{r['step']}: {r['stdout'] or ('failed: ' + r['stderr'] if r['exit_code'] else 'no change')}"
        for r in results
    )
    return {"results": results, "summary": summary}


# ── Supervised compose-file diffs (Phase 4) ────────────────────────────────────
# Bender's only file-editing capability, and deliberately narrow: only
# ~/stacks/*/docker-compose.yml files, never .env (secrets stay human-only). A
# proposed diff is never applied automatically — it needs its own Telegram
# approval, separate from the approval that runs the resulting plan. Always
# backed up before writing.
STACKS_ROOT = config.STACKS_ROOT
PENDING_DIFFS_FILE = config.STATE_DIR / "pending_diffs.json"


def _load_pending_diffs() -> dict:
    if not PENDING_DIFFS_FILE.exists():
        return {}
    try:
        return json.loads(PENDING_DIFFS_FILE.read_text())
    except Exception:
        return {}


def _save_pending_diffs(pending: dict) -> None:
    config.ensure_dirs()
    PENDING_DIFFS_FILE.write_text(json.dumps(pending, indent=2))


def read_service_block(stack_name: str, service_key: str) -> tuple[str, str] | None:
    """Return (full_file_content, exact_verbatim_block_text) for one service in a
    stack's docker-compose.yml, or None if the stack/service isn't found. Text-based,
    not a YAML round-trip — preserves comments/formatting exactly, which a
    parse-and-redump would lose. Used to give Amy real, copy-pasteable current state
    instead of asking her to describe a compose edit from memory/guesswork, and to let
    a splice (content.replace(block, new_block, 1)) produce an exact, minimal diff."""
    compose_path = STACKS_ROOT / stack_name / "docker-compose.yml"
    if not compose_path.is_file():
        return None
    content = compose_path.read_text()
    lines = content.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^ {{2}}{re.escape(service_key)}:\s*$", line):
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        # next line at <=2-space indent (a sibling service, or a new top-level key) ends the block
        if re.match(r"^ {0,2}\S", lines[j]):
            end = j
            break

    return content, "".join(lines[start:end])


def propose_compose_diff(stack_name: str, new_content: str, reason: str) -> dict:
    """Propose a diff to a stack's docker-compose.yml. Writes nothing to the real
    file — only records the proposal and returns a unified diff for a human to
    review in Telegram. Raises SafetyError for forbidden stacks or any path that
    isn't exactly stacks/<name>/docker-compose.yml."""
    if stack_name in FORBIDDEN_STACKS:
        raise SafetyError(f"Refusing to propose a diff for forbidden stack '{stack_name}'")

    compose_path = STACKS_ROOT / stack_name / "docker-compose.yml"
    if not compose_path.is_file():
        raise SafetyError(f"No docker-compose.yml for stack '{stack_name}' at {compose_path}")

    old_content = compose_path.read_text()
    if old_content == new_content:
        raise SafetyError("Proposed content is identical to the current file — nothing to diff")

    diff_text = "".join(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"{stack_name}/docker-compose.yml (current)",
        tofile=f"{stack_name}/docker-compose.yml (proposed)",
    ))

    diff_id = f"diff-{stack_name}-{int(time.time())}"
    pending = _load_pending_diffs()
    pending[diff_id] = {
        "stack": stack_name,
        "compose_path": str(compose_path),
        "new_content": new_content,
        "reason": reason,
        "diff_text": diff_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending_diffs(pending)
    log.info(f"Proposed diff {diff_id} for {stack_name}/docker-compose.yml: {reason}")
    return {"diff_id": diff_id, "diff_text": diff_text, "stack": stack_name}


def get_pending_diff(diff_id: str) -> dict | None:
    return _load_pending_diffs().get(diff_id)


def discard_pending_diff(diff_id: str) -> None:
    pending = _load_pending_diffs()
    pending.pop(diff_id, None)
    _save_pending_diffs(pending)


def apply_pending_diff(diff_id: str) -> dict:
    """Apply a previously-approved diff: back up the current file (.yml.bak.<ts>),
    then write the new content. Does not restart anything — that's still a
    separate, normal plan-approval step afterward."""
    entry = get_pending_diff(diff_id)
    if not entry:
        raise SafetyError(f"No pending diff found for '{diff_id}' (already applied or expired?)")

    compose_path = Path(entry["compose_path"])
    backup_path = compose_path.with_name(
        compose_path.name + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    backup_path.write_text(compose_path.read_text())
    compose_path.write_text(entry["new_content"])
    discard_pending_diff(diff_id)

    log.info(f"Applied diff {diff_id} to {compose_path} (backup: {backup_path})")
    return {"diff_id": diff_id, "compose_path": str(compose_path), "backup_path": str(backup_path)}


# ── Core execute function ─────────────────────────────────────────────────────
def execute(
    plan: dict,
    tg: TelegramClient | None = None,
) -> dict:
    """
    Execute all steps in an approved plan.
    Streams per-step status to Telegram if tg is provided.
    Returns execution result dict.

    Stops on first failure and asks user whether to continue or rollback.
    """
    plan_id = plan["id"]
    steps   = plan.get("steps", [])
    total   = len(steps)

    log.info(f"Bender starting execution of plan {plan_id} ({total} steps)")

    results = []
    errors  = []

    for step in steps:
        n           = step["n"]
        description = step.get("description", "")
        command     = step.get("command", "")

        log.info(f"Plan {plan_id} step {n}/{total}: {command}")

        # Safety check before every step
        try:
            _safety_check(command, plan)
        except SafetyError as e:
            msg = str(e)
            log.error(f"Safety check failed on step {n}: {msg}")
            if tg:
                tg.send(
                    f"🛑 *Safety block on step {n}*\n"
                    f"`{TelegramClient.s(msg)}`\n"
                    f"Execution halted. No further steps will run."
                )
            results.append({
                "n": n, "command": command,
                "exit_code": -1, "stdout_summary": "",
                "error": f"SAFETY_BLOCK: {msg}",
            })
            return {
                "plan_id": plan_id,
                "steps_completed": n - 1,
                "steps_total": total,
                "results": results,
                "final_status": "failed",
                "errors": [f"Step {n}: safety block"],
            }

        # Execute
        exit_code, stdout, stderr = _run_command(command)
        stdout_summary = stdout[:500] if stdout else ""

        _log_step(plan_id, step, exit_code, stdout, stderr)

        ok = _step_succeeded(command, exit_code)
        error_summary = stderr[:300] if stderr and not ok else ""

        # Telegram step update
        if tg:
            msg = TelegramClient.fmt_step_status(
                plan_id, n, total, description, ok, error_summary
            )
            tg.send(msg)

        step_result = {
            "n": n,
            "command": command,
            "exit_code": exit_code,
            "stdout_summary": stdout_summary,
        }
        if not ok:
            step_result["error"] = error_summary or f"exit code {exit_code}"
            errors.append(f"Step {n}: {error_summary or f'exit {exit_code}'}")

        results.append(step_result)

        if not ok:
            log.error(f"Step {n} failed (exit {exit_code}): {error_summary}")
            if tg:
                tg.send(TelegramClient.fmt_failed(
                    plan_id, n, description, error_summary or f"exit code {exit_code}"
                ))
            # Stop on failure — Farnsworth awaits user decision
            return {
                "plan_id": plan_id,
                "steps_completed": n - 1,
                "steps_total": total,
                "results": results,
                "final_status": "failed",
                "errors": errors,
            }

        log.info(f"Step {n} OK")

    log.info(f"Plan {plan_id} complete — all {total} steps succeeded")
    return {
        "plan_id": plan_id,
        "steps_completed": total,
        "steps_total": total,
        "results": results,
        "final_status": "success",
        "errors": errors,
    }


def execute_rollback(
    plan: dict,
    tg: TelegramClient | None = None,
) -> dict:
    """Execute rollback steps for a plan."""
    plan_id  = plan["id"]
    rollback = plan.get("rollback", [])
    total    = len(rollback)

    log.info(f"Bender executing rollback for plan {plan_id} ({total} steps)")

    results = []
    errors  = []

    for step in rollback:
        n       = step["n"]
        command = step.get("command", "")

        log.info(f"Rollback {plan_id} step {n}/{total}: {command}")

        try:
            _safety_check(command, plan)
        except SafetyError as e:
            log.error(f"Safety check failed on rollback step {n}: {e}")
            errors.append(f"Rollback step {n}: safety block")
            continue

        exit_code, stdout, stderr = _run_command(command)
        ok = _step_succeeded(command, exit_code)

        if tg:
            tg.send(
                f"↩️ Rollback step {n}/{total}: "
                f"{'✅' if ok else '❌'} `{TelegramClient.s(step.get('description', command[:60]))}`"
            )

        _log_step(f"{plan_id}-rollback", step, exit_code, stdout, stderr)
        results.append({"n": n, "command": command, "exit_code": exit_code})
        if not ok:
            errors.append(f"Rollback step {n}: exit {exit_code}")

    return {
        "plan_id": plan_id,
        "steps_completed": len(results),
        "steps_total": total,
        "results": results,
        "final_status": "rolled_back" if not errors else "partial_rollback",
        "errors": errors,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(description="Bender — Planet Express executor")
    parser.add_argument("plan_file", help="Path to plan JSON file")
    parser.add_argument("--rollback", action="store_true", help="Execute rollback steps")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = parser.parse_args()

    plan_path = Path(args.plan_file)
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}", file=sys.stderr)
        sys.exit(1)

    plan_data = json.loads(plan_path.read_text())

    # If the file contains a plans array, pick the first one
    if "plans" in plan_data and isinstance(plan_data["plans"], list):
        if not plan_data["plans"]:
            print("No plans in file.", file=sys.stderr)
            sys.exit(0)
        plan_data = plan_data["plans"][0]

    if args.dry_run:
        section = plan_data.get("rollback" if args.rollback else "steps", [])
        print(f"DRY RUN — {'rollback' if args.rollback else 'execution'} steps for plan {plan_data['id']}:")
        for step in section:
            print(f"  Step {step['n']}: {step['command']}")
        sys.exit(0)

    if args.rollback:
        result = execute_rollback(plan_data)
    else:
        result = execute(plan_data)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["final_status"] in ("success", "rolled_back") else 1)
