"""The hook layer (contract 12, v4 slice 7a): the reference adapter's pre/post tool hooks.

A hook sees one tool call (a tool name and its arguments) BEFORE it runs and answers `allow`, `ask` or `block`; after it runs
it records the outcome. Both directions call `mission.post_event` (so every tool call is on the mission's record) and the
pre-hook may block. The blocked classes are enforced even when the runtime itself is unrestricted:

    secret    reading secret stores and `.env` contents, dumping the environment
    vcs       destructive VCS (reset --hard, clean, force push, branch -D, rebase, ...) in a live tree
    publish   anything that publishes (git push, npm/cargo/gem publish, twine/docker push, gh release/pr)
    wipe      broad `rm` and the mkfs / dd / wipefs / shred class
    docker    `docker volume rm|prune`, `prune -a|--all`, `compose down -v`
    config    edits of sudoers and docker's daemon.json

and these are asked about: crossing into a protected resource, and irreversible-looking operations.

THESE HOOKS ARE A SPEED BUMP, NOT THE BOUNDARY. They are a pattern match over a command line that a determined agent can
route around (an interpreter, an encoding, a different binary). The boundary is the worker's identity and credentials, the
per-session environment Core builds from the grant, the scoped sudo and Docker proxy, the tripwire monitor, the checkpoint and
Core's own verification: every one of those holds with the hooks absent, and `tests/test_mission_hooks.py` asserts that.

The decision depends on the tool name, its arguments and the policy given at construction, and on nothing else: not the
transcript, not the process environment, not anything the agent wrote. A hostile transcript has no input to this function.

Failure mode (configured, default fail closed for blocked classes, fail open with a loud event otherwise): when the hook
itself cannot decide (the input is unreadable, the classifier raises), the raw input is screened for ANY blocked-class marker;
if it has one, or cannot be read at all, `blocked_failure` applies (default `closed`: block), otherwise `other_failure`
applies (default `open`: allow, with a loud event so the miss is on the record).
"""

import json
import os
import posixpath
import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

Verdict = Literal["allow", "ask", "block"]
BLOCKED_CLASSES = ("secret", "vcs", "publish", "wipe", "docker", "config")
MAX_COMMAND_CHARS = 65536
MAX_INPUT_BYTES = 1 << 20          # the most of a hook's stdin that is read and parsed (a large Write or heredoc is legitimate)
MAX_TOKENS = 512
EVENT_TEXT = 400


class Decision(NamedTuple):
    verdict: Verdict
    cls: str            # one of BLOCKED_CLASSES, "protected", "irreversible", "failure" or ""
    reason: str


ALLOW = Decision("allow", "", "")
SHELL_TOOLS = frozenset({"bash", "shell", "run", "exec", "command", "terminal"})


@dataclass(frozen=True)
class HookPolicy:
    """Who and what the hook protects. Built by Core from the grant and the site config (`policy_for`), never from the agent."""
    live_trees: Sequence[str] = ()              # working trees where destructive VCS is blocked
    protected_paths: Sequence[str] = ()         # absolute paths of protected resources (ask on crossing)
    blocked_failure: Literal["closed", "open"] = "closed"
    other_failure: Literal["closed", "open"] = "open"

    def to_json(self) -> str:
        return json.dumps({"live_trees": list(self.live_trees), "protected_paths": list(self.protected_paths),
                           "blocked_failure": self.blocked_failure, "other_failure": self.other_failure})

    @classmethod
    def from_json(cls, text: str) -> "HookPolicy":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("hook policy must be an object")
        bf, of = raw.get("blocked_failure", "closed"), raw.get("other_failure", "open")
        if bf not in ("closed", "open") or of not in ("closed", "open"):
            raise ValueError("failure modes are closed or open")
        return cls(tuple(str(x) for x in raw.get("live_trees", [])), tuple(str(x) for x in raw.get("protected_paths", [])), bf, of)


# ── what is secret / sensitive ────────────────────────────────────────────────────────────────────────

_SECRET_BASENAME = re.compile(
    r"^(\.env(\..+)?|.*\.env|\.netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc|\.htpasswd|id_(rsa|dsa|ecdsa|ed25519)|"
    r".*\.(pem|key|p12|pfx|jks|kdbx)|credentials(\.json)?|secrets?(\.(ya?ml|json|env|txt))?|token(s)?(\.(json|txt))?|"
    r"shadow|gshadow|master\.key)$", re.IGNORECASE)
_SECRET_SAFE = re.compile(r"^\.env\.(example|sample|template|dist)$", re.IGNORECASE)
_SECRET_PATHS = ("/etc/shadow", "/etc/gshadow", "/etc/planetexpress", "/etc/ssh/ssh_host", "/root/.ssh", "/run/secrets",
                 "/var/run/secrets", "/proc/self/environ", "/run/planetexpress", "/etc/ssl/private")
_SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker/config.json", ".config/gcloud", ".config/gh")
_READERS_HARMLESS = {"ls", "stat", "test", "[", "file", "wc", "find"}      # name the file, never its contents
_SUDOERS = ("/etc/sudoers", "/etc/sudoers.d")
_DAEMON_JSON = "/etc/docker/daemon.json"


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


def _is_secret_path(path: str) -> bool:
    norm = posixpath.normpath(path.replace("\\", "/"))
    base = posixpath.basename(norm)
    if _SECRET_SAFE.match(base):
        return False
    if _SECRET_BASENAME.match(base):
        return True
    if "/proc/" in norm and norm.endswith("/environ"):
        return True
    if any(norm == p or norm.startswith(p + "/") or norm.startswith(p) and p.endswith("ssh_host") for p in _SECRET_PATHS):
        return True
    return any(f"/{d}/" in norm + "/" or norm.endswith(f"/{d}") or norm.startswith(f"{d}/") or norm == d for d in _SECRET_DIRS)


def _is_config_path(path: str) -> bool:
    norm = posixpath.normpath(path)
    return norm == _DAEMON_JSON or norm in _SUDOERS or norm.startswith("/etc/sudoers.d/")


def _within(path: str, root: str) -> bool:
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


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


def _redirect_targets(command: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"(?:^|[\s;&|])(\d?>>?|<)\s*([^\s;&|<>]+)", command)]


def _paths_in(tokens: Sequence[str]) -> list[str]:
    paths = []
    for t in tokens:
        if "=" in t and not t.startswith(("/", "~", ".")):
            t = t.split("=", 1)[1]
        if t.startswith(("-",)) and "/" not in t:
            continue
        if t.startswith(("/", "~", "./", "../", ".")) or "/" in t or _SECRET_BASENAME.match(posixpath.basename(t)) is not None:
            paths.append(t)
    return paths


def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(path) if path.startswith("~") else path
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd or "/", path))


# ── the classifier ────────────────────────────────────────────────────────────────────────────────────

_DESTRUCTIVE_GIT = ({"reset": ("--hard", "--merge", "--keep"), "clean": ("-f", "-d", "-x", "--force"),
                     "checkout": ("--", ".", "-f", "--force"), "restore": ("--staged", ".", "--source"),
                     "branch": ("-D", "--delete", "-d", "-M"), "rebase": (), "filter-branch": (), "filter-repo": (),
                     "reflog": ("expire", "delete"), "gc": ("--prune",), "update-ref": ("-d",), "stash": ("drop", "clear"),
                     "worktree": ("remove", "prune"), "tag": ("-d", "--delete"), "commit": ("--amend",),
                     "push": ("--force", "-f", "--delete", "--mirror")})
_WIPERS = {"mkfs", "wipefs", "shred", "fdisk", "sfdisk", "parted", "mkswap", "blkdiscard", "cryptsetup", "lvremove", "vgremove",
           "pvremove", "zpool", "zfs"}
_IRREVERSIBLE = {"rm", "rmdir", "unlink", "truncate", "kill", "pkill", "killall", "crontab", "userdel", "groupdel", "passwd",
                 "iptables", "ip6tables", "nft", "ufw", "reboot", "shutdown", "poweroff", "halt", "mount", "umount", "swapoff"}
_SYSTEMCTL_IRREVERSIBLE = {"stop", "disable", "mask", "kill", "reset-failed", "daemon-reload"}
_DOCKER_IRREVERSIBLE = {"rm", "rmi", "stop", "kill", "down", "prune", "restart", "update", "pause"}
_SQL = re.compile(r"\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from)\b", re.IGNORECASE)


def _git_args(tokens: Sequence[str]) -> tuple[str | None, list[str], str | None]:
    """(subcommand, its args, the -C directory if any) of a git command line."""
    rest, workdir, i = list(tokens[1:]), None, 0
    while i < len(rest):
        a = rest[i]
        if a == "-C" and i + 1 < len(rest):
            workdir, i = rest[i + 1], i + 2
        elif a in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env") and i + 1 < len(rest):
            if a == "--work-tree":
                workdir = rest[i + 1]
            elif a == "--git-dir" and workdir is None:
                workdir = posixpath.dirname(rest[i + 1].rstrip("/")) if rest[i + 1].rstrip("/").endswith(".git") else rest[i + 1]
            i += 2
        elif a.startswith("--work-tree="):
            workdir, i = a.split("=", 1)[1], i + 1
        elif a.startswith("--git-dir=") and workdir is None:
            gd = a.split("=", 1)[1].rstrip("/")
            workdir, i = (posixpath.dirname(gd) if gd.endswith(".git") else gd), i + 1
        elif a.startswith("-C") and len(a) > 2:
            workdir, i = a[2:], i + 1
        elif a.startswith("-"):
            i += 1
        else:
            return a, rest[i + 1:], workdir
    return None, [], workdir


_PUBLISH_VERBS = {"npm": {"publish"}, "yarn": {"publish"}, "pnpm": {"publish"}, "cargo": {"publish"}, "gem": {"push"},
                  "twine": {"upload"}, "helm": {"push"}, "hg": {"push"}, "svn": {"commit", "ci"}, "flutter": {"publish"}}
_DOCKER_VALUED = frozenset({"--context", "-c", "--config", "-H", "--host", "-l", "--log-level", "--tlscacert", "--tlscert",
                            "--tlskey", "--project-name", "-p", "--file", "-f", "--profile", "--project-directory", "--env-file"})
_GH_VALUED = frozenset({"-R", "--repo", "--hostname"})


def _words(tokens: Sequence[str], valued: frozenset) -> list[str]:
    """The non-option words of a command line, with the values of the options that take one skipped."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            out += list(tokens[i + 1:])
            break
        if t in valued:
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            out.append(t)
            i += 1
    return out


def _publishes(low: str, toks: Sequence[str]) -> str | None:
    """What this command publishes, if it does, reading each tool's own option grammar."""
    if low == "git":
        sub, _args, _wd = _git_args(toks)
        return "git push" if sub == "push" else None
    if low in ("docker", "podman", "docker-compose"):
        words = _words(toks[1:], _DOCKER_VALUED)
        if "push" in words[:3] or any(t == "--push" or t.startswith("--push=") for t in toks[1:]):
            return f"{low} push"
        return None
    if low == "gh":
        words = _words(toks[1:], _GH_VALUED)
        verbs = {"release": {"create", "upload", "edit", "delete", "delete-asset"},
                 "pr": {"create", "merge", "edit", "close", "reopen", "comment", "review", "ready"}}
        if len(words) > 1 and words[1] in verbs.get(words[0], ()):
            return f"gh {words[0]} {words[1]}"
        return None
    verbs = _PUBLISH_VERBS.get(low)
    if verbs and any(t in verbs for t in toks[1:]):
        return f"{low} {sorted(verbs)[0]}"
    if low in ("curl", "wget") and any(m in toks for m in ("-T", "--upload-file")):
        return f"{low} upload"
    return None


def _vcs_destructive(sub: str, args: Sequence[str]) -> bool:
    flags = _DESTRUCTIVE_GIT.get(sub)
    if flags is None:
        return False
    if not flags:
        return True                                      # rebase, filter-branch: always rewriting
    if sub == "checkout":
        return any(a in ("--", ".", "-f", "--force") for a in args)
    if sub == "restore":
        return not any(a == "--staged" for a in args) or "." in args
    return any(a == f or (len(f) == 2 and f.startswith("-") and a.startswith("-") and not a.startswith("--") and f[1] in a[1:])
               for a in args for f in flags)


def _broad_rm(tokens: Sequence[str], cwd: str) -> str | None:
    rec = force = False
    paths: list[str] = []
    for t in tokens[1:]:
        if t.startswith("--"):
            rec = rec or t in ("--recursive",)
            force = force or t == "--force"
            if t == "--no-preserve-root":
                return "rm --no-preserve-root"
        elif t.startswith("-") and len(t) > 1:
            rec = rec or "r" in t.lower()
            force = force or "f" in t
        else:
            paths.append(t)
    if not rec:
        return None
    for p in paths:
        if p in (".", "..", "*", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/*") or p.startswith(("$HOME", "${HOME}")) and p.count("/") <= 1:
            return f"recursive rm of {p[:60]}"
        norm = _abs(p, cwd) if "$" not in p else p
        parts = [x for x in norm.split("/") if x]
        if norm.startswith("/") and len(parts) <= 2:
            return f"recursive rm of {norm[:60]}"
        if "*" in parts[-1:] and len(parts) <= 3 and norm.startswith("/"):
            return f"recursive rm of {norm[:60]}"
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
        if _is_config_path(p):
            return Decision("block", "config", f"{tool}: edits of sudoers and daemon.json are not allowed")
        if _is_secret_path(p):
            return Decision("block", "secret", f"{tool}: {posixpath.basename(p)} is a secret store")
    if tool_l in ("grep", "glob") and pattern and _SECRET_BASENAME.match(posixpath.basename(pattern)) and not _SECRET_SAFE.match(pattern):
        return Decision("block", "secret", f"{tool}: searching for secret files")
    for raw in paths:
        p = _abs(raw, cwd)
        if any(_within(p, root) for root in policy.protected_paths):
            return Decision("ask", "protected", f"{tool}: {p[:120]} is inside a protected resource")
    if writing and not paths:
        raise ValueError("a write tool call without a path")
    return ALLOW


def _classify_command(command: str, policy: HookPolicy, cwd: str) -> Decision:
    segments, unsure = _segments(command)
    targets = _redirect_targets(command)
    ask: Decision | None = None
    for target in targets:
        p = _abs(target, cwd)
        if _is_config_path(p):
            return Decision("block", "config", "a redirection into sudoers or daemon.json")
        if _is_secret_path(p):
            return Decision("block", "secret", f"a redirection touching {posixpath.basename(p)}")
    for toks in segments:
        prog = posixpath.basename(toks[0])
        low = prog.lower()
        # ── secret ────────────────────────────────────────────────────────────
        if _dumps_environment(low, toks):
            return Decision("block", "secret", f"{prog}: dumping the environment")
        if low not in _READERS_HARMLESS:
            for t in _paths_in(toks[1:]):
                if _is_secret_path(_abs(t, cwd)):
                    return Decision("block", "secret", f"{prog}: touches {posixpath.basename(t)[:60]}")
        # ── config ────────────────────────────────────────────────────────────
        if low in ("visudo", "sudoedit"):
            return Decision("block", "config", f"{prog}: edits sudoers")
        if any(_is_config_path(_abs(t, cwd)) for t in _paths_in(toks[1:])) and low not in ("ls", "stat", "test", "["):
            writers = {"tee", "sed", "cp", "mv", "ln", "install", "dd", "rm", "truncate", "chmod", "chown", "vi", "vim", "nano",
                       "ed", "perl", "python", "python3", "sh", "bash", "patch", "rsync", "echo", "printf", "cat"}
            if low in writers or len(toks) > 1:
                return Decision("block", "config", f"{prog}: touches sudoers or daemon.json")
        # ── publish ───────────────────────────────────────────────────────────
        published = _publishes(low, toks)
        if published:
            return Decision("block", "publish", f"{published}: publishing is not allowed")
        # ── docker ────────────────────────────────────────────────────────────
        if low in ("docker", "podman", "docker-compose"):
            words = [t for t in toks[1:]]
            if "volume" in words and any(w in words for w in ("rm", "remove", "prune")):
                return Decision("block", "docker", "docker volume removal")
            if "prune" in words and any(w in ("-a", "--all") or (w.startswith("-") and not w.startswith("--") and "a" in w) for w in words):
                return Decision("block", "docker", "docker prune -a")
            if "down" in words and any(w in ("-v", "--volumes") for w in words):
                return Decision("block", "docker", "compose down --volumes")
            if "system" in words and "prune" in words and any(w in ("--volumes", "-a", "--all") for w in words):
                return Decision("block", "docker", "docker system prune of volumes or images")
            if (words[:1] and words[0] in _DOCKER_IRREVERSIBLE) or (len(words) > 1 and words[1] in _DOCKER_IRREVERSIBLE):
                ask = ask or Decision("ask", "irreversible", f"{prog} {' '.join(words[:2])[:40]}")
        # ── wipe ──────────────────────────────────────────────────────────────
        if low in _WIPERS or low.startswith("mkfs"):
            return Decision("block", "wipe", f"{prog}: destroys a filesystem or disk")
        if low == "dd" and any(t.startswith("of=") for t in toks):
            return Decision("block", "wipe", "dd with an output target")
        if low in ("chmod", "chown", "chgrp") and any(t in ("-R", "-r", "--recursive") for t in toks) and any(
                posixpath.normpath(t) in ("/", "/etc", "/usr", "/var", "/home", "/root", "/bin", "/lib") for t in toks[1:]):
            return Decision("block", "wipe", f"{prog} -R over a system directory")
        if low == "find" and "-delete" in toks and any(t in ("/", "~", "$HOME") for t in toks[1:]):
            return Decision("block", "wipe", "find -delete from a root")
        if low == "rm":
            broad = _broad_rm(toks, cwd)
            if broad:
                return Decision("block", "wipe", broad)
        # ── vcs ───────────────────────────────────────────────────────────────
        if low == "git":
            gsub, gargs, workdir = _git_args(toks)
            if gsub and _vcs_destructive(gsub, gargs):
                where = _abs(workdir, cwd) if workdir else cwd
                if not where or any(_within(where, tree) for tree in policy.live_trees) or any(
                        _within(_abs(a, where), tree) for a in gargs if a.startswith(("/", ".")) for tree in policy.live_trees):
                    return Decision("block", "vcs", f"git {gsub}: destructive VCS in a live tree")
                ask = ask or Decision("ask", "irreversible", f"git {gsub} rewrites history in {where[:60]}")
        # ── ask classes ───────────────────────────────────────────────────────
        if low in _IRREVERSIBLE or (low == "systemctl" and any(t in _SYSTEMCTL_IRREVERSIBLE for t in toks[1:])):
            ask = ask or Decision("ask", "irreversible", f"{prog} {' '.join(toks[1:3])[:40]}")
        for t in _paths_in(toks[1:]):
            if any(_within(_abs(t, cwd), root) for root in policy.protected_paths):
                ask = Decision("ask", "protected", f"{prog}: {t[:100]} is inside a protected resource")
                break
    if _SQL.search(command):
        ask = ask or Decision("ask", "irreversible", "a destructive SQL statement")
    if any(t for t in targets) and not ask:
        for target in targets:
            if any(_within(_abs(target, cwd), root) for root in policy.protected_paths):
                ask = Decision("ask", "protected", "a redirection into a protected resource")
    if ask is None and re.search(r"(^|[^>])>\s*[^\s>&|]", command) and "/dev/null" not in command and "2>&1" not in command:
        ask = Decision("ask", "irreversible", "a redirection that may truncate a file")
    if ask is None and unsure and _MARKERS.search(command):
        ask = Decision("ask", "irreversible", "a command this hook could not fully parse names a blocked-class word")
    return ask or ALLOW


# ── the runner: failure modes and the record ──────────────────────────────────────────────────────────

_MARKERS = re.compile(
    r"(\.env|shadow|id_rsa|\.pem|\.ssh|secret|credential|environ|printenv|\brm\b|mkfs|\bdd\b|wipefs|shred|\bgit\b|publish|"
    r"\bpush\b|upload|prune|volume|compose|sudoers|visudo|daemon\.json|passwd|token|\bgh\b)", re.IGNORECASE)


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

def policy_for(cfg: Any, protected_resources: Sequence[str] = (), *, stacks_root: str | None = None) -> HookPolicy:
    """From `missions.*` config and the protected set. Protected resources that are paths or mounts become `ask` roots."""
    paths: list[str] = []
    for resource in protected_resources:
        _host, _sep, rest = resource.partition("/")
        kind, _s, name = rest.partition(":") if rest else ("", "", "")
        if kind in ("path", "mount") and name.startswith("/"):
            paths.append(name)
    hooks = getattr(cfg, "hooks", None)
    return HookPolicy(live_trees=tuple(cfg.live_trees), protected_paths=tuple(paths),
                      blocked_failure=getattr(hooks, "blocked_failure", "closed"),
                      other_failure=getattr(hooks, "other_failure", "open"))


# ── the CLI the runtime's hook configuration invokes ──────────────────────────────────────────────────

POLICY_ENV = "PE_HOOK_POLICY"      # the policy, as JSON, put in the session's environment by Core at launch
POLICY_FILE_ENV = "PE_HOOK_POLICY_FILE"   # a Core-owned file holding the policy, used when it is too large for the environment
LOG_ENV = "PE_HOOK_LOG"            # where the hook records its calls; the Supervisor drains it into the mission's events
HOOK_LOG_NAME = ".pe-hook.log"
MAX_POLICY_BYTES = 16384
MAX_POLICY_FILE_BYTES = 1 << 20
MAX_LOG_LINE = 4096


def main(argv: Sequence[str] | None = None, stdin=None, stderr=None, environ: Mapping[str, str] | None = None) -> int:
    """`python -m planet_express.missions.hooks pre|post`: reads the runtime's hook JSON on stdin. Exit 0 allows (printing a
    JSON permission decision for `ask`), exit 2 blocks with the reason on stderr. The policy is the JSON in PE_HOOK_POLICY,
    which Core put in the environment the runtime was started with, and the hook command itself is installed by settings.json
    in a Core-owned directory the session cannot write (its hash is checked at every poll, and a changed or missing file
    kills the session). Nothing in the input can switch the hook off, and a missing or unreadable policy falls back to the
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
        # parsed, falling back to the default policy would silently drop every protected path, so the call is blocked instead.
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
    if decision.verdict == "ask":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                 "permissionDecisionReason": f"pe-hook: {decision.reason}"}}))
    elif decision.cls == "failure":
        print(f"pe-hook: {decision.reason}", file=stderr)
    return 0


def claude_code_settings(command: Sequence[str]) -> dict:
    """The Claude Code `hooks` settings that run this module before and after every tool call. `command` is the absolute argv
    that starts it, e.g. ["/opt/pe/venv/bin/python", "-m", "planet_express.missions.hooks"]."""
    base = " ".join(shlex.quote(part) for part in command)

    def entry(which: str) -> list[dict]:
        return [{"matcher": "*", "hooks": [{"type": "command", "command": f"{base} {which}"}]}]
    return {"hooks": {"PreToolUse": entry("pre"), "PostToolUse": entry("post")}}


def install_claude_code(hook_dir: Path, command: Sequence[str]) -> dict[str, str]:
    """Write the hook settings into `hook_dir` before the session launches, and return the environment that makes the runtime
    read them (CLAUDE_CONFIG_DIR). `hook_dir` is a per-session directory that CORE owns, outside the session's workspace:
    the session can read settings.json (0644 in a 0755 directory) and can neither edit nor delete it, and the Supervisor
    checks its hash at every poll. Only the append-only hook log stays in the workspace, as untrusted data. A failure raises:
    a mission configured to run under hooks does not start without them."""
    target = Path(hook_dir) / "settings.json"
    target.write_text(json.dumps(claude_code_settings(command), indent=2) + "\n", encoding="utf-8")
    target.chmod(0o644)
    return {"CLAUDE_CONFIG_DIR": str(hook_dir)}


def claude_code_argv(argv: Sequence[str], hook_dir: Path) -> list[str]:
    """The runtime's argv with PE's settings forced on it: the hook settings by `--settings`, and `--setting-sources ""` so NO other
    settings file is read. Without it the CLI also loads user, project and local settings, and the project ones live in the
    session's own workspace (`.claude/settings.json`): a session could plant permissions or hooks there that govern its own later
    behaviour, or the next turn's. Any `--settings` or `--setting-sources` the configured argv already has is dropped, so PE's win."""
    out: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg in ("--settings", "--setting-sources"):
            skip = True
        elif not arg.startswith(("--settings=", "--setting-sources=")):
            out.append(arg)
    return [*out, "--settings", str(Path(hook_dir) / "settings.json"), "--setting-sources", ""]


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


__all__ = ["ALLOW", "BLOCKED_CLASSES", "Decision", "HookPolicy", "HookRunner", "claude_code_argv", "claude_code_settings", "classify",
           "install_claude_code", "main", "policy_for"]

if __name__ == "__main__":
    raise SystemExit(main())
