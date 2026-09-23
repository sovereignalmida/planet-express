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
from datetime import datetime, timezone
from pathlib import Path

import casa_amy as amy
import casa_bender as bender
import config
from planet_express.execution import actions, runbook as runbooks
from state_models import UpdateHistory
from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.zoidberg")

# Same rule Bender enforces at runtime for restart/start commands — never touch these
# services with an automated update; they always go through the human-approved plan flow.
NETWORK_GUARD_SERVICE_SUBSTRINGS = ["traefik", "adguard"]

# User-configurable: add stack names here to skip auto-update for that stack entirely
# (e.g. if you want to hand-manage updates for something). Empty by default.
EXCLUDE_STACKS: set[str] = set()

# (stack_name, service_key) pairs to skip auto-update for individually -- for one service
# inside an otherwise-normal shared stack that shouldn't be auto-canaried (e.g. your own
# actively-developed :latest build, where auto-pulling on a schedule isn't the same risk
# profile as a public app's routine security patch). Declared in config.yaml, not here --
# this used to be a local constant, which is exactly the kind of drifting copy
# config.FORBIDDEN_STACKS was already consolidated to avoid.
EXCLUDE_SERVICES = config.EXCLUDE_SERVICES

PULL_TIMEOUT_SECONDS = 300
CANARY_WATCH_SECONDS = 90
CANARY_POLL_INTERVAL_SECONDS = 5
INTER_SERVICE_DELAY_SECONDS = 20

UPDATE_HISTORY_FILE = config.UPDATE_HISTORY_FILE


def _run(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
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


def _normalize_image_id(image_id: str) -> str:
    """`docker compose images -q` prints the bare hex image ID, while `docker image inspect
    --format {{.Id}}` prints "sha256:<hex>" (seen with Compose 2.40.3 / Engine 29.1.3 in
    the test homelab). Compared raw, they never matched, so every service looked updated:
    the canary pass recreated all of them, and crash-looping services raised false
    "rollback also failed" alerts. Normalize both sides to bare hex."""
    return image_id.strip().removeprefix("sha256:")


def service_image_id(stack_dir: Path, service: str) -> str | None:
    """Image ID the RUNNING container for this service currently uses, if any. This is
    the rollback target if a canary update goes wrong — it's the last known-good state,
    which is not necessarily the same as what the compose-referenced tag resolves to
    locally (see service_image_ref/local_image_id below)."""
    exit_code, out, _err = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml images -q {service}"
    )
    if exit_code != 0 or not out.strip():
        return None
    return _normalize_image_id(out.strip().splitlines()[0])


def service_image_ref(stack_dir: Path, service: str) -> str | None:
    """The image reference (e.g. 'amir20/dozzle:latest') this service resolves to per
    its compose config — not what's running, what the compose file/env vars say."""
    exit_code, out, _err = _run(
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
    exit_code, out, _err = _run(
        "docker image inspect --format " + shlex.quote("{{.Id}}") + f" {image_ref}"
    )
    if exit_code != 0 or not out.strip():
        return None
    return _normalize_image_id(out)


def _log_update_history(entry: dict) -> None:
    config.ensure_dirs()
    entries = []
    if UPDATE_HISTORY_FILE.exists():
        try:
            raw = json.loads(UPDATE_HISTORY_FILE.read_text())
            # Pre-Spec-3 format was a bare top-level list with no envelope. That history
            # is low-stakes (past canary-update outcomes only, nothing operationally
            # load-bearing) and isn't migrated -- an old bare list on disk here just
            # means "nothing to carry forward," not an error.
            if isinstance(raw, dict):
                entries = raw.get("entries", [])
        except Exception:  # noqa: BLE001
            entries = []
    entries.append(entry)
    history = UpdateHistory(entries=entries[-200:])  # cap growth
    UPDATE_HISTORY_FILE.write_text(history.model_dump_json(indent=2))


# ── Canary health check (mirrors Leela's crash-loop signal) ─────────────────────
# Moved to planet_express/execution/actions.py (landing 1a, T5) so the typed-action
# verifier shares one implementation. These names stay as thin wrappers: the canary code
# below and tests/test_zoidberg_health_regression.py call them unchanged. A freshly
# recreated container has no prior restarts, so the baseline stays at the default 0.
def _container_name_for(stack_dir: Path, service: str) -> str | None:
    return actions.service_container(stack_dir, service)


def _is_healthy_now(container_name: str) -> tuple[bool, str]:
    return actions.container_health(container_name)


def _watch_until_stable(container_name: str, seconds: int) -> tuple[bool, str]:
    return actions.watch_until_stable(
        container_name, seconds, poll_seconds=CANARY_POLL_INTERVAL_SECONDS
    )


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
        log.exception(f"Amy investigation crashed for {stack_name}/{service}")
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


# ── Typed canary update (slice 5b-3) ────────────────────────────────────────────
def _classify(step: dict | None, result) -> tuple[str, str]:
    """(status, reason) in Zoidberg's own vocabulary, from the engine's step row.

    Deterministic, not reason-matching: `update.canary` records its outputs only once it has
    actually deployed something, so their presence separates "the update was tried and undone"
    from "it was refused before anything moved".
    """
    if result.outcome == "refused" or step is None:
        return "skipped", result.reason
    deployed = bool(step.get("output"))
    if step["status"] == "passed":
        return ("updated", step["reason"]) if step["effect"] == "applied" else ("no_change", step["reason"])
    if step["effect"] == "unknown":
        # No outputs means the step never got as far as reporting them — an unexpected failure
        # after dispatch, not "there was no image to go back to": the rollback window is open and
        # holds that image (Codex, T42).
        return ("rollback_failed" if deployed else "interrupted"), step["reason"]
    if deployed:
        return "rolled_back", step["reason"]
    if step["reason"].startswith("pull failed"):
        return "pull_failed", step["reason"]
    return "skipped", step["reason"]


def _typed_canary(stack_dir: Path, service: str, tg: TelegramClient | None, commands) -> dict:
    """One service, one `update.canary` runbook, run on the engine under D34."""
    stack_name = stack_dir.name
    base = {"stack": stack_name, "service": service}
    # Eligibility first: a digest-pinned or build-only service has no tag to move, so it is refused
    # here — recorded, with no execution created for something that could never run (Codex, T42).
    reference = service_image_ref(stack_dir, service)
    if not actions.is_canary_reference(reference):
        reason = (f"{reference} is not a canary-eligible image reference" if reference
                  else "no image reference (build-only service)")
        log.info(f"{stack_name}/{service}: {reason}")
        commands.record_event("canary.ineligible", stack=stack_name, service=service,
                              reason=reason, requested_via="zoidberg")
        return base | {"status": "skipped", "reason": reason}
    try:
        target = actions.resolve_target(stack_name, service, for_mutation=True)
        binding = commands.binder.service(target, timeout=actions.DOCKER_TIMEOUT_SECONDS)
    except actions.TargetError as exc:
        log.warning(f"{stack_name}/{service}: not updatable — {exc}")
        commands.record_event("canary.ineligible", stack=stack_name, service=service,
                              reason=str(exc), requested_via="zoidberg")
        return base | {"status": "skipped", "reason": str(exc)}

    runbook = runbooks.Runbook.model_validate({
        "title": f"Canary update {stack_name}/{service}",
        "steps": [{"type": "update.canary", "params": {"stack": stack_name, "service": service},
                   "binding": binding}],
        "artifacts": {},
    })
    result = commands.run_automatic(runbook, origin="zoidberg",
                                    target_key=f"{stack_name}/{service}")
    step = (result.steps or [None])[0]
    status, reason = _classify(step, result)
    outputs = (step or {}).get("output") or {}
    entry = base | {"status": status, "reason": reason,
                    "old_id": outputs.get("old_image_id"), "new_id": outputs.get("new_image_id")}

    if status in ("updated", "rolled_back", "rollback_failed", "interrupted"):
        _log_update_history({"ts": datetime.now(timezone.utc).isoformat(), **base,
                             "old_id": entry["old_id"], "new_id": entry["new_id"],
                             "status": status, "reason": reason})
    if status == "updated":
        log.info(f"{stack_name}/{service}: {reason}")
    elif status in ("rolled_back", "rollback_failed", "interrupted"):
        _report_failed_update(stack_dir, service, tg, status, reason)
    else:
        log.info(f"{stack_name}/{service}: {status} — {reason}")
    return entry


def _report_failed_update(stack_dir: Path, service: str, tg: TelegramClient | None,
                          status: str, reason: str) -> None:
    """Today's rollback message and Amy escalation, unchanged in shape."""
    stack_name = stack_dir.name
    _, logs_out, _ = _run(
        f"docker compose -f {stack_dir}/docker-compose.yml logs --tail 50 {service}")
    action = {
        "rolled_back": "rolled back to the previous image successfully",
        "rollback_failed": "⚠️ rollback ALSO failed — needs manual attention now",
        "interrupted": "⚠️ the update did not finish and its outcome is unknown — its rollback "
                       "image is held back from pruning until you settle it",
    }[status]
    msg = (
        f"🩺 *Zoidberg: {stack_name}/{service} update failed*\n"
        f"Reason: {TelegramClient.s(reason)}\n"
        f"Action: {action}\n"
        f"Recent logs:\n`{TelegramClient.s(logs_out[-800:])}`"
    )
    log.warning(msg.replace("\n", " | "))
    if tg:
        tg.send(msg)
        _investigate_update_failure(stack_name, service, reason, logs_out, tg)


# ── Core canary update for one service ──────────────────────────────────────────
def canary_update_service(
    stack_dir: Path, service: str, tg: TelegramClient | None, dry_run: bool = False,
    commands=None,
) -> dict:
    """Update one service. A real pass runs the typed `update.canary` step on the engine (D34);
    `--dry-run` stays a read-only report and never recreates anything."""
    if not dry_run:
        if commands is None:
            raise ValueError("a real canary pass needs the command service (slice 5b-3)")
        return _typed_canary(stack_dir, service, tg, commands)

    stack_name = stack_dir.name
    # The rollback target is what's actually RUNNING right now — not what the tag happens to
    # resolve to (those can differ, see local_image_id's docstring).
    old_id = service_image_id(stack_dir, service)
    image_ref = service_image_ref(stack_dir, service)
    exit_code, _out, err = _run(
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
    return {
        "stack": stack_name, "service": service, "status": "update_available_dry_run",
        "old_id": old_id, "new_id": new_id,
    }


# ── Full pass ────────────────────────────────────────────────────────────────────
def run_update_pass(tg: TelegramClient | None = None, dry_run: bool = False,
                    commands=None) -> list[dict]:
    results = []
    stacks = eligible_stacks()
    log.info(f"Zoidberg update pass starting — {len(stacks)} eligible stack(s)")
    for stack_dir in stacks:
        services = stack_services(stack_dir)
        for service in services:
            try:
                result = canary_update_service(stack_dir, service, tg, dry_run=dry_run,
                                               commands=commands)
            except Exception as e:
                log.exception(f"Zoidberg update crashed for {stack_dir.name}/{service}")
                result = {"stack": stack_dir.name, "service": service, "status": "error", "reason": str(e)}
            results.append(result)
            if not dry_run:
                time.sleep(INTER_SERVICE_DELAY_SECONDS)

    updated = [r for r in results if r["status"] == "updated"]
    rolled_back = [r for r in results
                   if r["status"] in ("rolled_back", "rollback_failed", "interrupted")]
    log.info(
        f"Zoidberg update pass complete — {len(updated)} updated cleanly, "
        f"{len(rolled_back)} needed rollback, {len(results)} services checked total"
    )
    return results


class _CliState:
    """The CLI holds no host-mutation lock — it refuses to run at all while core is up (see
    main()), so there is no second mutator to coordinate with."""

    busy_reason = "cli"
    mutation_owner = None

    def try_begin_mutation(self, owner, **_):
        return True

    def end_mutation(self, owner, **_):
        return None


def _cli_command_service():
    """A command service for a `--force`d CLI pass: the engine path is the only way to update."""
    from notifier import FakeNotifier
    from planet_express.application.command_service import CommandService
    from planet_express.core.store import Store

    store = Store(config.ACTIONS_DB)
    store.init()
    return CommandService(store, FakeNotifier(), _CliState())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zoidberg — Planet Express canary auto-updater")
    parser.add_argument("--dry-run", action="store_true", help="Report what would update, touch nothing")
    parser.add_argument(
        "--force", action="store_true",
        help="run a real update pass even while casa-planetexpress is active (bypasses its mutation lock)",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.force and bender.core_service_active():
        print(
            "casa-planetexpress is running (or its state couldn't be checked), and its "
            "host-mutation lock can't see this CLI, so a "
            "real update pass could collide with a plan, restart or scan in progress. Use "
            "Telegram /patchnow, run with --dry-run, or pass --force.",
            file=sys.stderr,
        )
        return 2

    config.ensure_dirs()
    out = run_update_pass(tg=None, dry_run=args.dry_run,
                          commands=None if args.dry_run else _cli_command_service())
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    sys.exit(main())
