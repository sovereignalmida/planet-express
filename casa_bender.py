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
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
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
# casa-stacks.service` satisfied that prefix, letting the whole string through
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


class SudoScopeError(SafetyError):
    """A sudo command was outside the declared allowlist — distinct from other
    safety blocks so callers can show a clearer "run it yourself" message instead
    of a generic hard-fail (approving in chat could never make it actually run,
    since Bender executes headlessly with no TTY for a password prompt).

    action/unit are set only when the command parsed as `sudo systemctl <action>
    <unit>` but that (action, unit) pair just isn't declared -- the only shape
    /grant can turn into a concrete config.yaml suggestion. A command that didn't
    even parse that way (arbitrary sudo) has neither, since there's no unit to
    suggest a grant for -- this project's sudo mechanism only ever covers
    systemctl start/stop/restart on a declared unit."""

    def __init__(self, message: str, command: str, action: str | None = None, unit: str | None = None):
        super().__init__(message)
        self.command = command
        self.action = action
        self.unit = unit


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
            raise SudoScopeError(
                f"Sudo command not in the declared allowlist (only 'sudo systemctl "
                f"start|stop|restart <unit>' can ever be permitted): '{segment}'",
                command=segment,
            )
        action, unit = m.group(1), m.group(2)
        if not _sudo_action_allowed(unit, action):
            raise SudoScopeError(
                f"Sudo action '{action}' on '{unit}' is not declared in "
                f"config.yaml's sudo_allowlist: '{segment}'",
                command=segment,
                action=action.lower(),
                unit=unit,
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
            check=False,      # returncode inspected by the caller, not raised
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


# ── Argv runner (typed actions + verifier) ────────────────────────────────────
# The runner every typed action and the verifier go through. Unlike _run_command
# (shell=True, kept only for legacy LLM-written plans until slice 5 retires them), the
# command is an argv list that never passes through a shell, so no part of it can be
# reinterpreted as `&&`, `$(...)` or a redirect. The child also gets a minimal
# environment: casa-planetexpress's own environment carries the LLM API key and the
# Telegram bot token, and nothing Bender runs needs either.
RUN_ARGV_TIMEOUT_EXIT = 124  # same convention as coreutils timeout(1)
# Non-secret variables only. The Docker ones are connection settings the docker CLI
# needs to reach the same daemon the shell=True commands always inherited: a custom
# DOCKER_HOST/DOCKER_CONTEXT or a rootless socket found via XDG_RUNTIME_DIR. Dropping them
# would point health checks at the default socket, report healthy containers as missing,
# and trigger false rollbacks (found by Codex review, landing 1a).
_RUN_ARGV_ENV_KEYS = (
    "PATH", "HOME", "LANG",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY", "DOCKER_API_VERSION", "XDG_RUNTIME_DIR",
    # ssh:// Docker hosts/contexts authenticate through the agent; without its socket every
    # docker call to a remote daemon fails (Codex review, landing 1a).
    "SSH_AUTH_SOCK",
)
_RUN_ARGV_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def run_argv(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run argv without a shell; return (returncode, stdout, stderr), output stripped.

    A string is rejected rather than split: callers must build argv themselves, so a
    stray shell-style command string can never be quietly accepted. Timeout returns
    RUN_ARGV_TIMEOUT_EXIT (124), a missing executable 127, and any other OS-level launch
    failure 126, all with a human-readable stderr, never an exception."""
    if not isinstance(argv, list):
        raise TypeError(f"run_argv takes an argv list, got {type(argv).__name__}")
    if not argv:
        raise ValueError("run_argv needs a non-empty argv list")
    if not all(isinstance(part, str) for part in argv):
        raise TypeError("every run_argv argument must be a str")

    env = {key: os.environ[key] for key in _RUN_ARGV_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", _RUN_ARGV_DEFAULT_PATH)
    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,  # returncode inspected by the caller, not raised
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return RUN_ARGV_TIMEOUT_EXIT, "", f"{argv[0]} timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: executable not found"
    except OSError as e:
        return 126, "", f"{argv[0]}: {e}"


# ── Log step to file ──────────────────────────────────────────────────────────
def _log_step(plan_id: str, step: dict, exit_code: int, stdout: str, stderr: str) -> None:
    try:
        config.ensure_dirs()
        log_file = config.LOG_DIR / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.log"
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
    except Exception as e:  # noqa: BLE001
        log.warning(f"Failed to write step log: {e}")


# ── Read-only diagnostics (pre-planning) ──────────────────────────────────────
# Called by Farnsworth's diagnostic tool loop (casa_farnsworth.py) *before* a plan
# is written, so plans can be based on real state (e.g. "is gluetun's forward
# actually dead, or just out of sync") instead of a worst-case guess. Fail closed
# on an allowlist of read-only command prefixes -- the inverse of _safety_check's
# default-allow posture, appropriate here because this path runs fully unattended,
# before any human has seen or approved anything. Routing diagnostics through
# Bender (rather than giving Farnsworth its own shell access) keeps the README's
# "only Bender ever touches the host" invariant true even for commands that never
# become part of an approved plan.
READONLY_DIAGNOSTIC_PREFIXES = [
    "docker inspect",
    "docker logs",
    "docker ps",
    "journalctl",
    "systemctl status",
    "systemctl is-active",
    "systemctl is-enabled",
    "df -h",
    "df -i",
]


class DiagnosticNotAllowed(SafetyError):
    """A diagnostic tool call didn't match the read-only allowlist."""


# _split_command_segments only recognizes &&, ||, ;, |, and newline as separators
# (that's all _safety_check's sudo-smuggling defense ever needed). It does NOT
# recognize redirection (>, >>, <), command substitution ($(...) or backticks), or
# a lone job-control `&` -- all of which shell=True still happily executes as a
# *second* command/side effect tacked onto an otherwise-allowlisted one (e.g.
# `docker ps > config.yaml` or `docker ps & rm -rf /`). Caught by an independent
# Codex review before this shipped -- reject them outright rather than trying to
# split on them too, since diagnostics never legitimately need any of them.
_DIAGNOSTIC_SHELL_METACHAR_RE = re.compile(r"[<>`]|\$\(|(?<!&)&(?!&)")

# journalctl's read-only-looking bare prefix still exposes flags that mutate
# journal state on disk (rotate/vacuum/flush/etc.) or that never terminate
# (--follow), which would hang planning until the command timeout. Rather than
# denylist individual mutating/blocking flags (GNU getopt accepts unambiguous
# abbreviations like `--rot` for `--rotate`, which a substring denylist can't
# catch), allowlist the flags a real diagnostic read ever needs and reject
# anything else outright.
# Presence-only checks for --tail/-n would still accept `--tail 999999999999`,
# so cap the actual bound to something a diagnostic call has any real use for.
_DIAGNOSTIC_MAX_LINES = 2000

_JOURNALCTL_SAFE_FLAGS = {
    "-u", "--unit", "-n", "--lines", "-o", "--output", "-p", "--priority",
    "-b", "--boot", "-k", "--dmesg", "-g", "--grep", "-e", "--pager-end",
    "-x", "--catalog", "--no-pager", "--since", "--until", "--user",
    "--system", "-r", "--reverse",
}

# docker inspect --format is Go template syntax, which is expressive enough
# (index/call/range/with, nested navigation, functions) that trying to denylist
# every way to spell "give me .Config.Env anyway" is a losing game -- e.g.
# `{{json (index . "Config")}}` dumps the whole Config object (Env included)
# without the literal substrings ".config" or "env" ever appearing (a real gap an
# independent Codex review caught before this shipped). Allowlist the handful of
# exact, known-safe field paths a diagnostic ever legitimately needs instead: the
# actual --format/-f value (extracted via shlex, not a raw-string scan) must
# match this exactly, or the whole command is rejected.
_DOCKER_INSPECT_SAFE_FIELD_RE = re.compile(
    r"^\{\{\s*(json\s+)?\."
    r"(State(\.[A-Za-z]+){1,3}"
    r"|Name|Id|Created|RestartCount|Image"
    r"|NetworkSettings\.Networks\.[\w.\-]+\.(IPAddress|Gateway|MacAddress))"
    r"\s*\}\}$"
)


def _flag_value(tokens: list[str], flag_names: tuple[str, ...]) -> str | None:
    """Return the value passed to the LAST `--flag value` or `--flag=value`
    occurrence in `tokens` (like getopt-based CLIs, later occurrences of a flag
    win), or None if the flag isn't present at all. Used to validate the *value*
    of a bounding/field-selecting flag, not just whether the flag was typed --
    `--tail all`/`-n -1` would otherwise pass a presence-only check while still
    letting the command dump unbounded output, and only checking the *first*
    occurrence of a repeated flag (e.g. two `--format`s) would let a safe decoy
    value hide the real, unsafe one docker actually uses."""
    value = None
    for i, token in enumerate(tokens):
        if "=" in token:
            name, _, val = token.partition("=")
            if name in flag_names:
                value = val
        elif token in flag_names and i + 1 < len(tokens):
            value = tokens[i + 1]
    return value


def _check_readonly_diagnostic(command: str) -> None:
    """Raise DiagnosticNotAllowed unless every segment of `command` starts with an
    allowlisted read-only prefix. Segment-split first (same helper _safety_check
    uses for the same reason) so a compound command can't smuggle a mutating
    command past a check that only looked at the first segment."""
    if _DIAGNOSTIC_SHELL_METACHAR_RE.search(command):
        raise DiagnosticNotAllowed(
            f"Diagnostic command contains shell redirection/substitution/background "
            f"operators, which are never allowed: '{command}'"
        )
    for segment in _split_command_segments(command):
        seg_lower = segment.lower()
        if not any(seg_lower.startswith(prefix) for prefix in READONLY_DIAGNOSTIC_PREFIXES):
            raise DiagnosticNotAllowed(
                f"Diagnostic command not in the read-only allowlist: '{segment}'"
            )
        if seg_lower.startswith("docker inspect"):
            # Bare `docker inspect` dumps the full container config, including
            # Config.Env -- which routinely holds passwords/API keys and would
            # otherwise get shipped straight to the external LLM provider and
            # written into the diagnostic log. Require a --format that doesn't
            # touch Env/Config/the whole object so the model can only ever pull
            # narrow, non-secret fields.
            #
            # Extract the value via shlex (proper POSIX quote parsing), not a
            # regex scan of the raw string -- a regex scanning raw characters can
            # be fooled by adjacent-quote concatenation tricks (e.g. splitting the
            # literal "{{" across two quoted pieces) that shlex reassembles the
            # same way a real shell would. If --format/-f is repeated, docker uses
            # the LAST value, so _flag_value must (and does) return the last one
            # too -- a safe first value can't be used as a decoy for an unsafe one.
            docker_tokens = shlex.split(segment)
            fmt_value = _flag_value(docker_tokens, ("--format", "-f"))
            if fmt_value is None:
                raise DiagnosticNotAllowed(
                    f"docker inspect must use --format to select specific fields (bare "
                    f"inspect dumps the full config, including Config.Env secrets): "
                    f"'{segment}'"
                )
            if not _DOCKER_INSPECT_SAFE_FIELD_RE.match(fmt_value.strip()):
                raise DiagnosticNotAllowed(
                    f"docker inspect --format must be one of a small set of known-safe "
                    f"field paths (State.*, Name, Id, Created, RestartCount, Image, "
                    f"NetworkSettings.Networks.<net>.IPAddress/Gateway/MacAddress) -- "
                    f"arbitrary Go-template expressions, including anything reaching "
                    f"into Config (which holds Env/secrets), are rejected: '{segment}'"
                )
        if seg_lower.startswith("docker logs"):
            tokens = shlex.split(segment)[2:]
            if re.search(r"(^|\s)(-f|--follow)(=|\s|$)", seg_lower):
                # Equals-form (`--follow=true`, `-f=true`) doesn't change that it's
                # still the follow flag -- catch it before the value check below,
                # since docker treats `--follow=false` the same as omitting it.
                raise DiagnosticNotAllowed(
                    f"docker logs --follow never terminates -- not allowed for a bounded "
                    f"diagnostic call: '{segment}'"
                )
            tail_value = _flag_value(tokens, ("--tail",))
            if (
                tail_value is None
                or not re.fullmatch(r"\d+", tail_value)
                or int(tail_value) > _DIAGNOSTIC_MAX_LINES
            ):
                raise DiagnosticNotAllowed(
                    f"docker logs must use --tail with an integer between 1 and "
                    f"{_DIAGNOSTIC_MAX_LINES} to bound output ('all', a negative value, "
                    f"omitting it, or an unreasonably large value all risk huge memory "
                    f"use and blocking the planning pipeline): '{segment}'"
                )
        if seg_lower.startswith("journalctl"):
            tokens = shlex.split(segment)[1:]
            for token in tokens:
                if not token.startswith("-"):
                    continue  # positional arg (e.g. a filter expression), not a flag
                flag = token.split("=", 1)[0]
                if flag not in _JOURNALCTL_SAFE_FLAGS:
                    raise DiagnosticNotAllowed(
                        f"journalctl flag '{flag}' is not in the read-only allowlist "
                        f"(covers state-mutating flags like --rotate/--vacuum and "
                        f"never-terminating ones like --follow): '{segment}'"
                    )
            lines_value = _flag_value(tokens, ("-n", "--lines"))
            if (
                lines_value is None
                or not re.fullmatch(r"\d+", lines_value)
                or int(lines_value) > _DIAGNOSTIC_MAX_LINES
            ):
                raise DiagnosticNotAllowed(
                    f"journalctl must use -n/--lines with an integer between 1 and "
                    f"{_DIAGNOSTIC_MAX_LINES} to bound output ('all', a negative value, "
                    f"omitting it, or an unreasonably large value all risk huge memory "
                    f"use and blocking the planning pipeline): '{segment}'"
                )


def run_diagnostic(command: str) -> tuple[int, str, str]:
    """Run a single read-only diagnostic command for Farnsworth's pre-planning tool
    loop. Enforces the read-only allowlist AND the normal _safety_check (forbidden
    commands/stacks, sudo scope) as defense in depth, then executes and logs it
    exactly like a real plan step. Raises SafetyError (never runs the command) if
    either check fails -- callers decide how to surface that to the model."""
    _check_readonly_diagnostic(command)
    _safety_check(command, plan={})
    exit_code, stdout, stderr = _run_command(command)
    _log_step("diagnostic", {"n": None, "command": command}, exit_code, stdout, stderr)
    return exit_code, stdout, stderr


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
# Every propose/discard/apply call does a read-modify-write of the same file. Each
# Telegram command runs in its own daemon thread, so without this lock two concurrent
# calls (e.g. two /install proposals) could race and silently drop one's write.
# Reentrant because apply_pending_diff() calls get_pending_diff()/discard_pending_diff()
# (each of which also acquires this lock) from within its own held critical section.
_PENDING_DIFFS_LOCK = threading.RLock()


def _load_pending_diffs() -> dict:
    if not PENDING_DIFFS_FILE.exists():
        return {}
    try:
        return json.loads(PENDING_DIFFS_FILE.read_text())
    except Exception:  # noqa: BLE001
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


def propose_compose_diff(stack_name: str, new_content: str, reason: str, is_new_stack: bool = False) -> dict:
    """Propose a diff to a stack's docker-compose.yml. Writes nothing to the real
    file — only records the proposal and returns a unified diff for a human to
    review in Telegram. Raises SafetyError for forbidden stacks or any path that
    isn't exactly stacks/<name>/docker-compose.yml.

    is_new_stack must be explicitly passed True by a caller that intends to onboard a
    brand-new stack (Fry) — it is NOT inferred from the file's absence. Defaulting to
    False preserves the original behavior for ordinary compose edits (Amy): if the file
    is missing when an edit was expected, that's raised as an error, not silently
    reinterpreted as "create a new stack" (e.g. if the file was deleted between Amy
    reading the current block and proposing her edit)."""
    if stack_name in FORBIDDEN_STACKS:
        raise SafetyError(f"Refusing to propose a diff for forbidden stack '{stack_name}'")

    compose_path = STACKS_ROOT / stack_name / "docker-compose.yml"
    if is_new_stack:
        if compose_path.is_file():
            raise SafetyError(
                f"'{compose_path}' already exists — refusing to treat this as a new-stack "
                f"proposal; propose an edit against the current file instead"
            )
    elif not compose_path.is_file():
        raise SafetyError(f"No docker-compose.yml for stack '{stack_name}' at {compose_path}")

    # Only enforced for brand-new stacks (onboarding): existing stacks (Amy's normal
    # compose-edit flow) may legitimately have names with characters this doesn't allow
    # (underscores, uppercase, etc) — those are still safe because they already resolve
    # to a real, existing path under STACKS_ROOT, checked below regardless. A new-stack
    # name has no existing path to anchor it, so it's restricted to a safe character set
    # up front rather than trusting callers (e.g. a Telegram-derived domain slug) to have
    # already sanitized it.
    if is_new_stack and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", stack_name):
        raise SafetyError(f"Invalid stack name '{stack_name}' — must be lowercase alphanumeric/hyphen only")

    # Belt-and-suspenders regardless of new vs. existing: if STACKS_ROOT/<stack_name> is
    # ever a symlink to somewhere else, resolve() would follow it — verify the real,
    # resolved destination is still under STACKS_ROOT before trusting it as the target of
    # a diff that apply_pending_diff will later write to.
    resolved_root = STACKS_ROOT.resolve()
    resolved_target = compose_path.parent.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise SafetyError(f"Stack directory for '{stack_name}' resolves outside STACKS_ROOT — refusing")

    old_content = compose_path.read_text() if not is_new_stack else ""
    if old_content == new_content:
        raise SafetyError("Proposed content is identical to the current file — nothing to diff")

    diff_text = "".join(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"{stack_name}/docker-compose.yml (current)",
        tofile=f"{stack_name}/docker-compose.yml (proposed)",
    ))

    with _PENDING_DIFFS_LOCK:
        diff_id = f"diff-{stack_name}-{int(time.time())}"
        pending = _load_pending_diffs()
        if diff_id in pending:
            # Two proposals for the same stack within the same second — make the ID
            # unique rather than let the second silently overwrite the first. A short
            # fixed-length (4 hex char) suffix, not a thread ident (which can run to 15+
            # digits) — diff_id is embedded verbatim in a Telegram inline-button
            # callback_data ("approve_diff:<diff_id>"), capped at 64 bytes total.
            diff_id = f"{diff_id}-{uuid.uuid4().hex[:4]}"
        pending[diff_id] = {
            "stack": stack_name,
            "compose_path": str(compose_path),
            "new_content": new_content,
            "reason": reason,
            "diff_text": diff_text,
            "is_new_stack": is_new_stack,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_pending_diffs(pending)
    log.info(f"Proposed diff {diff_id} for {stack_name}/docker-compose.yml: {reason}")
    return {"diff_id": diff_id, "diff_text": diff_text, "stack": stack_name}


def get_pending_diff(diff_id: str) -> dict | None:
    with _PENDING_DIFFS_LOCK:
        return _load_pending_diffs().get(diff_id)


def discard_pending_diff(diff_id: str) -> None:
    with _PENDING_DIFFS_LOCK:
        pending = _load_pending_diffs()
        pending.pop(diff_id, None)
        _save_pending_diffs(pending)


def apply_pending_diff(diff_id: str) -> dict:
    """Apply a previously-approved diff: back up the current file (.yml.bak.<ts>),
    then write the new content. Does not restart anything — that's still a
    separate, normal plan-approval step afterward."""
    with _PENDING_DIFFS_LOCK:
        return _apply_pending_diff_locked(diff_id)


def _apply_pending_diff_locked(diff_id: str) -> dict:
    entry = get_pending_diff(diff_id)
    if not entry:
        raise SafetyError(f"No pending diff found for '{diff_id}' (already applied or expired?)")

    compose_path = Path(entry["compose_path"])

    # Re-check containment at apply time, not just at proposal time: the proposal-time
    # check in propose_compose_diff() only proves the path was safe THEN. Between propose
    # and approve, STACKS_ROOT/<stack>/ could be replaced with a symlink pointing outside
    # STACKS_ROOT — is_file() would still read False for a symlinked dir with no compose
    # file inside it, and mkdir(exist_ok=True) doesn't care that the directory entry is a
    # symlink, so without this the write below would silently land outside STACKS_ROOT.
    resolved_root = STACKS_ROOT.resolve()
    resolved_target = compose_path.parent.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise SafetyError(f"'{compose_path.parent}' resolves outside STACKS_ROOT — refusing to apply")

    # compose_path itself could be a (possibly broken) symlink even when its parent
    # directory is fine — is_file() reads False for a broken symlink, so the new-stack
    # branch below would treat it as absent, and write_text() follows the link and
    # creates the real target wherever it points. Checking is_symlink() directly (rather
    # than relying on resolve(), which by design still "succeeds" for a broken link)
    # catches this even when the link's target doesn't exist yet to resolve.
    if compose_path.is_symlink():
        raise SafetyError(f"'{compose_path}' is a symlink — refusing to write through it")

    # entry["is_new_stack"] records whether the file was absent at proposal time. If it's
    # since appeared (another process/manual edit between propose and approve), applying
    # this stale new-stack proposal would silently clobber whatever showed up — refuse
    # instead of overwriting an unrelated, possibly manually-created file.
    if entry.get("is_new_stack") and compose_path.is_file():
        raise SafetyError(
            f"'{compose_path}' now exists but this diff was proposed when it was absent — "
            f"refusing to overwrite; discard this diff and re-propose against the current file"
        )

    # The mirror image of the check above: this was an ordinary edit of an existing file
    # (is_new_stack False/absent from older entries), but the file has since disappeared
    # (deleted, stack removed). Falling through to the "create it fresh" branch below
    # would silently resurrect a removed stack with no backup and no history — refuse
    # instead of treating a vanished existing file the same as a genuine new stack.
    if not entry.get("is_new_stack") and not compose_path.is_file():
        raise SafetyError(
            f"'{compose_path}' no longer exists but this diff was proposed as an edit to an "
            f"existing file — refusing to recreate it; discard this diff and re-propose if the "
            f"stack is meant to exist"
        )

    backup_path = None
    if compose_path.is_file():
        backup_path = compose_path.with_name(
            compose_path.name + f".bak.{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}"
        )
        backup_path.write_text(compose_path.read_text())
    else:
        compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(entry["new_content"])
    discard_pending_diff(diff_id)

    log.info(f"Applied diff {diff_id} to {compose_path} (backup: {backup_path})")
    return {
        "diff_id": diff_id,
        "compose_path": str(compose_path),
        "backup_path": str(backup_path) if backup_path else None,
    }


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
        except SudoScopeError as e:
            msg = str(e)
            log.error(f"Sudo scope block on step {n}: {msg}")
            config.ensure_dirs()
            config.LAST_SUDO_BLOCK_FILE.write_text(json.dumps({
                "plan_id": plan_id,
                "step": n,
                "command": e.command,
                "action": e.action,
                "unit": e.unit,
                "blocked_at": datetime.now(timezone.utc).isoformat(),
            }))
            # len(results), not n - 1: plan steps come straight from unvalidated LLM
            # JSON (PlanSet uses extra="allow"), so a malformed/non-contiguous `n`
            # shouldn't be trusted for "how many steps actually ran" -- results only
            # grows once per completed iteration, so its length before this append is
            # the real count regardless of what the step claims n is.
            steps_ran = len(results)
            if tg:
                rollback_note = (
                    f"\n\n⚠️ {steps_ran} earlier step(s) already ran before this block — "
                    f"the plan is partially applied. Reply /rollback {plan_id} to roll "
                    f"back or /skip {plan_id} to leave it as-is."
                    if steps_ran > 0 else ""
                )
                tg.send(
                    f"🔒 *Plan #{plan_id} step {n} needs sudo outside current scope*\n"
                    f"`{TelegramClient.s(e.command)}`\n\n"
                    f"Not in Bender's declared sudo allowlist. Approving here couldn't "
                    f"run it anyway — the OS sudo grant doesn't cover it either, so it "
                    f"would just hang on a password prompt until timeout. Run it "
                    f"yourself if you want it applied. Remaining steps were not run."
                    f"{rollback_note}\n\n"
                    f"To permanently allow this, send `/grant` for the exact lines to "
                    f"add to config.yaml and sudoers.d."
                )
            results.append({
                "n": n, "command": command,
                "exit_code": -1, "stdout_summary": "",
                "error": f"SUDO_SCOPE_BLOCK: {msg}",
            })
            return {
                "plan_id": plan_id,
                "steps_completed": steps_ran,
                "steps_total": total,
                "results": results,
                "final_status": "blocked_sudo",
                "errors": [f"Step {n}: sudo scope block"],
            }
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
        # Many failures (e.g. a probe script that echoes its diagnosis and exits 1)
        # signal via stdout with empty stderr — fall back to stdout so that message
        # isn't silently dropped from the error summary shown to the user/Amy.
        error_summary = (stderr or stdout)[:300] if not ok else ""

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
