"""The hook layer (contract 12, trust mode): the reference adapter's pre/post tool hooks.

A hook sees one tool call (a tool name and its arguments) BEFORE it runs and answers `allow` or `block`; after it runs it
records the outcome. Both directions go on the mission's record (every tool call is there). Trust mode
(docs/designs/v4-trust-mode.md): a session has real access, the same as the operator at a terminal, so there is no `ask`, no
protected path and no "irreversible" class. What is left is a short list of truly catastrophic actions, the WHOLE list:

    wipe      wiping a filesystem or disk: `mkfs*`, `wipefs`, `dd` to a block device, a recursive `rm` of `/`, `/etc`,
              `/home`, `/var/lib/docker`, `/boot`, `/usr` or the stacks root
    docker    deleting Docker data: `docker volume rm|prune`, `docker system prune -a|--all|--volumes`, any `prune -a`,
              `compose down -v|--volumes`
    secret    reading PE's own secrets: `/etc/planetexpress`, `/run/planetexpress`, `/proc/<pid>/environ`, dumping the
              environment (the session's holds its broker access code and PE's settings)
    pe        changing PE itself: its config, its sudoers files (`/etc/sudoers.d/planetexpress*`), its hook files, its
              database, its installed programs. This keeps the record honest.

Everything else is allowed: `docker restart X`, `docker compose up -d`, `sudo systemctl restart x`, reading a stack's `.env`.
A block is still caught through `sudo`, `env`, `xargs`, `sh -c` and the other wrappers below.

THESE HOOKS ARE A SPEED BUMP, NOT THE BOUNDARY. They are a pattern match over a command line that a determined agent can
route around (an interpreter, an encoding, a different binary), and with sudo that is accepted. They exist to catch mistakes;
the full record, the checkpoint and PE's own verification answer for the rest.

The decision depends on the tool name, its arguments and the policy given at construction, and on nothing else: not the
transcript, not the process environment, not anything the agent wrote. A hostile transcript has no input to this function.

Failure mode (configured, default fail closed for blocked classes, fail open with a loud event otherwise): when the hook
itself cannot decide (the input is unreadable, the classifier raises), the raw input is screened for ANY blocked-class marker;
if it has one, or cannot be read at all, `blocked_failure` applies (default `closed`: block), otherwise `other_failure`
applies (default `open`: allow, with a loud event so the miss is on the record).

STDLIB ONLY: Core copies this file into each session's hook directory as `pe_hook.py` at every launch and the runtime runs
that copy as a plain script (`install_claude_code`), so it must import nothing from planet_express.
"""

import json
import os
import posixpath
import re
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

Verdict = Literal["allow", "block"]
BLOCKED_CLASSES = ("wipe", "docker", "secret", "pe")
MAX_COMMAND_CHARS = 65536
MAX_INPUT_BYTES = 1 << 20          # the most of a hook's stdin that is read and parsed (a large Write or heredoc is legitimate)
MAX_TOKENS = 512
EVENT_TEXT = 400


class Decision(NamedTuple):
    verdict: Verdict
    cls: str            # one of BLOCKED_CLASSES, "failure" or ""
    reason: str


ALLOW = Decision("allow", "", "")
SHELL_TOOLS = frozenset({"bash", "shell", "run", "exec", "command", "terminal"})


@dataclass(frozen=True)
class HookPolicy:
    """What the hard blocks need, built by Core (`policy_for`), never by the agent: the failure modes, the stacks root (a
    recursive `rm` of it is a wipe) and the paths of THIS install of PE that a session must not change (its config file, its
    database, its hook root), on top of the fixed ones in `_PE_OWN`."""
    blocked_failure: Literal["closed", "open"] = "closed"
    other_failure: Literal["closed", "open"] = "open"
    stacks_root: str = ""
    pe_paths: Sequence[str] = ()

    def to_json(self) -> str:
        return json.dumps({"blocked_failure": self.blocked_failure, "other_failure": self.other_failure,
                           "stacks_root": self.stacks_root, "pe_paths": list(self.pe_paths)})

    @classmethod
    def from_json(cls, text: str) -> "HookPolicy":
        """Unknown keys are ignored: a pre-trust-mode policy (`live_trees`, `protected_paths`, `docker_lifecycle`) left in a
        stale environment or policy file still loads, it just no longer decides anything."""
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("hook policy must be an object")
        bf, of = raw.get("blocked_failure", "closed"), raw.get("other_failure", "open")
        if bf not in ("closed", "open") or of not in ("closed", "open"):
            raise ValueError("failure modes are closed or open")
        stacks, pe_paths = raw.get("stacks_root", ""), raw.get("pe_paths", [])
        if not isinstance(stacks, str) or not isinstance(pe_paths, list) or not all(isinstance(p, str) for p in pe_paths):
            raise ValueError("stacks_root is a string and pe_paths a list of strings")
        return cls(bf, of, stacks, tuple(pe_paths))


# ── PE's own secrets and PE's own files ──────────────────────────────────────────────────────────────

_PE_SECRET = ("/etc/planetexpress", "/run/planetexpress")
# Fixed paths that are PE itself (a session that changed them would change the rules it runs under, or the record): its config
# directory, its launcher and launch.conf, its per-session hook files and installed library, and (below) its sudoers files and
# databases. Core adds this install's config file, database and hook root through the policy.
_PE_OWN = ("/etc/planetexpress", "/etc/planetexpress-launch", "/usr/libexec/planetexpress", "/usr/local/lib/planetexpress",
           "/var/lib/planetexpress/mission-hooks", "/run/planetexpress")
_PE_DATA = "/var/lib/planetexpress"
_DB_NAME = re.compile(r".*\.(db|sqlite3?)(-wal|-shm|-journal)?$")
_SUDOERS_D = "/etc/sudoers.d"
_READERS_HARMLESS = {"ls", "stat", "test", "[", "file", "wc", "find"}      # name the file, never its contents
# Programs that read the paths they are given and write none of them. Anything else that names a path of PE's own counts as
# changing it (a false block is a mistake caught; the agent can say so in its report).
_READERS = _READERS_HARMLESS | {"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "zcat", "zgrep", "diff",
                                "cmp", "sha256sum", "sha1sum", "md5sum", "b2sum", "jq", "yq", "readlink", "realpath", "du",
                                "tree", "namei", "lsattr", "getfacl", "strings", "hexdump", "xxd", "od", "nl", "sort", "uniq",
                                "cut", "awk"}
# Programs whose LAST positional argument (or `-t DIR`) is the only one they write: the others are read.
_COPIERS = {"cp", "rsync", "scp", "install", "ln"}


def _within(path: str, root: str) -> bool:
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _is_pe_secret(path: str) -> bool:
    norm = posixpath.normpath(path.replace("\\", "/"))
    if re.fullmatch(r"/proc/[^/]+(/task/[^/]+)?/environ", norm):
        return True
    return any(_within(norm, root) for root in _PE_SECRET)


def _is_pe_own(path: str, policy: HookPolicy) -> bool:
    norm = posixpath.normpath(path.replace("\\", "/"))
    if any(_within(norm, root) for root in _PE_OWN):
        return True
    if posixpath.dirname(norm) == _SUDOERS_D and posixpath.basename(norm).startswith("planetexpress"):
        return True
    if _within(norm, _PE_DATA) and _DB_NAME.fullmatch(posixpath.basename(norm)):
        return True
    for own in policy.pe_paths:
        own = posixpath.normpath(own)
        if _within(norm, own) or (_DB_NAME.fullmatch(posixpath.basename(own)) and norm.startswith(own + "-")):
            return True
    return False


def _dumps_environment(low: str, toks: Sequence[str]) -> bool:
    """`printenv` (it prints a named variable's value too), a bare `env`/`set`, and `export`/`declare`/`typeset` with no names
    (their listing forms: no arguments, `-p`, `-x`). An assignment such as `export FOO=1` is not a dump."""
    if low == "printenv":
        return True
    if low in ("env", "set"):
        return len(toks) == 1
    if low in ("export", "declare", "typeset"):
        return all(t.startswith("-") for t in toks[1:])
    return False


# ── a command line, tokenised ─────────────────────────────────────────────────────────────────────────

_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)")


def _split_top(command: str) -> list[str]:
    """Split a command line at `&&`, `||`, `;`, `|`, newline and a lone `&`, but only OUTSIDE quotes (so a quoted script stays in
    one piece for the nested-shell reader). `&` that belongs to `2>&1`, `&>` or `>&` is not a separator."""
    parts, buf, quote, i, n = [], [], "", 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < n:
            buf.append(c + command[i + 1])
            i += 1
        elif c in ";\n|" or (c == "&" and (command[i - 1:i] not in ("<", ">", "&", "|") or i == 0)
                              and command[i + 1:i + 2] not in (">",)):
            if c in "|&" and command[i + 1:i + 2] == c:
                i += 1                                  # && and ||
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts
_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "fi", "done", "esac", "case", "for",
                       "select", "in", "function"})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash", "fish", "busybox", "su", "runuser"})
_INLINE_INTERPRETERS = frozenset({"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php", "lua", "awk", "gawk"})
MAX_SEGMENTS = 20000
MAX_DEPTH = 3
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


_KNOWN_BOOL_LONG = frozenset({"--preserve-env", "--set-home", "--non-interactive", "--login", "--shell", "--stdin", "--background",
                              "--reset-timestamp", "--remove-timestamp", "--foreground", "--preserve-status", "--verbose",
                              "--null", "--no-run-if-empty", "--ignore-environment", "--portability", "--append", "--quiet"})


class _Wrapper(NamedTuple):
    valued_short: str = ""
    valued_long: frozenset = frozenset()
    bool_short: str = ""
    positional: int = 0
    assigns: bool = False


# What each command that merely runs another command takes before it. An option not listed here makes the segment "unsure".
_WRAPPERS: dict[str, _Wrapper] = {
    "sudo": _Wrapper("CDghprRtTUu", frozenset({"--user", "--group", "--host", "--prompt", "--chdir", "--chroot", "--role", "--type",
                                               "--close-from", "--other-user", "--command-timeout"}), "AbEeHiKklnPSsVvB"),
    "doas": _Wrapper("Cu", frozenset(), "nsL"),
    "nice": _Wrapper("n", frozenset({"--adjustment"}), ""),
    "ionice": _Wrapper("cnpPu", frozenset(), "t"),
    "time": _Wrapper("fo", frozenset({"--format", "--output"}), "pavVl"),
    "timeout": _Wrapper("sk", frozenset({"--signal", "--kill-after"}), "v", 1),
    "nohup": _Wrapper(),
    "setsid": _Wrapper("", frozenset(), "cfw"),
    "stdbuf": _Wrapper("ioe", frozenset(), ""),
    "xargs": _Wrapper("ILnPsEdaJ", frozenset({"--max-args", "--max-procs", "--delimiter", "--arg-file", "--replace"}), "0rtpxoO"),
    "env": _Wrapper("uCS", frozenset({"--unset", "--chdir", "--split-string"}), "i0v", 0, True),
    "command": _Wrapper("", frozenset(), "pvV"),
    "exec": _Wrapper("a", frozenset(), "cl"),
    "builtin": _Wrapper(),
}


def _skip_options(toks: Sequence[str], i: int, spec: _Wrapper) -> tuple[int, bool]:
    """Index of the wrapped command and whether every option on the way was understood."""
    while i < len(toks):
        t = toks[i]
        if t == "--":
            return i + 1, True
        if spec.assigns and _ASSIGN.fullmatch(t):
            i += 1
            continue
        if not t.startswith("-") or t == "-":
            return i, True
        if t.startswith("--"):
            if "=" in t:
                i += 1
            elif t in spec.valued_long:
                i += 2
            elif t in _KNOWN_BOOL_LONG:
                i += 1
            else:
                return i + 1, False                       # an option this hook does not know may take a value: unsure
            continue
        if re.fullmatch(r"-\d+", t):
            i += 1
            continue
        chars, consumed_next = t[1:], False
        for k, ch in enumerate(chars):
            if ch in spec.valued_short:
                consumed_next = k == len(chars) - 1
                break
            if ch not in spec.bool_short:
                return i + 1, False
        i += 2 if consumed_next else 1
    return i, True


def _strip_wrappers(toks: list[str]) -> tuple[list[str], bool]:
    """The command a segment really runs, with the wrappers (sudo, nice, env, timeout, ...), assignments and shell keywords
    taken off. The bool is True when something on the way could not be understood."""
    unsure = False
    for _ in range(24):
        while toks and toks[0] in _KEYWORDS:
            toks = toks[1:]
        if toks:
            toks = [toks[0].lstrip("({")] + toks[1:] if toks[0][:1] in "({" and len(toks[0]) > 1 else toks
            if toks[0] in ("(", "{", ""):
                toks = toks[1:]
        if not toks:
            break
        if _ASSIGN.fullmatch(toks[0]):
            toks = toks[1:]
            continue
        head = posixpath.basename(toks[0])
        spec = _WRAPPERS.get(head)
        if spec is None:
            break
        if head == "command" and any(t in ("-v", "-V") for t in toks[1:3]):
            return [], unsure                         # a lookup, not an execution
        i, ok = _skip_options(toks, 1, spec)
        unsure = unsure or not ok
        rest = toks[i + spec.positional:]
        if head == "env" and not rest:
            return ["env"], unsure                    # `env` / `env -i` / `env -u X` print the environment
        toks = rest
    return toks, unsure


def _script_of(toks: Sequence[str]) -> str | None:
    """The script a shell is asked to run with -c (also in clusters such as -lc or -ec), or eval's arguments."""
    head = posixpath.basename(toks[0])
    if head == "eval":
        return " ".join(toks[1:])
    for k, t in enumerate(toks[1:], 1):
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return toks[k + 1] if k + 1 < len(toks) else ""
        if not t.startswith("-") and head not in ("su", "runuser"):
            break
    return None


def _segments(command: str, depth: int = 0) -> tuple[list[list[str]], bool]:
    """Each simple command as a token list, and whether any part of the line could not be understood with confidence. Quoting
    is honoured within a segment; the split ignores quotes, which can only make MORE segments (never hide one). `sh -c '...'`,
    `bash -lc`, `su -c` and `eval` are read through, to a bounded depth."""
    out: list[list[str]] = []
    unsure = depth > MAX_DEPTH
    if unsure:
        return out, True
    parts = _split_top(command)
    for match in _SUBSTITUTION.finditer(command):          # $(...), `...`, <(...): run too, wherever they are written
        parts.append(next(g for g in match.groups() if g is not None))
    if len(parts) > MAX_SEGMENTS:
        parts, unsure = parts[:MAX_SEGMENTS], True
    for part in parts:
        part = part.strip().rstrip(")")
        if not part:
            continue
        try:
            toks = shlex.split(part, comments=False, posix=True)
        except ValueError:
            toks, unsure = part.split(), True
        toks, bad = _strip_wrappers(toks)
        unsure = unsure or bad
        if not toks:
            continue
        head = posixpath.basename(toks[0])
        if head in _SHELLS or head == "eval":
            script = _script_of(toks)
            if script is not None:
                inner, inner_unsure = _segments(script, depth + 1)
                out += inner
                unsure = unsure or inner_unsure
                continue
        elif head in _INLINE_INTERPRETERS and any(t in ("-c", "-e", "-E", "-r") or re.fullmatch(r"-[a-zA-Z]*[ce]", t) for t in toks[1:3]):
            unsure = True                             # code in a language this hook does not read
        out.append(toks[:MAX_TOKENS])
    return out, unsure


def _redirect_targets(command: str) -> list[tuple[str, str]]:
    """(operator, target) of every redirection: `>`/`>>` write the target, `<` reads it."""
    return [(m.group(1), m.group(2)) for m in re.finditer(r"(?:^|[\s;&|])(\d?>>?|<)\s*([^\s;&|<>]+)", command)]


def _paths_in(tokens: Sequence[str]) -> list[str]:
    """The tokens that name a path (including the value of `name=/path` and `--opt=/path`)."""
    paths = []
    for t in tokens:
        if "=" in t and not t.startswith(("/", "~", ".")):
            t = t.split("=", 1)[1]
        if t.startswith("-") and "/" not in t:
            continue
        if t.startswith(("/", "~", "./", "../", ".")) or "/" in t:
            paths.append(t)
    return paths


def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(path) if path.startswith("~") else path
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd or "/", path))


def _written_paths(low: str, toks: Sequence[str]) -> list[str]:
    """The paths a command may write. A reader writes none (`sed` and `awk` unless editing in place); a copier writes only its
    destination; anything else is taken to write every path it names."""
    args = list(toks[1:])
    if low == "sed" or low in ("awk", "gawk"):
        if not any(a == "-i" or a.startswith(("-i", "--in-place")) or (low != "sed" and a == "inplace") for a in args):
            return []
        return _paths_in(args)
    if low == "find":
        return _paths_in(args) if any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint", "-fls") for a in args) else []
    if low in _READERS:
        return []
    if low in _COPIERS:
        for i, a in enumerate(args):
            if a in ("-t", "--target-directory") and i + 1 < len(args):
                return [args[i + 1]]
            if a.startswith("--target-directory="):
                return [a.split("=", 1)[1]]
        positional = [a for a in args if not a.startswith("-")]
        return _paths_in(positional[-1:])
    return _paths_in(args)


# ── the classifier ────────────────────────────────────────────────────────────────────────────────────

_WIPE_ROOTS = ("/", "/etc", "/home", "/var/lib/docker", "/boot", "/usr")
_HARMLESS_DEVICES = ("/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/tty")


def _wipe_roots(policy: HookPolicy) -> tuple[str, ...]:
    stacks = posixpath.normpath(policy.stacks_root) if policy.stacks_root.startswith("/") else ""
    return _WIPE_ROOTS + ((stacks,) if stacks and stacks != "/" else ())


def _broad_rm(tokens: Sequence[str], cwd: str, policy: HookPolicy) -> str | None:
    """A recursive rm of one of the wipe roots, or of everything directly inside one (`/etc/*`)."""
    rec = False
    paths: list[str] = []
    for t in tokens[1:]:
        if t == "--no-preserve-root":
            return "rm --no-preserve-root"
        if t.startswith("--"):
            rec = rec or t == "--recursive"
        elif t.startswith("-") and len(t) > 1:
            rec = rec or "r" in t.lower()
        else:
            paths.append(t)
    if not rec:
        return None
    roots = _wipe_roots(policy)
    for p in paths:
        if "$" in p:
            continue
        norm = _abs(p, cwd)
        base = posixpath.dirname(norm) if posixpath.basename(norm) in ("*", ".*") else None
        if norm in roots or (base is not None and base in roots):
            return f"recursive rm of {norm[:60]}"
    return None


def _docker_wipe(words: Sequence[str]) -> str | None:
    if "volume" in words and any(w in words for w in ("rm", "remove", "prune")):
        return "docker volume removal"
    if "system" in words and "prune" in words and any(w in ("--volumes", "-a", "--all") for w in words):
        return "docker system prune of volumes or images"
    if "prune" in words and any(w in ("-a", "--all") or (w.startswith("-") and not w.startswith("--") and "a" in w) for w in words):
        return "docker prune -a"
    if "down" in words and any(w in ("-v", "--volumes") or w.startswith("--volumes=") for w in words):
        return "compose down --volumes"
    return None


def classify(tool: str, args: Mapping[str, Any], policy: HookPolicy, cwd: str = "") -> Decision:
    """The decision for one tool call. Pure: depends on nothing but its arguments."""
    tool_l = str(tool).lower()
    if tool_l in SHELL_TOOLS:
        command = args.get("command") if isinstance(args.get("command"), str) else args.get("cmd")
        if not isinstance(command, str):
            raise ValueError("a shell tool call without a command string")
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError("a command too long to classify")        # the failure mode decides; the head is never judged alone
        return _classify_command(command, policy, str(args.get("cwd") or cwd or ""))
    paths = [str(v) for k, v in args.items() if k in ("file_path", "path", "filepath", "notebook_path", "target", "destination")
             and isinstance(v, str)]
    pattern = args.get("pattern") if isinstance(args.get("pattern"), str) else ""
    writing = tool_l in ("write", "edit", "multiedit", "notebookedit", "create", "str_replace_editor", "apply_patch", "patch")
    for raw in paths:
        p = _abs(raw, cwd)
        if _is_pe_secret(p):
            return Decision("block", "secret", f"{tool}: {p[:120]} holds Planet Express's own secrets")
        if writing and _is_pe_own(p, policy):
            return Decision("block", "pe", f"{tool}: {p[:120]} is part of Planet Express itself")
    if tool_l in ("grep", "glob") and pattern.startswith("/") and _is_pe_secret(pattern.split("*", 1)[0] or "/"):
        return Decision("block", "secret", f"{tool}: searching Planet Express's own secrets")
    if writing and not paths:
        raise ValueError("a write tool call without a path")
    return ALLOW


def _classify_command(command: str, policy: HookPolicy, cwd: str) -> Decision:
    segments, unsure = _segments(command)
    for op, target in _redirect_targets(command):
        p = _abs(target, cwd)
        if _is_pe_secret(p):
            return Decision("block", "secret", f"a redirection touching {p[:80]}")
        if ">" in op and _is_pe_own(p, policy):
            return Decision("block", "pe", f"a redirection into {p[:80]}, part of Planet Express itself")
    for toks in segments:
        prog = posixpath.basename(toks[0])
        low = prog.lower()
        # ── secret ────────────────────────────────────────────────────────────
        if _dumps_environment(low, toks):
            return Decision("block", "secret", f"{prog}: dumping the environment")
        runs_more = low == "find" and any(t in ("-exec", "-execdir", "-ok", "-okdir", "-delete") for t in toks)
        if low not in _READERS_HARMLESS or runs_more:       # `find -exec cat {}` reads what it finds
            for t in _paths_in(toks[1:]):
                if _is_pe_secret(_abs(t, cwd)):
                    return Decision("block", "secret", f"{prog}: {t[:80]} holds Planet Express's own secrets")
        # ── pe ────────────────────────────────────────────────────────────────
        for t in _written_paths(low, toks):
            if _is_pe_own(_abs(t, cwd), policy):
                return Decision("block", "pe", f"{prog}: {t[:80]} is part of Planet Express itself")
        # ── docker ────────────────────────────────────────────────────────────
        if low in ("docker", "podman", "docker-compose", "podman-compose"):
            wiped = _docker_wipe(toks[1:])
            if wiped:
                return Decision("block", "docker", f"{wiped} deletes Docker data")
        # ── wipe ──────────────────────────────────────────────────────────────
        if low == "wipefs" or low.startswith("mkfs"):
            return Decision("block", "wipe", f"{prog}: destroys a filesystem")
        if low == "dd":
            for t in toks[1:]:
                if t.startswith("of=") and posixpath.normpath(t[3:]).startswith("/dev/") \
                        and posixpath.normpath(t[3:]) not in _HARMLESS_DEVICES and not t[3:].startswith("/dev/fd/"):
                    return Decision("block", "wipe", f"dd to the device {t[3:][:40]}")
        if low == "rm":
            broad = _broad_rm(toks, cwd, policy)
            if broad:
                return Decision("block", "wipe", broad)
    if unsure:
        # Code this hook does not read (an inline interpreter, a shell nested past the depth, an option it does not know) that
        # names something on the hard-block list is blocked: the list is short and catastrophic, so a mistake hidden in an
        # unreadable line must not reach it. Anything else it cannot read is allowed, and recorded like every call.
        if _PE_MARKERS.search(command) or any(own and own in command for own in policy.pe_paths):
            return Decision("block", "pe", "a command this hook could not fully read names Planet Express's own files")
        if _DOCKER_MARKERS.search(command):
            return Decision("block", "docker", "a command this hook could not fully read would delete Docker data")
        if _WIPE_MARKERS.search(command):
            return Decision("block", "wipe", "a command this hook could not fully read would wipe a filesystem or disk")
    return ALLOW


# ── the runner: failure modes and the record ──────────────────────────────────────────────────────────

_PE_MARKERS = re.compile(r"(/etc/planetexpress|/run/planetexpress|/environ\b|sudoers\.d/planetexpress|mission-hooks|"
                         r"/usr/local/lib/planetexpress|/usr/libexec/planetexpress)", re.IGNORECASE)
_DOCKER_MARKERS = re.compile(r"\bvolume\s+(rm|remove|prune)\b|\bsystem\s+prune\b[^;&|\n]*(--volumes|--all|\s-\w*a)|\bprune\s+(-\w*a|--all)|\bdown\s+(-v|--volumes)\b")
_WIPE_MARKERS = re.compile(r"""\bmkfs|\bwipefs\b|\bof=/dev/(?!null|zero|std|tty|fd)|"""
                           r"""\brm\s+-\w*[rR]\w*\s+(--\S+\s+)*/([\s*'"`);]|$|etc|home|usr|boot|var/lib/docker)|"""
                           r"""rmtree\(\s*['"]/(etc|home|usr|boot|var/lib/docker)?['"/]""")
_MARKERS = re.compile(
    r"(planetexpress|environ|printenv|\benv\b|\bset\b|export|declare|mkfs|wipefs|\bdd\b|\brm\b|prune|volume|\bdown\b|sudoers|"
    r"mission-hooks)", re.IGNORECASE)


@dataclass
class HookRunner:
    """Runs the classifier under the configured failure mode and writes the record. `post` is the worker socket's
    `mission.post_event` (kind, data); a `post` that fails never changes a verdict - a pre-hook BLOCK stands even when the
    event could not be written, and the failure is reported on stderr by the CLI."""
    policy: HookPolicy = field(default_factory=HookPolicy)
    post: Callable[[str, dict], Any] | None = None
    post_failures: int = 0

    def _record(self, kind: str, body: dict) -> None:
        if self.post is None:
            return
        try:
            self.post(kind, body)
        except Exception:                                  # noqa: BLE001 - recording must never decide anything
            self.post_failures += 1

    def pre(self, raw: Any) -> Decision:
        """`raw` is the hook's input exactly as the runtime gave it (a dict, or the JSON text of one)."""
        decision, summary = self._decide(raw)
        self._record("tool", {"hook": "pre", "tool": summary[0], "verdict": decision.verdict, "class": decision.cls,
                              "reason": decision.reason[:EVENT_TEXT], "input": summary[1]})
        return decision

    def post_hook(self, raw: Any) -> None:
        tool, shown = _summary(raw)
        outcome = _outcome(raw)
        self._record("note", {"hook": "post", "tool": tool, "outcome": outcome, "input": shown})

    def _decide(self, raw: Any) -> tuple[Decision, tuple[str, str]]:
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(data, dict):
                raise ValueError("hook input is not an object")
            tool = data.get("tool_name") or data.get("tool")
            args = data.get("tool_input") if "tool_input" in data else data.get("args", {})
            if not isinstance(tool, str) or not isinstance(args, dict):
                raise ValueError("hook input has no tool name and arguments")
            return classify(tool, args, self.policy, str(data.get("cwd") or "")), _summary(data)
        except Exception as exc:                           # noqa: BLE001 - the hook could not decide: the failure mode does
            return self._failed(raw, exc), _summary(raw)

    def _failed(self, raw: Any, exc: Exception) -> Decision:
        text = raw if isinstance(raw, str) else (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else _dump(raw))
        blocked_like = (not isinstance(raw, (dict, str, bytes))) or _MARKERS.search(text or "") is not None or not text
        data = raw
        if isinstance(raw, (str, bytes)):
            try:
                data = json.loads(raw)
            except ValueError:
                blocked_like = True                        # cannot even be read: it might be anything
                data = None
        if isinstance(data, dict):
            # A shell tool whose command could not be read might be anything, and so might a call with no tool name.
            tool = data.get("tool_name") or data.get("tool")
            blocked_like = blocked_like or not isinstance(tool, str) or tool.lower() in SHELL_TOOLS
        mode = self.policy.blocked_failure if blocked_like else self.policy.other_failure
        why = f"the hook could not decide ({type(exc).__name__}: {str(exc)[:80]})"
        if mode == "closed":
            return Decision("block", "failure", why + "; failing closed")
        return Decision("allow", "failure", why + "; FAILING OPEN: this call was not checked")


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, default=str)[:MAX_COMMAND_CHARS]
    except (TypeError, ValueError):
        return ""


def _summary(raw: Any) -> tuple[str, str]:
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        tool = str(data.get("tool_name") or data.get("tool") or "?")[:64]
        args = data.get("tool_input") if "tool_input" in data else data.get("args", {})
        shown = args.get("command") or args.get("file_path") or args.get("path") or args.get("pattern") or "" if isinstance(args, dict) else ""
        return tool, str(shown)[:EVENT_TEXT]
    except Exception:                                      # noqa: BLE001
        return "?", ""


def _outcome(raw: Any) -> str:
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        resp = data.get("tool_response") or data.get("result") or {}
        if isinstance(resp, dict):
            err = resp.get("is_error") or resp.get("error") or (resp.get("exit_code") not in (None, 0))
            return "error" if err else "ok"
    except Exception:                                      # noqa: BLE001
        pass
    return "unknown"


# ── Core's side: the policy a session's hooks are given ───────────────────────────────────────────────

def policy_for(cfg: Any, protected_resources: Sequence[str] = (), *, stacks_root: str | None = None,
               pe_paths: Sequence[str] = ()) -> HookPolicy:
    """From `missions.hooks.*`, the stacks root and the paths of this PE install (its config file, database, hook root).
    `protected_resources` is accepted for older callers and ignored: in trust mode the protected set is recorded, never
    enforced by the hook."""
    hooks = getattr(cfg, "hooks", None)
    own = [p for p in (*pe_paths, getattr(hooks, "root", None) or "") if isinstance(p, str) and p.startswith("/")]
    return HookPolicy(blocked_failure=getattr(hooks, "blocked_failure", "closed"),
                      other_failure=getattr(hooks, "other_failure", "open"),
                      stacks_root=stacks_root or "", pe_paths=tuple(dict.fromkeys(own)))


# ── the CLI the runtime's hook configuration invokes ──────────────────────────────────────────────────

POLICY_ENV = "PE_HOOK_POLICY"      # the policy, as JSON, put in the session's environment by Core at launch
POLICY_FILE_ENV = "PE_HOOK_POLICY_FILE"   # a Core-owned file holding the policy, used when it is too large for the environment
LOG_ENV = "PE_HOOK_LOG"            # where the hook records its calls; the Supervisor drains it into the mission's events
HOOK_LOG_NAME = ".pe-hook.log"
MAX_POLICY_BYTES = 16384
MAX_POLICY_FILE_BYTES = 1 << 20
MAX_LOG_LINE = 4096


def main(argv: Sequence[str] | None = None, stdin=None, stderr=None, environ: Mapping[str, str] | None = None) -> int:
    """`<python> <hook dir>/pe_hook.py pre|post` (or `python -m planet_express.missions.hooks`): reads the runtime's hook JSON on
    stdin. Exit 0 allows, exit 2 blocks with the reason on stderr. The policy is the JSON in PE_HOOK_POLICY, which Core put in
    the environment the runtime was started with, and the hook command and this script are installed in a Core-owned
    directory the session cannot write (their hashes are checked at every poll, and a changed or missing file kills the
    session). Nothing in the input can switch the hook off, and a missing or unreadable policy falls back to the
    default one, which fails closed for blocked classes. A hook is still only a speed bump: a session can simply not call it. The session token never reaches a hook: calls are
    recorded to PE_HOOK_LOG, which Core reads (bounded, as untrusted data) and appends to the mission's events."""
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    environ = os.environ if environ is None else environ
    which = argv[0] if argv else "pre"
    if which not in ("pre", "post"):
        print("usage: hooks pre|post", file=stderr)
        return 2
    if environ.get(POLICY_FILE_ENV):
        # Core pointed this session at a policy file (it was too large for the environment). If that file cannot be read or
        # parsed, falling back to the default policy would silently drop this install's own paths, so the call is blocked.
        try:
            with open(environ[POLICY_FILE_ENV], "rb") as handle:
                raw_policy = handle.read(MAX_POLICY_FILE_BYTES + 1)
            if len(raw_policy) > MAX_POLICY_FILE_BYTES:
                raise ValueError("policy file too large")
            policy = HookPolicy.from_json(raw_policy.decode("utf-8"))
        except (ValueError, OSError):
            if which == "pre":
                print("pe-hook: blocked: the session's hook policy file could not be loaded", file=stderr)
                return 2
            policy = HookPolicy()                          # a post hook only records what happened; it does not use the policy
    else:
        try:
            text = environ[POLICY_ENV]
            if len(text) > MAX_POLICY_BYTES:
                raise ValueError("policy too large")
            policy = HookPolicy.from_json(text)
        except (KeyError, ValueError):
            policy = HookPolicy()                          # the default policy fails closed for blocked classes
    runner = HookRunner(policy, post=_log_poster(environ))
    raw = stdin.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raw = None                                         # too large to judge: not an object, so the failure mode decides
    if which == "post":
        runner.post_hook(raw)
        return 0
    decision = runner.pre(raw)
    if runner.post_failures:
        print("pe-hook: the call could not be recorded", file=stderr)
    if decision.verdict == "block":
        print(f"pe-hook: blocked ({decision.cls}): {decision.reason}", file=stderr)
        return 2
    if decision.cls == "failure":
        print(f"pe-hook: {decision.reason}", file=stderr)
    return 0


HOOK_SCRIPT_NAME = "pe_hook.py"
DEFAULT_INTERPRETER = "/usr/bin/python3"


def claude_code_settings(command: Sequence[str]) -> dict:
    """The Claude Code `hooks` settings that run the hook before and after every tool call. `command` is the absolute argv
    that starts it, e.g. ["/usr/bin/python3", "<hook dir>/pe_hook.py"]."""
    base = " ".join(shlex.quote(part) for part in command)

    def entry(which: str) -> list[dict]:
        return [{"matcher": "*", "hooks": [{"type": "command", "command": f"{base} {which}"}]}]
    return {"hooks": {"PreToolUse": entry("pre"), "PostToolUse": entry("post")}}


def install_claude_code(hook_dir: Path, command: Sequence[str] = (DEFAULT_INTERPRETER,)) -> dict[str, str]:
    """Copy THIS file into `hook_dir` as `pe_hook.py` and write the hook settings that run it, before the session launches;
    return the environment that makes the runtime read them (CLAUDE_CONFIG_DIR). A copy made at every launch is the hook this
    Core runs, so it can never be stale (a hand-installed copy once was). `command[0]` is the interpreter
    (`missions.hooks.command`, default /usr/bin/python3); anything after it (an older config's script path or `-m` module) is
    ignored in favour of the copy. `hook_dir` is a per-session directory that CORE owns, outside the session's workspace: the
    session can read settings.json and pe_hook.py (0644 in a 0755 directory) and can neither edit nor delete them, and the
    Supervisor checks both hashes at every poll. Only the append-only hook log stays in the workspace, as untrusted data. A
    failure raises: a mission configured to run under hooks does not start without them."""
    interpreter = (list(command) or [DEFAULT_INTERPRETER])[0]
    if not interpreter.startswith("/"):
        raise ValueError("the hook interpreter must be an absolute path")
    script = Path(hook_dir) / HOOK_SCRIPT_NAME
    temp = script.with_name(f".{HOOK_SCRIPT_NAME}.tmp")
    shutil.copyfile(Path(__file__).resolve(), temp)
    temp.chmod(0o644)
    os.replace(temp, script)
    target = Path(hook_dir) / "settings.json"
    target.write_text(json.dumps(claude_code_settings([interpreter, str(script)]), indent=2) + "\n", encoding="utf-8")
    target.chmod(0o644)
    return {"CLAUDE_CONFIG_DIR": str(hook_dir)}


def claude_code_argv(argv: Sequence[str], hook_dir: Path) -> list[str]:
    """The runtime's argv with PE's settings forced on it: the hook settings by `--settings`, and `--setting-sources ""` so NO other
    settings file is read. Without it the CLI also loads user, project and local settings, and the project ones live in the
    session's own workspace (`.claude/settings.json`): a session could plant permissions or hooks there that govern its own later
    behaviour, or the next turn's. This closes the settings *files* only; other workspace-resident configuration (`.mcp.json`,
    `.claude/agents`, `.claude/skills`, `.claude/commands`, `CLAUDE.md`) is a separate question, tracked as a Console (P1) test list.
    It assumes the configured argv runs the claude CLI directly, which is what `install_claude_code` means. Any `--settings` or `--setting-sources` the configured argv already has is dropped, so PE's win. The flags go straight after the
    executable: appended, a `--` terminator would turn them into prompt text and a dangling value-taking option would consume them."""
    head: list[str] = []
    skip = False
    args = list(argv)
    exe, args = args[0], args[1:]
    for index, arg in enumerate(args):
        if arg == "--":                          # everything after the terminator is positional: never an option, never stripped
            tail = args[index:]
            break
        if skip:
            skip = False
        elif arg in ("--settings", "--setting-sources"):
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise ValueError(f"{arg} has no value: refusing to guess which argument was meant")
            skip = True
        elif not arg.startswith(("--settings=", "--setting-sources=")):
            head.append(arg)
    else:
        tail = []
    return [exe, "--settings", str(Path(hook_dir) / "settings.json"), "--setting-sources", "", *head, *tail]


def _log_poster(environ: Mapping[str, str]) -> Callable[[str, dict], Any] | None:
    """One JSON line per call appended to PE_HOOK_LOG (never following a symlink). Without a log path there is nothing to
    record to, and the hook says so; it still decides."""
    path = environ.get(LOG_ENV)
    if not path:
        return None

    def post(kind: str, data: dict) -> None:
        line = json.dumps({"kind": kind, "data": data}, separators=(",", ":"), default=str)
        if len(line) > MAX_LOG_LINE:
            line = json.dumps({"kind": kind, "data": {"hook": str(data.get("hook"))[:8], "truncated": True}})
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        try:
            os.fchmod(fd, 0o640)                      # Core reads it through the workspace's group, whatever the umask was
            os.write(fd, line.encode("utf-8") + b"\n")
        finally:
            os.close(fd)
    return post


__all__ = ["ALLOW", "BLOCKED_CLASSES", "HOOK_SCRIPT_NAME", "Decision", "HookPolicy", "HookRunner", "claude_code_argv",
           "claude_code_settings", "classify", "install_claude_code", "main", "policy_for"]

if __name__ == "__main__":
    raise SystemExit(main())
