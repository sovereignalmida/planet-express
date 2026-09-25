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
import fnmatch
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
from datetime import datetime, timezone

import config
from planet_express.core.redact import redact

log = logging.getLogger("planetexpress.bender")

# ── Safety constants ──────────────────────────────────────────────────────────
# Commands containing these strings require network_confirm flag in the plan
NETWORK_GUARD_TOKENS = ["CASA_TRAEFIK", "CASA_ADGUARD", "adguard", "traefik"]

# These stack names must NEVER be touched — single source of truth in config.py,
# imported here rather than kept as a local copy that could drift.
FORBIDDEN_STACKS = config.FORBIDDEN_STACKS

COMMAND_TIMEOUT_SECONDS = 120

# Config-declared allowlist of sudo-scoped systemctl actions — single source of truth in config.py,
# same pattern as FORBIDDEN_STACKS above. Empty by default; a fresh install grants nothing until the
# operator declares it (and grants it at the OS level via sudoers.d) explicitly.
SUDO_ALLOWLIST = config.SUDO_ALLOWLIST

# Anything other than `sudo systemctl <action> <unit>` was never a legitimate use of the sudo grant
# this project asks for (docker needs no sudo — direct socket access).
#
# Deliberately requires a literal, bare `sudo`, and a strict systemd-unit-name character class for
# the unit. Both were Codex findings from the days when a plan step was a shell string: with
# `shell=True`, `$(sudo mount -a)/sudo systemctl restart casa-stacks.service` satisfied a
# path-prefix-tolerant regex, and `$(sudo${IFS}mount${IFS}-a)data.mount` satisfied a `\S+` unit
# group *and* `fnmatch("*.mount")`, while the shell executed the embedded command substitution.
# There is no shell left to exploit (slice 5b-5) — this stays strict anyway, because the allowlist
# is the thing that decides what `sudo` may do, and it should not depend on how it is called.
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


# The shell control operators that separate one command from another. Defined once and
# shared by _split_command_segments (which checks each piece independently, for legacy
# plans) and run_diagnostic's one-command-per-call rule -- three copies of this set would
# have to agree forever, and wouldn't.
_SHELL_CONTROL_OPERATOR_RE = re.compile(r"&&|\|\||;|\||\n")


def _split_command_segments(command: str) -> list[str]:
    """Split a compound shell command on control operators so each piece can be
    checked independently — otherwise a legitimate `sudo systemctl start x.mount &&
    sudo rm -rf /` could smuggle a forbidden second command past a whole-string check.
    Newlines split too: `_run_command()` runs everything with shell=True, and bash
    treats a newline as a statement separator exactly like `;`."""
    return [seg.strip() for seg in _SHELL_CONTROL_OPERATOR_RE.split(command) if seg.strip()]


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

    Deliberately checks for the word `sudo` *anywhere* in the segment, not just at the start --
    a wrapper like `env sudo mount -a` or `sh -c 'sudo mount -a'` would bypass a prefix-only check
    by never technically "starting with sudo" (a real gap an independent Codex review caught before
    it shipped, back when these strings reached a shell)."""
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


# ── Argv runner (typed actions + verifier) ────────────────────────────────────
# The runner everything goes through — there is no other way to run a command in this project
# since slice 5b-5. The command is an argv list that never passes through a shell, so no part of it
# can be reinterpreted as `&&`, `$(...)` or a redirect. The child also gets a minimal environment:
# casa-planetexpress's own environment carries the LLM API key and the Telegram bot token, and
# nothing Bender runs needs either.
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


def run_argv_bounded(argv: list[str], timeout: int, max_bytes: int) -> tuple[int, str, str, bool]:
    """run_argv with a memory bound: keep only the NEWEST `max_bytes` of each stream.

    `run_argv` uses capture_output, which buffers everything a command writes before the caller
    can cap it. For `docker logs --tail 500` that is 500 records of unbounded size, polled every
    few seconds from the dashboard, so one container writing huge records could exhaust core's
    memory (Codex review, T29). Each stream is drained by its own thread into a rolling buffer; if
    a stream overflows, the partial first line is dropped and `truncated` is True.

    Same argv rules, minimal environment and exit conventions as run_argv (124 timeout, 127
    missing executable, 126 other launch failure). Output is NOT stripped, so callers see
    trailing content exactly as written. Returns (returncode, stdout, stderr, truncated)."""
    if not isinstance(argv, list):
        raise TypeError(f"run_argv_bounded takes an argv list, got {type(argv).__name__}")
    if not argv:
        raise ValueError("run_argv_bounded needs a non-empty argv list")
    if not all(isinstance(part, str) for part in argv):
        raise TypeError("every run_argv_bounded argument must be a str")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")

    env = {key: os.environ[key] for key in _RUN_ARGV_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", _RUN_ARGV_DEFAULT_PATH)
    try:
        proc = subprocess.Popen(
            argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: executable not found", False
    except OSError as e:
        return 126, "", f"{argv[0]}: {e}", False

    captured: dict[str, bytes] = {}
    overflowed: dict[str, bool] = {}

    def drain(name, stream):
        tail = bytearray()
        over = False
        try:
            while chunk := stream.read(65536):
                tail += chunk
                if len(tail) > max_bytes:
                    del tail[: len(tail) - max_bytes]
                    over = True
        finally:
            stream.close()
        captured[name], overflowed[name] = bytes(tail), over

    readers = [threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True),
               threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for reader in readers:
            reader.join(timeout=1)
        return RUN_ARGV_TIMEOUT_EXIT, "", f"{argv[0]} timed out after {timeout}s", False
    for reader in readers:
        reader.join()

    def text(name):
        value = captured.get(name, b"").decode("utf-8", errors="replace")
        if overflowed.get(name) and "\n" in value:
            value = value.split("\n", 1)[1]   # drop the partial first line
        return value

    return returncode, text("stdout"), text("stderr"), any(overflowed.values())


CORE_SERVICE_UNIT = "casa-planetexpress.service"
# `systemctl is-active` exit codes that mean "definitely not running", confirmed on the
# test homelab (systemd 255): 3 = inactive or failed, 4 = no such unit.
_SYSTEMCTL_NOT_RUNNING_EXIT_CODES = (3, 4)


def core_service_active() -> bool:
    """True if the Planet Express core service is running on this host. The mutating CLI
    entry points (casa_zoidberg, casa_stackctl) refuse while it is, because they run
    outside its in-process host-mutation lock (landing 1b)."""
    rc, _out, _err = run_argv(["systemctl", "is-active", "--quiet", CORE_SERVICE_UNIT], timeout=10)
    if rc == 0:
        return True
    if rc in _SYSTEMCTL_NOT_RUNNING_EXIT_CODES:
        return False
    # Fail closed: a timeout (124), a missing systemctl (127) or any other unexpected exit
    # means we can't tell, and guessing "not running" would let the CLI collide with a
    # mutation in progress (Codex review, landing 1b).
    log.warning(
        f"Could not determine whether {CORE_SERVICE_UNIT} is running "
        f"(systemctl is-active exit {rc}); treating it as running"
    )
    return True


# ── Log step to file ──────────────────────────────────────────────────────────
def _log_step(plan_id: str, step: dict, exit_code: int, stdout: str, stderr: str) -> None:
    try:
        config.ensure_dirs()
        log_file = config.LOG_DIR / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.log"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "plan_id": plan_id,
            "step_n": step.get("n"),
            "command": redact(step["command"]) if step.get("command") else step.get("command"),
            "exit_code": exit_code,
            "stdout": redact(stdout)[:2000],
            "stderr": redact(stderr)[:1000],
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


# One plain-language name per allowlisted prefix, for the Chat tab's evidence cards. The
# face of a card says what was checked; the argv lives behind the disclosure. Keyed on the
# same list above so adding a prefix without a label fails a test rather than shipping a
# card labelled "docker".
READONLY_DIAGNOSTIC_LABELS = {
    "docker inspect": "Container configuration",
    "docker logs": "Container log",
    "docker ps": "Container list",
    "journalctl": "System journal",
    "systemctl status": "Unit status",
    "systemctl is-active": "Unit running?",
    "systemctl is-enabled": "Unit enabled?",
    "df -h": "Disk usage",
    "df -i": "Inode usage",
}


def diagnostic_label(command: str) -> str:
    """The longest matching prefix wins: "systemctl is-active" must not be labelled with
    "systemctl status"'s name just because it was declared first."""
    lowered = (command or "").strip().lower()
    best = ""
    for prefix in READONLY_DIAGNOSTIC_LABELS:
        if lowered.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return READONLY_DIAGNOSTIC_LABELS[best] if best else "Read-only check"


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
    """Raise DiagnosticNotAllowed unless every segment of `command` starts with an allowlisted
    read-only prefix and names no forbidden stack. Segment-split first, so a compound command
    cannot smuggle something past a check that only looked at the first segment.

    The forbidden-stack rule used to live in `_safety_check`, which slice 5b-5 deleted along with
    the shell executor. It is kept here deliberately: "never touch these stacks" was always meant
    to include not reading their logs, and losing it silently with the pattern list would have been
    a policy change smuggled in as a refactor.
    """
    if _DIAGNOSTIC_SHELL_METACHAR_RE.search(command):
        raise DiagnosticNotAllowed(
            f"Diagnostic command contains shell redirection/substitution/background "
            f"operators, which are never allowed: '{command}'"
        )
    for segment in _split_command_segments(command):
        seg_lower = segment.lower()
        for stack in FORBIDDEN_STACKS:
            if re.search(rf"\b{re.escape(stack)}\b", segment, re.IGNORECASE):
                raise DiagnosticNotAllowed(f"Forbidden stack referenced: '{stack}'")
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

    # Checked AFTER the per-segment allowlist above, deliberately: `docker ps && rm -rf /`
    # must still fail on `rm -rf /` not being allowlisted, which is the guarantee
    # test_denies_sudo_smuggled_via_compound_command asserts. Only a compound whose every
    # segment is already allowlisted reaches here.
    operator = _SHELL_CONTROL_OPERATOR_RE.search(command)
    if operator:
        name = "newline" if operator.group() == "\n" else operator.group()
        raise DiagnosticNotAllowed(
            f"Diagnostic command contains shell control operator {name!r}; "
            f"only one command per call is allowed: '{command}'"
        )


def run_diagnostic(command: str) -> tuple[int, str, str]:
    """Run a single read-only diagnostic command for Farnsworth's pre-planning tool loop.

    Its guard is the read-only allowlist — an allowlist, and so strictly stronger than the pattern
    blocklist that used to back it up — plus the sudo scope check. Raises SafetyError (never runs
    the command) if either fails; callers decide how to surface that to the model.
    """
    _check_readonly_diagnostic(command)
    _check_sudo_allowlist(command)
    argv = shlex.split(command)
    if not argv:
        raise DiagnosticNotAllowed("Diagnostic command must not be empty")
    exit_code, stdout, stderr = run_argv(argv, timeout=COMMAND_TIMEOUT_SECONDS)
    # Bound retained diagnostic output after redaction, including literal secrets
    # spanning the cutoff. subprocess.run still captures the full output first;
    # a true streaming memory bound is out of scope here.
    stdout, stderr = redact(stdout)[:65536], redact(stderr)[:65536]
    _log_step("diagnostic", {"n": None, "command": command}, exit_code, stdout, stderr)
    return exit_code, stdout, stderr


# ── Safe prune ─────────────────────────────────────────────────────────────────
# Called only by Farnsworth's maybe_run_safe_prune(), which has already verified disk
# pressure is real and every container is in a known-safe state. Deliberately narrow:
# only image/network prune, never "docker system prune". Argv, not command strings: there is no
# shell left to split them (slice 5b-5).
SAFE_PRUNE_STEPS = [
    ("image prune", ["docker", "image", "prune", "-a", "-f"]),
    ("network prune", ["docker", "network", "prune", "-f"]),
]


STACKS_ROOT = config.STACKS_ROOT


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


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(description="Bender — Planet Express executor")
    parser.add_argument("--diagnostic", metavar="COMMAND",
                        help="run one read-only diagnostic command through the allowlist")
    args = parser.parse_args()

    if not args.diagnostic:
        parser.error("nothing to run here: a mutation is a typed step in an approved runbook, run "
                     "by the engine (docs/designs/slice-5b-multistep-execution.md)")
    try:
        exit_code, stdout, stderr = run_diagnostic(args.diagnostic)
    except SafetyError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"exit_code": exit_code, "stdout": stdout, "stderr": stderr}, indent=2))
    sys.exit(0 if exit_code == 0 else 1)
