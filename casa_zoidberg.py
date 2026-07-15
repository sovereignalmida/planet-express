"""
casa_zoidberg.py — Dr. Zoidberg: Canary-Tested Auto-Patcher
"Ah, sweet mystery of life, at last I've found you! ...also, that container's dead, Jim."

Weekly (or on-demand): for every eligible stack, pull each service's image and compare
digests. Anything that actually changed gets a one-service-at-a-time canary rollout:
recreate with the new image, watch it using the same crash-loop signal Leela uses, and if
it doesn't stabilize, automatically roll back to the previous image and tell the user why.
Silent on success by design — only speaks up when it can't self-heal on its own.

Never touches Traefik or AdGuard (network-guarded, same as Bender) — those always go
through the normal plan/approval flow. Never touches forbidden stacks (ai/clawbot) — same
list Bender enforces, imported from config.py so it can't drift.

Usage:
    python casa_zoidberg.py              # run the canary-update pass now
    python casa_zoidberg.py --dry-run     # report what would be pulled, touch nothing
"""

import argparse
import json
import logging
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import casa_bender as bender
import casa_amy as amy
from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.zoidberg")

# Same rule Bender enforces at runtime for restart/start commands — never touch these
# services with an automated update; they always go through the human-approved plan flow.
NETWORK_GUARD_SERVICE_SUBSTRINGS = ["traefik", "adguard"]

# User-configurable: add stack names here to skip auto-update for that stack entirely
# (e.g. if you want to hand-manage updates for something). Empty by default.
EXCLUDE_STACKS: set[str] = set()

# User-configurable: (stack_name, service_key) pairs to skip auto-update for
# individually -- for one service inside an otherwise-normal shared stack that
# shouldn't be auto-canaried.
# - ("services", "backend") / ("services", "frontend"): Billarr
#   (ghcr.io/sovereignalmida/billarr-*) -- the user's own actively-developed app, migrated
#   into the services stack 2026-07-07 from its former standalone location
#   (~/apps/services/billarr, orphaned from ~/stacks/ before that). Auto-canary-pulling
#   someone's own :latest build on a schedule is not the same risk profile as a public
#   app's routine security patch -- leave its deploy cadence to the user.
EXCLUDE_SERVICES: set[tuple[str, str]] = {("services", "backend"), ("services", "frontend")}

PULL_TIMEOUT_SECONDS = 300
CANARY_WATCH_SECONDS = 90
CANARY_POLL_INTERVAL_SECONDS = 5
ROLLBACK_WATCH_SECONDS = 30
ROLLBACK_CANDIDATE_GRACE_MINUTES = 15
INTER_SERVICE_DELAY_SECONDS = 20

ROLLBACK_CANDIDATES_FILE = config.STATE_DIR / "rollback_candidates.json"
UPDATE_HISTORY_FILE = config.STATE_DIR / "update_history.json"


def _run(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


# ── Discovery ──────────────────────────────────────────────────────────────────
def eligible_stacks() -> list[Path]:
    """Every stack config.py considers active (forbidden stacks already excluded),
    minus anything in this module's own EXCLUDE_STACKS."""
    return [d for d in config.active_stack_dirs() if d.name not in EXCLUDE_STACKS]


def stack_services(stack_dir: Path) -> list[str]:
    exit_code, out, err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml config --services"
    )
    if exit_code != 0:
        log.warning(f"Could not list services for {stack_dir.name}: {err}")
        return []
    services = [s for s in out.splitlines() if s.strip()]
    return [
        s for s in services
        if not any(tok in s.lower() for tok in NETWORK_GUARD_SERVICE_SUBSTRINGS)
        and (stack_dir.name, s) not in EXCLUDE_SERVICES
    ]


def service_image_id(stack_dir: Path, service: str) -> str | None:
    """Image ID the RUNNING container for this service currently uses, if any. This is
    the rollback target if a canary update goes wrong — it's the last known-good state,
    which is not necessarily the same as what the compose-referenced tag resolves to
    locally (see service_image_ref/local_image_id below)."""
    exit_code, out, err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml images -q {service}"
    )
    if exit_code != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0]


def service_image_ref(stack_dir: Path, service: str) -> str | None:
    """The image reference (e.g. 'amir20/dozzle:latest') this service resolves to per
    its compose config — not what's running, what the compose file/env vars say."""
    exit_code, out, err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml config --images {service}"
    )
    if exit_code != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0]


def local_image_id(image_ref: str) -> str | None:
    """What a repo:tag reference currently resolves to in the local image cache, if it's
    been pulled at all. Comparing this against service_image_id() (the running
    container's actual image) is the real "is there an update to apply" signal — NOT
    comparing this value to itself before/after a pull, which is wrong whenever a tag was
    already pulled fresh at some point without the container ever being recreated to
    match it (exactly what happened with dozzle during this Phase's live validation:
    :latest had already been re-pulled during an earlier stack recovery, but the running
    container was still 7 weeks old — comparing "running before" vs "running after" a
    no-op pull always reported no_change, silently missing a real pending update)."""
    exit_code, out, err = _run(
        "docker image inspect --format " + shlex.quote("{{.Id}}") + f" {image_ref}"
    )
    if exit_code != 0 or not out.strip():
        return None
    return out.strip()


# ── Rollback-candidate bookkeeping (also read by Farnsworth's safe-prune gate) ──
def _load_rollback_candidates() -> dict:
    if not ROLLBACK_CANDIDATES_FILE.exists():
        return {"candidates": []}
    try:
        return json.loads(ROLLBACK_CANDIDATES_FILE.read_text())
    except Exception:
        return {"candidates": []}


def _save_rollback_candidates(data: dict) -> None:
    config.ensure_dirs()
    ROLLBACK_CANDIDATES_FILE.write_text(json.dumps(data, indent=2))


def _add_rollback_candidate(stack: str, service: str, old_image_id: str) -> None:
    data = _load_rollback_candidates()
    now = datetime.now(timezone.utc)
    # Drop expired entries while we're here rather than letting the file grow forever.
    data["candidates"] = [
        c for c in data.get("candidates", [])
        if datetime.fromisoformat(c["expires_at"]) > now
    ]
    data["candidates"].append({
        "stack": stack, "service": service, "old_image_id": old_image_id,
        "recorded_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ROLLBACK_CANDIDATE_GRACE_MINUTES)).isoformat(),
    })
    _save_rollback_candidates(data)


def _clear_rollback_candidate(stack: str, service: str) -> None:
    data = _load_rollback_candidates()
    data["candidates"] = [
        c for c in data.get("candidates", [])
        if not (c["stack"] == stack and c["service"] == service)
    ]
    _save_rollback_candidates(data)


def _log_update_history(entry: dict) -> None:
    config.ensure_dirs()
    history = []
    if UPDATE_HISTORY_FILE.exists():
        try:
            history = json.loads(UPDATE_HISTORY_FILE.read_text())
        except Exception:
            history = []
    history.append(entry)
    UPDATE_HISTORY_FILE.write_text(json.dumps(history[-200:], indent=2))  # cap growth


# ── Canary health check (mirrors Leela's crash-loop signal) ─────────────────────
def _container_name_for(stack_dir: Path, service: str) -> str | None:
    exit_code, out, err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml ps -q {service}"
    )
    if exit_code != 0 or not out.strip():
        return None
    container_id = out.strip().splitlines()[0]
    exit_code, name, _ = _run(f"docker inspect --format {{{{.Name}}}} {container_id}")
    return name.lstrip("/") if exit_code == 0 and name else None


def _is_healthy_now(container_name: str) -> tuple[bool, str]:
    """Same signal Leela's check_containers() uses: running, not crash-looping, not
    unhealthy. A container that never becomes healthy at all (no healthcheck defined,
    status stays 'running') is treated as OK — absence of a healthcheck isn't a failure.

    Most services on this host (e.g. Radarr) have no Docker healthcheck at all, in which
    case `.State.Health` doesn't exist in the inspect output and a bare
    `{{.State.Health.Status}}` template errors out completely — not just that field, the
    whole `docker inspect` call fails. The `{{if .State.Health}}...{{else}}none{{end}}`
    guard is required, not cosmetic; caught by dry-run testing before this ever ran live."""
    exit_code, out, err = _run(
        "docker inspect --format "
        + shlex.quote(
            "{{.State.Status}}\t{{.RestartCount}}\t"
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
        )
        + f" {container_name}"
    )
    if exit_code != 0:
        return False, f"inspect failed: {err}"
    parts = out.split("\t")
    status = parts[0] if len(parts) > 0 else ""
    restart_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    health = parts[2] if len(parts) > 2 else ""
    if status != "running":
        return False, f"status={status}"
    if health == "unhealthy":
        return False, "healthcheck failing"
    if restart_count >= 1:
        return False, f"restarted {restart_count}x during watch window"
    return True, "ok"


def _watch_until_stable(container_name: str, seconds: int) -> tuple[bool, str]:
    elapsed = 0
    last_reason = "no data"
    while elapsed < seconds:
        time.sleep(CANARY_POLL_INTERVAL_SECONDS)
        elapsed += CANARY_POLL_INTERVAL_SECONDS
        ok, reason = _is_healthy_now(container_name)
        last_reason = reason
        if not ok:
            return False, reason
    return True, last_reason


# ── Rollback ───────────────────────────────────────────────────────────────────
def _rollback(stack_dir: Path, service: str, old_id: str | None,
              tg: TelegramClient | None, reason: str) -> dict:
    stack_name = stack_dir.name

    if not old_id:
        msg = (
            f"🛑 *{stack_name}/{service} update failed* ({reason}) and there's no previous "
            f"image ID to roll back to — needs manual attention."
        )
        log.error(msg)
        if tg:
            tg.send(msg)
        return {"stack": stack_name, "service": service, "status": "failed_no_rollback", "reason": reason}

    log.warning(f"{stack_name}/{service}: rolling back to {old_id} ({reason})")
    _, logs_out, _ = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml logs --tail 50 {service}"
    )

    new_id = service_image_id(stack_dir, service)
    image_repo = None
    if new_id:
        _, repo_out, _ = _run(
            "docker inspect --format "
            + shlex.quote("{{if .RepoTags}}{{index .RepoTags 0}}{{end}}")
            + f" {new_id}"
        )
        image_repo = repo_out.strip() if repo_out and ":" in repo_out else None

    rolled_back_ok = False
    if image_repo:
        plan_ctx = {"id": f"zoidberg-rollback-{stack_name}-{service}"}
        try:
            bender._safety_check(f"docker tag {old_id} {image_repo}", plan=plan_ctx)
            exit_code, _, err = bender._run_command(f"docker tag {old_id} {image_repo}")
            if exit_code == 0:
                bender._safety_check(
                    f"docker compose -f {stack_dir}/docker-compose.yml up -d {service}",
                    plan=plan_ctx,
                )
                exit_code, _, err = bender._run_command(
                    f"docker compose -f {stack_dir}/docker-compose.yml up -d {service}"
                )
                if exit_code == 0:
                    container_name = _container_name_for(stack_dir, service)
                    if container_name:
                        rolled_back_ok, _ = _watch_until_stable(container_name, ROLLBACK_WATCH_SECONDS)
        except bender.SafetyError as e:
            log.error(f"Rollback safety check blocked {stack_name}/{service}: {e}")

    _clear_rollback_candidate(stack_name, service)
    _log_update_history({
        "ts": datetime.now(timezone.utc).isoformat(), "stack": stack_name, "service": service,
        "old_id": old_id, "new_id": new_id,
        "status": "rolled_back" if rolled_back_ok else "rollback_failed",
        "reason": reason,
    })

    status_line = (
        "rolled back to the previous image successfully" if rolled_back_ok
        else "⚠️ rollback ALSO failed — needs manual attention now"
    )
    msg = (
        f"🩺 *Zoidberg: {stack_name}/{service} update failed*\n"
        f"Reason: {TelegramClient.s(reason)}\n"
        f"Action: {status_line}\n"
        f"Recent logs:\n`{TelegramClient.s(logs_out[-800:])}`"
    )
    log.warning(msg.replace("\n", " | "))
    if tg:
        tg.send(msg)
        _investigate_update_failure(stack_name, service, reason, logs_out, tg)

    return {
        "stack": stack_name, "service": service,
        "status": "rolled_back" if rolled_back_ok else "rollback_failed",
        "reason": reason,
    }


def _investigate_update_failure(
    stack_name: str, service: str, reason: str, logs_tail: str, tg: TelegramClient
) -> None:
    """Escalate a canary-update failure to Amy for deeper diagnosis. Runs after the
    rollback message so the user sees "here's what happened" before "here's why" —
    never blocks the update pass itself on Amy's (slower, LLM-backed) analysis."""
    try:
        diagnosis = amy.diagnose(
            stack=stack_name, service=service, container_name=service,
            reason=reason, logs_tail=logs_tail,
        )
    except Exception as e:
        log.exception(f"Amy investigation crashed for {stack_name}/{service}: {e}")
        tg.send(f"🛑 Amy's investigation of {stack_name}/{service} crashed: `{str(e)[:200]}`")
        return

    tg.send(TelegramClient.fmt_diagnosis(stack_name, service, diagnosis))
    remediation = diagnosis.get("proposed_remediation", {})
    if remediation.get("requires_compose_edit"):
        tg.send(
            f"📝 Amy says this needs a compose-file edit: "
            f"{TelegramClient.s(remediation.get('compose_edit_description', '(no description given)'))}\n\n"
            f"She can't write the file herself — that edit still needs to be made by hand "
            f"(or via a follow-up session) and proposed through the normal diff-approval flow."
        )


# ── Core canary update for one service ──────────────────────────────────────────
def canary_update_service(
    stack_dir: Path, service: str, tg: TelegramClient | None, dry_run: bool = False
) -> dict:
    stack_name = stack_dir.name
    # The rollback target is what's actually RUNNING right now, captured before we touch
    # anything — not what the tag happens to resolve to (those can differ, see
    # local_image_id's docstring).
    old_id = service_image_id(stack_dir, service)
    image_ref = service_image_ref(stack_dir, service)

    exit_code, out, err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml pull {service}",
        timeout=PULL_TIMEOUT_SECONDS,
    )
    if exit_code != 0:
        log.warning(f"{stack_name}/{service}: pull failed — {err}")
        return {"stack": stack_name, "service": service, "status": "pull_failed", "reason": err}

    if not image_ref:
        log.warning(f"{stack_name}/{service}: could not resolve image reference")
        return {"stack": stack_name, "service": service, "status": "unknown_no_image_ref"}

    new_id = local_image_id(image_ref)
    if not new_id or new_id == old_id:
        return {"stack": stack_name, "service": service, "status": "no_change"}

    if dry_run:
        return {
            "stack": stack_name, "service": service, "status": "update_available_dry_run",
            "old_id": old_id, "new_id": new_id,
        }

    log.info(f"{stack_name}/{service}: new image available ({old_id} -> {new_id}), recreating...")
    if old_id:
        _add_rollback_candidate(stack_name, service, old_id)

    plan_ctx = {"id": f"zoidberg-update-{stack_name}-{service}"}
    up_cmd = f"docker compose -f {stack_dir}/docker-compose.yml up -d {service}"
    try:
        bender._safety_check(up_cmd, plan=plan_ctx)
    except bender.SafetyError as e:
        log.warning(f"{stack_name}/{service}: update skipped, safety check blocked it — {e}")
        _clear_rollback_candidate(stack_name, service)
        return {"stack": stack_name, "service": service, "status": "skipped_safety_block", "reason": str(e)}

    exit_code, out, err = bender._run_command(up_cmd)
    bender._log_step(f"zoidberg-update-{stack_name}", {"n": 1, "command": up_cmd}, exit_code, out, err)

    if exit_code != 0:
        log.warning(f"{stack_name}/{service}: up -d failed immediately — {err}")
        return _rollback(stack_dir, service, old_id, tg, reason=f"up -d failed: {err[:200]}")

    container_name = _container_name_for(stack_dir, service)
    if not container_name:
        log.warning(f"{stack_name}/{service}: could not resolve container name to watch")
        _clear_rollback_candidate(stack_name, service)
        return {"stack": stack_name, "service": service, "status": "unknown_no_container_name"}

    stable, reason = _watch_until_stable(container_name, CANARY_WATCH_SECONDS)
    if stable:
        _clear_rollback_candidate(stack_name, service)
        _log_update_history({
            "ts": datetime.now(timezone.utc).isoformat(), "stack": stack_name, "service": service,
            "old_id": old_id, "new_id": new_id, "status": "updated",
        })
        log.info(f"{stack_name}/{service}: update stable after {CANARY_WATCH_SECONDS}s")
        return {"stack": stack_name, "service": service, "status": "updated", "old_id": old_id, "new_id": new_id}

    return _rollback(stack_dir, service, old_id, tg, reason=reason)


# ── Full pass ────────────────────────────────────────────────────────────────────
def run_update_pass(tg: TelegramClient | None = None, dry_run: bool = False) -> list[dict]:
    results = []
    stacks = eligible_stacks()
    log.info(f"Zoidberg update pass starting — {len(stacks)} eligible stack(s)")
    for stack_dir in stacks:
        services = stack_services(stack_dir)
        for service in services:
            try:
                result = canary_update_service(stack_dir, service, tg, dry_run=dry_run)
            except Exception as e:
                log.exception(f"Zoidberg update crashed for {stack_dir.name}/{service}: {e}")
                result = {"stack": stack_dir.name, "service": service, "status": "error", "reason": str(e)}
            results.append(result)
            if not dry_run:
                time.sleep(INTER_SERVICE_DELAY_SECONDS)

    updated = [r for r in results if r["status"] == "updated"]
    rolled_back = [r for r in results if r["status"] in ("rolled_back", "rollback_failed", "failed_no_rollback")]
    log.info(
        f"Zoidberg update pass complete — {len(updated)} updated cleanly, "
        f"{len(rolled_back)} needed rollback, {len(results)} services checked total"
    )
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(description="Zoidberg — Planet Express canary auto-updater")
    parser.add_argument("--dry-run", action="store_true", help="Report what would update, touch nothing")
    args = parser.parse_args()

    config.ensure_dirs()
    out = run_update_pass(tg=None, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
