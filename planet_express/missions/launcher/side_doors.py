"""Setup preflight side doors (brief 6.8): no other path to root around the worker boundary.

    worker-identity       no session uid is Core, root or in the `docker` group, and the docker gid is not a session gid
    docker-tcp-exposure   the Docker daemon has no unauthenticated TCP listener (config, units and live sockets); a
                          socket proxy with an allowlist is acceptable once the operator names its port
    sudoers-managed       every sudoers rule that reaches Core is one the generator wrote, byte for byte; unmanaged ones
                          (a wildcard that sudoers matches across spaces, `ALL`) are reported and removed by `repair`
                          only with approval, validated with `visudo -c`, undone from journaled evidence

Same discipline as `setup_steps` (probe reads only; repair compares what it saw and refuses over anything that is not
setup's; undo refuses once the file moved on), written against plain paths so the tests run on a throwaway tree.
Every probe FAILS CLOSED: a file it cannot read, parse or reason about is a failed (and `blocked`) probe, never a pass.
`repair_gate_report` is what a Repair-tier start consults; Observe and Propose never do. Nothing here runs a shell:
`ss` and `visudo` are run as argv with a minimal environment.
"""

import json
import os
import re
import shlex
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from planet_express.missions.launcher.preflight import Check, PreflightReport
from planet_express.missions.launcher.setup_steps import (
    Probe,
    Step,
    StepError,
    StepRefused,
    read_regular,
    sha256,
    write_atomic,
)

DOCKER_TCP_PORTS = (2375, 2376)
_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
_TRUE = frozenset({"1", "t", "true"})
_FALSE = frozenset({"0", "f", "false"})


def _exec_tls_verified(exec_start: str) -> bool:
    """Whether a dockerd command line demands client-verified TLS: `--tlsverify` true (bare, or `=true`/`=1`; the last one
    wins, a false or unreadable value is not verification) AND a non-empty `--tlscacert` to verify clients against."""
    try:
        words = shlex.split(exec_start)
    except ValueError:
        return False
    verify, cacert = False, False
    for i, word in enumerate(words):
        flag, eq, value = word.partition("=")
        if flag in ("--tlsverify", "-tlsverify"):
            verify = (not eq) or value.strip().lower() in _TRUE
        elif flag in ("--tlscacert", "-tlscacert"):
            if not eq and i + 1 < len(words):
                value = words[i + 1]
            cacert = bool(value.strip())
    return verify and cacert


_TCP_HOST = re.compile(r"""(?:-H|--host)[ =]*["']?(tcp://[^\s"']*)""", re.IGNORECASE)
MAX_FILE = 1 << 20


def _approved(paths: "SideDoorPaths", step: str, detail: str) -> None:
    if not paths.approve(step, detail):
        raise StepError(f"{step}: the repair needs operator approval (not given); nothing was changed")


@dataclass
class SideDoorPaths:
    core_user: str
    uid_min: int
    uid_max: int
    passwd: str = "/etc/passwd"
    group: str = "/etc/group"
    sudoers: str = "/etc/sudoers"
    sudoers_dir: str = "/etc/sudoers.d"
    daemon_json: str = "/etc/docker/daemon.json"
    docker_units: Sequence[str] = ("/lib/systemd/system", "/usr/lib/systemd/system", "/etc/systemd/system")
    visudo: str = "/usr/sbin/visudo"
    ss: str = "ss"
    managed: Mapping[str, bytes] = field(default_factory=dict)      # sudoers path -> the generator's exact output
    proxy_ports: Sequence[int] = ()                                  # operator-acknowledged allowlisting socket proxies
    approve: Callable[[str, str], bool] = lambda step, detail: False   # nothing is repaired without a yes
    owner_uid: int = 0
    owner_gid: int = 0
    run: Callable = field(default=subprocess.run, repr=False)


def _read(path: str) -> str:
    data = read_regular(path)
    if data is None:
        raise StepError(f"{path} is absent")
    if len(data) >= MAX_FILE:
        raise StepError(f"{path} is too large to check")
    return data.decode("utf-8", errors="replace")


def _passwd(text: str) -> list[tuple[str, int, int]]:
    out = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            out.append((parts[0], int(parts[2]), int(parts[3])))
    return out


def _groups(text: str) -> dict[str, tuple[int, list[str]]]:
    out = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            out[parts[0]] = (int(parts[2]), [m for m in (parts[3].split(",") if len(parts) > 3 else []) if m])
    return out


def _preserve(path: str) -> tuple[int, int, int]:
    info = os.lstat(path)
    return stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def _restore_files(step: Step, evidence: dict) -> str:
    """Put back every file the step rewrote, but only if every one is still byte-for-byte what the step wrote."""
    files = evidence.get("files", {})
    for path, info in files.items():
        current = read_regular(path)
        if current is None or sha256(current) != info["sha256"]:
            raise StepRefused(f"{path} was changed after setup wrote it; nothing was reverted")
    for path, info in files.items():
        write_atomic(path, info["previous"].encode(), info["mode"], info["uid"], info["gid"])
    return f"restored {len(files)} file(s)"


# ── (a) worker identity ───────────────────────────────────────────────────────────────────────────

class WorkerIdentity(Step):
    name = "worker-identity"

    def _offenders(self) -> tuple[list[str], str | None]:
        p = self.p
        passwd, groups = _passwd(_read(p.passwd)), _groups(_read(p.group))
        problems: list[str] = []
        if not 0 < p.uid_min <= p.uid_max:
            return [], "the session uid range is malformed or includes root"
        core = next((uid for name, uid, _gid in passwd if name == p.core_user), None)
        if core is None:
            return [], f"the Core user {p.core_user!r} is not in {p.passwd}"
        if p.uid_min <= core <= p.uid_max:
            problems.append(f"the Core identity (uid {core}) is inside the session uid range")
        docker = groups.get("docker")
        if docker:
            gid, members = docker
            if p.uid_min <= gid <= p.uid_max:
                problems.append(f"the docker gid {gid} is inside the session gid range")
            for name, uid, pgid in passwd:
                if p.uid_min <= uid <= p.uid_max and (name in members or pgid == gid):
                    problems.append(f"session account {name} (uid {uid}) is in the docker group")
        return problems, None

    def probe(self) -> Probe:
        try:
            problems, fatal = self._offenders()
        except (StepError, OSError) as exc:
            return Probe(False, f"cannot check the worker identity: {exc}", blocked=True)
        if fatal:
            return Probe(False, fatal, blocked=True)
        if problems:
            return Probe(False, "; ".join(problems), blocked=not self._repairable(problems))
        return Probe(True, f"uids {self.p.uid_min}-{self.p.uid_max}: not Core, not in docker")

    @staticmethod
    def _repairable(problems: list[str]) -> bool:
        return all("is in the docker group" in x for x in problems)

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.ok:
            return {}
        if state.blocked:
            raise StepError(state.detail + ". Planet Express will not change accounts or the session range for you")
        p = self.p
        _approved(p, self.name, state.detail)
        passwd, groups = _passwd(_read(p.passwd)), _groups(_read(p.group))
        _gid, members = groups["docker"]
        remove = {n for n, uid, pgid in passwd if p.uid_min <= uid <= p.uid_max and n in members}
        previous = _read(p.group)
        lines = []
        for line in previous.splitlines(keepends=True):
            parts = line.rstrip("\n").split(":")
            if parts[0] == "docker" and len(parts) >= 4:
                parts[3] = ",".join(m for m in parts[3].split(",") if m and m not in remove)
                line = ":".join(parts) + ("\n" if line.endswith("\n") else "")
            lines.append(line)
        new = "".join(lines)
        mode, uid, gid_owner = _preserve(p.group)
        info = {"previous": previous, "sha256": sha256(new.encode()), "mode": mode, "uid": uid, "gid": gid_owner}
        journal.append("evidence", step=self.name, data={"files": {p.group: info}})
        write_atomic(p.group, new.encode(), mode, uid, gid_owner)
        if not self.probe().ok:
            raise StepError("the docker group was rewritten but the session accounts are still in it")
        return {"files": {p.group: info}}

    def undo(self, journal, evidence: dict) -> str:
        return _restore_files(self, evidence)


# ── (b) docker TCP ────────────────────────────────────────────────────────────────────────────────

class DockerTcpExposure(Step):
    name = "docker-tcp-exposure"

    def _daemon_json(self) -> list[str]:
        text = read_regular(self.p.daemon_json)
        if text is None:
            return []
        try:
            doc = json.loads(text.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            raise StepError(f"{self.p.daemon_json} is not valid JSON") from None
        if not isinstance(doc, dict):
            raise StepError(f"{self.p.daemon_json} is not a JSON object")
        hosts = doc.get("hosts", [])
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise StepError(f"{self.p.daemon_json}: hosts is not a list of strings")
        tls = doc.get("tlsverify") is True and bool(doc.get("tlscacert"))
        return [f"{h} (hosts in {self.p.daemon_json}, no client-verified TLS)" for h in hosts
                if h.strip().lower().startswith("tcp://") and not tls]

    def _unit_files(self) -> list[str]:
        out = []
        for root in self.p.docker_units:
            if not os.path.isdir(root):
                continue
            for dirpath, dirs, files in os.walk(root):
                if dirpath[len(root):].count(os.sep) >= 2:
                    dirs[:] = []
                    continue
                in_dropin = dirpath.endswith((".service.d", ".socket.d")) and os.path.basename(dirpath).startswith("docker")
                for name in sorted(files):
                    if in_dropin or (name.startswith("docker") and name.endswith((".service", ".socket", ".conf"))):
                        out.append(os.path.join(dirpath, name))
        return out

    def _units(self) -> list[str]:
        """TCP exposure in docker's units and drop-ins: `-H`/`--host` with tcp:// (any spelling), and any TCP
        ListenStream in a docker socket unit. An ExecStart that expands variables cannot be judged: fail closed."""
        found = []
        for path in self._unit_files():
            data = read_regular(path)
            if data is None:
                continue
            body = data[:MAX_FILE].decode("utf-8", errors="replace").replace("\\\n", " ")
            socket_unit = ".socket" in path
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", ";")):
                    continue
                if stripped.startswith("ExecStart"):
                    value = stripped.split("=", 1)[1] if "=" in stripped else ""
                    if "dockerd" in value and "$" in value:
                        raise StepError(f"cannot tell what {path} passes to dockerd (it expands a variable)")
                    if _exec_tls_verified(value):
                        continue
                    for host in _TCP_HOST.findall(value):
                        found.append(f"{host} (ExecStart in {path}, no client-verified TLS)")
                elif stripped.startswith("ListenStream") and socket_unit:
                    listen = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                    if listen and not listen.startswith(("/", "@")):
                        found.append(f"ListenStream={listen} (docker socket unit {path})")
        return found

    def _live(self) -> tuple[list[str], list[str]]:
        try:
            res = self.p.run([self.p.ss, "-H", "-ltnp"], capture_output=True, timeout=15, check=False, env=_ENV,
                             stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepError(f"cannot list the listening sockets ({type(exc).__name__})") from None
        if res.returncode != 0:
            raise StepError("cannot list the listening sockets")
        bad, proxies = [], []
        for line in res.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            # With one protocol selected `ss` omits the Netid column ("LISTEN 0 4096 local peer"); with several it
            # leads with it ("tcp LISTEN 0 4096 local peer"). Find the state, then take the local address after it.
            state = next((i for i, f in enumerate(fields[:2]) if f == "LISTEN"), None)
            if state is None or len(fields) < state + 4:
                continue
            local = fields[state + 3]
            port = local.rsplit(":", 1)[-1]
            is_daemon = 'users:(("dockerd"' in line
            if not is_daemon and not (port.isdigit() and int(port) in DOCKER_TCP_PORTS):
                continue
            if not is_daemon and int(port) in self.p.proxy_ports:
                proxies.append(local)
            else:
                bad.append(f"{local} (listening socket{', dockerd' if is_daemon else ', port ' + port})")
        return bad, proxies

    def probe(self) -> Probe:
        try:
            bad = self._daemon_json() + self._units()
            live, proxies = self._live()
        except (StepError, OSError) as exc:
            return Probe(False, f"cannot check the Docker daemon exposure: {exc}", blocked=True)
        bad += live
        if bad:
            return Probe(False, "unauthenticated Docker TCP: " + "; ".join(bad[:4]), blocked=True)
        if proxies:
            return Probe(True, f"only an acknowledged socket proxy at {', '.join(proxies)}")
        return Probe(True, "no TCP listener on the Docker daemon")

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.ok:
            return {}
        raise StepError(state.detail + ". Planet Express does not edit the Docker daemon for you: remove the tcp:// host "
                        "(daemon.json `hosts` or the unit's `-H tcp://`), or put an allowlisting socket proxy in front of "
                        "it and name its port in missions.docker_proxy_ports")

    def undo(self, journal, evidence: dict) -> str:
        return "nothing to undo: this step never changes the Docker daemon"


# ── (c) sudoers ───────────────────────────────────────────────────────────────────────────────────

_ALIAS_KINDS = ("User", "Runas", "Host", "Cmnd")
_ALIAS_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_ALIAS_SPLIT = re.compile(r"\s*:\s*(?=[A-Z][A-Z0-9_]*\s*=)")
_SKIP = re.compile(r"^(Defaults|Cmnd_Alias|Host_Alias|Runas_Alias|User_Alias|@include|#include|@includedir|#includedir)\b")


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """(first physical line, last physical line, joined text) for each non-comment logical line."""
    out, buffer, start = [], "", 0
    for number, raw in enumerate(text.splitlines()):
        if not buffer:
            start = number
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        stripped = buffer.strip()
        # `#1001 ALL=...` is a rule for uid 1001, not a comment; only `#include`, `#includedir` and plain text are.
        if stripped and not (stripped.startswith("#") and not re.match(r"^#(\d|include)", stripped)):
            out.append((start, number, stripped))
        buffer = ""
    return out


MAX_INCLUDE_DEPTH = 16
MAX_SUDOERS_FILES = 512


class SudoersManaged(Step):
    name = "sudoers-managed"

    def _files(self) -> list[str]:
        """Every file sudo would read: the main file, `sudoers.d`, and anything they `@include` / `@includedir`,
        recursively (a nested include is read, a cycle is cut, a target that cannot be resolved or read fails closed)."""
        p = self.p
        files: list[str] = []
        queue: list[tuple[str, int]] = [(p.sudoers, 0)]
        dirs: list[tuple[str, int, bool]] = [(p.sudoers_dir, 0, False)]       # (dir, depth, named by an include?)
        seen_dirs: set[str] = set()
        while queue or dirs:
            if len(files) > MAX_SUDOERS_FILES:
                raise StepError("sudoers includes too many files to check")
            if dirs:
                directory, depth, explicit = dirs.pop(0)
                if directory in seen_dirs:
                    continue
                if not os.path.isdir(directory):
                    if explicit:                  # a declared include that cannot be inspected is a failed check
                        raise StepError(f"@includedir {directory[:80]} is not a readable directory")
                    continue
                seen_dirs.add(directory)
                for name in sorted(os.listdir(directory)):
                    if "." not in name and not name.endswith("~"):          # sudo itself ignores the others
                        queue.append((os.path.join(directory, name), depth))
                continue
            path, depth = queue.pop(0)
            if path in files:
                continue
            if depth > MAX_INCLUDE_DEPTH:
                raise StepError(f"sudoers includes nest deeper than {MAX_INCLUDE_DEPTH}: {path}")
            text = _read(path)                    # an included file that cannot be read is a failed check
            files.append(path)
            for _a, _b, line in _logical_lines(text):
                match = re.match(r"^[@#]include(dir)?\s+(.+)$", line)
                if not match:
                    continue
                target = match.group(2).strip().strip('"')
                if "%" in target:
                    raise StepError(f"{path}: an include with a substitution ({target[:60]}) cannot be checked")
                if not target.startswith("/"):
                    target = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if match.group(1):
                    dirs.append((target, depth + 1, True))
                else:
                    queue.append((target, depth + 1))
        return files

    def _principals(self) -> tuple[set[str], int | None, set[int]]:
        """Names, Core's uid and the gids it belongs to: what a sudoers user token must match to reach Core."""
        p = self.p
        passwd, groups = _passwd(_read(p.passwd)), _groups(_read(p.group))
        names = {p.core_user, "ALL"}
        core = next(((u, g) for n, u, g in passwd if n == p.core_user), None)
        gids = {core[1]} if core else set()
        sessions = {n for n, u, g in passwd if p.uid_min <= u <= p.uid_max or p.uid_min <= g <= p.uid_max}
        names |= sessions                                  # session accounts, when there are named ones
        for gname, (gid, members) in groups.items():
            if (p.core_user in members or gid == (core[1] if core else None) or p.uid_min <= gid <= p.uid_max
                    or sessions & set(members)):
                names.add("%" + gname)
                gids.add(gid)
        return names, (core[0] if core else None), gids

    def _reaches(self, token: str, names: set[str], core_uid: int | None, gids: set[int]) -> bool:
        """Does one sudoers user token name Core, or any session uid or gid?"""
        p = self.p
        if token in names:
            return True
        match = re.fullmatch(r"#(\d+)", token)
        if match:
            uid = int(match.group(1))
            return uid == core_uid or p.uid_min <= uid <= p.uid_max
        match = re.fullmatch(r"%#(\d+)", token)
        if match:
            gid = int(match.group(1))
            return gid in gids or p.uid_min <= gid <= p.uid_max
        return False

    def _violations(self) -> dict[str, list[tuple[int, int, str, str]]]:
        """Rules that reach Core or any session identity and are not the generator's, plus every line this reader cannot
        parse with confidence (reported as `unparseable`: when in doubt the host is NOT clean)."""
        p = self.p
        names, core_uid, gids = self._principals()
        texts: dict[str, str] = {}
        for path in self._files():
            data = read_regular(path)
            if data is None:
                continue
            if len(data) >= MAX_FILE:
                raise StepError(f"{path} is too large to check")
            texts[path] = data.decode("utf-8", errors="replace")
        aliases: dict[str, dict[str, list[str]]] = {k: {} for k in _ALIAS_KINDS}
        for text in texts.values():
            for _a, _b, line in _logical_lines(text):
                match = re.match(r"^(User|Runas|Host|Cmnd)_Alias\s+(.*)$", line)
                if match:
                    for definition in _ALIAS_SPLIT.split(match.group(2)):
                        name, eq, members = definition.partition("=")
                        if eq and _ALIAS_NAME.fullmatch(name.strip()):
                            aliases[match.group(1)][name.strip()] = [m.strip() for m in members.split(",") if m.strip()]
        out: dict[str, list[tuple[int, int, str, str]]] = {}

        def note(path: str, first: int, last: int, line: str, kind: str) -> None:
            out.setdefault(path, []).append((first, last, line, kind))

        for path, text in texts.items():
            expected = p.managed.get(path)
            if expected is not None:
                if text.encode() != expected:
                    note(path, 0, 0, "", "managed file differs from the generator output")
                continue
            for first, last, line in _logical_lines(text):
                if _SKIP.match(line):
                    continue
                parsed = self._parse_rule(line, aliases)
                if parsed is None:
                    note(path, first, last, line, "unparseable")
                    continue
                users, cmds = parsed
                if not any(self._reaches(u, names, core_uid, gids) for u in users):
                    continue
                kind = ("wildcard" if "*" in cmds else "ALL" if re.search(r"(^|[\s,])ALL($|[\s,])", cmds)
                        else "unmanaged")
                note(path, first, last, line, kind)
        return out

    @staticmethod
    def _expand_users(token: str, table: dict[str, list[str]], seen: frozenset = frozenset()) -> list[str] | None:
        """Leaf user tokens of one user spec item, through User_Aliases. None: it cannot be resolved with confidence."""
        if token.startswith("!"):
            return []                                   # a negation never ADDS a user
        if token.startswith(("+", "%+")):
            return None                                 # a netgroup: not resolvable here
        if _ALIAS_NAME.fullmatch(token) and token != "ALL":
            if token not in table or token in seen:
                return None                             # undefined (or circular) alias
            leaves: list[str] = []
            for member in table[token]:
                inner = SudoersManaged._expand_users(member, table, seen | {token})
                if inner is None:
                    return None
                leaves += inner
            return leaves
        return [token]

    def _parse_rule(self, line: str, aliases: dict[str, dict[str, list[str]]]) -> tuple[list[str], str] | None:
        """(user leaf tokens, command text) of a `users hosts = (runas) tags: cmds` rule, or None if unsure."""
        left, eq, right = line.partition("=")
        if not eq or not right.strip():
            return None
        groups: list[str] = []
        current = ""
        for token in left.split():
            if current and (current.endswith(",") or token.startswith(",")):
                current += token
            else:
                if current:
                    groups.append(current)
                current = token
        if current:
            groups.append(current)
        if len(groups) != 2 or groups[0].endswith(",") or groups[1].endswith(","):
            return None
        users: list[str] = []
        for item in (u for u in groups[0].split(",") if u):
            leaves = self._expand_users(item, aliases["User"])
            if leaves is None:
                return None
            users += leaves
        cmds = re.sub(r"^\s*\([^)]*\)\s*", "", right)
        cmds = re.sub(r"\b[A-Z_]+:\s*", "", cmds)
        for word in re.findall(r"[A-Z][A-Z0-9_]*", cmds):
            cmds += " " + " ".join(aliases["Cmnd"].get(word, []))
        return users, cmds

    def probe(self) -> Probe:
        try:
            found = self._violations()
        except (StepError, OSError) as exc:
            return Probe(False, f"cannot check sudoers: {exc}", blocked=True)
        if not found:
            return Probe(True, "only generator-managed sudoers rules reach Core")
        parts = []
        for path, items in sorted(found.items()):
            for _a, _b, line, kind in items[:2]:
                parts.append(f"{path}: {kind}" + (f" rule {line[:70]!r}" if line else ""))
        return Probe(False, "unmanaged sudoers rules: " + "; ".join(parts[:4]))

    def repair(self, journal) -> dict:
        state = self.probe()
        if state.ok:
            return {}
        if state.blocked:
            raise StepError(state.detail)
        p = self.p
        _approved(p, self.name, state.detail)
        found = self._violations()
        evidence: dict[str, dict] = {}
        for path, items in sorted(found.items()):
            previous = (read_regular(path) or b"").decode("utf-8", errors="replace")
            expected = p.managed.get(path)
            if expected is not None:
                new = expected
            else:
                lines = previous.splitlines(keepends=True)
                for first, last, _line, _kind in items:
                    for index in range(first, last + 1):
                        lines[index] = "# removed by Planet Express: " + lines[index]
                new = "".join(lines).encode()
            mode, uid, gid = _preserve(path)
            staging = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.pe-check")
            write_atomic(staging, new, 0o440, p.owner_uid, p.owner_gid)
            try:
                if not self._visudo_ok(staging):
                    raise StepError(f"visudo -c rejects the edited {path}; nothing was installed")
            finally:
                try:
                    os.unlink(staging)
                except FileNotFoundError:
                    pass
            evidence[path] = {"previous": previous, "sha256": sha256(new), "mode": mode, "uid": uid, "gid": gid}
            journal.append("evidence", step=self.name, data={"files": dict(evidence)})
            write_atomic(path, new, mode, uid, gid)
        if not self.probe().ok:
            raise StepError("sudoers was edited but unmanaged rules still reach Core")
        return {"files": evidence}

    def _visudo_ok(self, path: str) -> bool:
        try:
            res = self.p.run([self.p.visudo, "-c", "-f", path], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=30, env=_ENV, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 0

    def undo(self, journal, evidence: dict) -> str:
        return _restore_files(self, evidence)


# ── the gate ──────────────────────────────────────────────────────────────────────────────────────

def side_door_steps(paths: SideDoorPaths) -> list[Step]:
    return [WorkerIdentity(paths), DockerTcpExposure(paths), SudoersManaged(paths)]


def repair_gate_report(steps: Sequence[Step]) -> PreflightReport:
    """What a Repair-tier start consults: one check per side door, all of which must pass. A probe that raises, or an
    empty list of steps, is a failure: the gate never opens by accident."""
    report = PreflightReport("side-doors")
    for step in steps:
        try:
            state = step.probe()
        except Exception as exc:               # noqa: BLE001
            report.checks.append(Check(step.name, False, f"the probe crashed ({type(exc).__name__})"))
            continue
        report.checks.append(Check(step.name, bool(state.ok), state.detail))
    if not steps:
        report.checks.append(Check("side-doors", False, "no side-door checks are configured"))
    return report


__all__ = ["DockerTcpExposure", "SideDoorPaths", "SudoersManaged", "WorkerIdentity", "repair_gate_report",
           "side_door_steps"]
