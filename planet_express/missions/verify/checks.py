"""The verification check catalogue (contract 10): eight typed checks, each Core-executed.

Every check is a strict params model plus a function `(params, env, timeout_s) -> Outcome`. The rules all of them share:

* argv only, never a shell; every external call is bounded in time and in output bytes;
* a check that cannot prove its claim FAILS (an unreadable project, a truncated log, a missing allowlist, an unknown
  container are failures, never passes), and an exception inside a check is a failure;
* a Docker `healthy` string never satisfies anything on its own: `container_state` also needs `running`, not
  restarting, a stable start time, and the in-container probes are separate checks;
* the detail a check reports is a short fixed phrase plus numbers and names. Command output, log text and response
  bodies are matched against, never copied.

`run_check` adds the retry loop and a hard backstop deadline around each attempt.
"""

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..canonical import CanonicalError, loads_strict
from ..models import VerificationCheck
from .plan import CheckResult

MAX_TIMEOUT_S = 60
MAX_RETRIES = 3
HTTP_MAX_BODY = 64 * 1024
FILE_MAX_BYTES = 8 * 1024 * 1024
REGEX_MAX_PATTERN = 128
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHELLS = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish", "busybox", "env"})

BoundedRun = Callable[[list[str], int, int], tuple[int, str, str, bool]]


# ── params ───────────────────────────────────────────────────────────────────────────────────────────

class _P(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Timeout = Annotated[StrictInt, Field(ge=1, le=MAX_TIMEOUT_S)]
Retries = Annotated[StrictInt, Field(ge=0, le=MAX_RETRIES)]
Literal256 = Annotated[StrictStr, Field(min_length=1, max_length=256)]


def _name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError("not a container or stack name")
    return value


def _abs(value: str) -> str:
    if not value.startswith("/") or "\0" in value or ".." in value.split("/") or len(value) > 512:
        raise ValueError("not an absolute path without '..'")
    return value


class HttpParams(_P):
    url: Annotated[StrictStr, Field(max_length=512)]
    resolve_to: StrictStr | None = None          # connect here, send the real Host and SNI
    expect_status: Annotated[StrictInt, Field(ge=100, le=599)] = 200
    expect_body: Literal256 | None = None        # a LITERAL substring: no regex in this check
    consecutive: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    interval_s: Annotated[StrictInt, Field(ge=0, le=10)] = 1
    timeout_s: Timeout = 10
    retries: Retries = 0

    @field_validator("url")
    @classmethod
    def _url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("a plain http(s) URL without credentials")
        if any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError("no whitespace or control characters in a URL")
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("bad port") from None
        return url

    @field_validator("resolve_to")
    @classmethod
    def _resolve(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.ip_address(value)          # an address literal, never a name
        return value


class ExecProbeParams(_P):
    container: StrictStr
    argv: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=256)]], Field(min_length=1, max_length=16)]
    expect_contains: Literal256 | None = None
    timeout_s: Timeout = 10
    retries: Retries = 0

    @field_validator("container")
    @classmethod
    def _c(cls, v: str) -> str:
        return _name(v)

    @field_validator("argv")
    @classmethod
    def _argv(cls, argv: list[str]) -> list[str]:
        if any("\0" in a for a in argv) or argv[0].startswith("-"):
            raise ValueError("argv has NUL or starts with an option")
        return argv


class ContainerStateParams(_P):
    container: StrictStr
    min_uptime_s: Annotated[StrictInt, Field(ge=0, le=3600)] = 10
    observe_s: Annotated[StrictInt, Field(ge=0, le=30)] = 3      # a second look: no restart may happen inside it
    timeout_s: Timeout = 15
    retries: Retries = 0

    @field_validator("container")
    @classmethod
    def _c(cls, v: str) -> str:
        return _name(v)


class MountReadableParams(_P):
    path: StrictStr
    container: StrictStr | None = None           # None: readable by Core itself
    timeout_s: Timeout = 10
    retries: Retries = 0

    @field_validator("path")
    @classmethod
    def _p(cls, v: str) -> str:
        return _abs(v)

    @field_validator("container")
    @classmethod
    def _c(cls, v: str | None) -> str | None:
        return None if v is None else _name(v)


class ProjectCompleteParams(_P):
    stack: StrictStr
    timeout_s: Timeout = 30
    retries: Retries = 0

    @field_validator("stack")
    @classmethod
    def _s(cls, v: str) -> str:
        return _name(v)


class LogAbsentParams(_P):
    container: StrictStr
    pattern: Literal256
    window_s: Annotated[StrictInt, Field(ge=1, le=3600)] = 300
    regex: StrictBool = False                    # off: `pattern` is a literal substring
    timeout_s: Timeout = 20
    retries: Retries = 0

    @field_validator("container")
    @classmethod
    def _c(cls, v: str) -> str:
        return _name(v)

    @model_validator(mode="after")
    def _pattern(self) -> "LogAbsentParams":
        if "\0" in self.pattern:
            raise ValueError("pattern has NUL")
        if self.regex:
            if len(self.pattern) > REGEX_MAX_PATTERN:
                raise ValueError(f"a regex is at most {REGEX_MAX_PATTERN} characters")
            try:
                re.compile(self.pattern)
            except re.error:
                raise ValueError("pattern is not a valid regex") from None
        return self


class FileStateParams(_P):
    path: StrictStr
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    mode: Annotated[StrictStr, Field(pattern=r"^[0-7]{3,4}$")] | None = None
    timeout_s: Timeout = 10
    retries: Retries = 0

    @field_validator("path")
    @classmethod
    def _p(cls, v: str) -> str:
        return _abs(v)

    @model_validator(mode="after")
    def _one(self) -> "FileStateParams":
        if self.sha256 is None and self.mode is None:
            raise ValueError("file_state needs sha256 or mode")
        return self


class QueryParams(_P):
    tool: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    arguments_json: Annotated[StrictStr, Field(max_length=2048)] = "{}"
    expect_contains: Literal256 | None = None
    timeout_s: Timeout = 20
    retries: Retries = 0

    @field_validator("arguments_json")
    @classmethod
    def _args(cls, text: str) -> str:
        args = _query_args(text)
        if args is None:
            raise ValueError("arguments_json must be a flat JSON object of strings, ints and bools")
        return text


def _query_args(text: str) -> dict[str, Any] | None:
    try:
        args = loads_strict(text)
    except CanonicalError:
        return None
    if not isinstance(args, dict) or any(not isinstance(v, str | int | bool) for v in args.values()):
        return None
    return args


PARAM_MODELS: dict[str, type[_P]] = {
    "http": HttpParams, "exec_probe": ExecProbeParams, "container_state": ContainerStateParams,
    "mount_readable": MountReadableParams, "project_complete": ProjectCompleteParams,
    "log_absent": LogAbsentParams, "file_state": FileStateParams, "query": QueryParams,
}


# ── environment ──────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HttpRequest:
    scheme: str
    host: str
    port: int
    target: str
    resolve_to: str | None
    timeout_s: float
    max_body: int = HTTP_MAX_BODY


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    truncated: bool = False


def host_header_for(host: str, port: int, default_port: int) -> str:
    """The Host header value: an IPv6 literal is bracketed (`[::1]:8080`), the port is dropped when it is the default."""
    name = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return name if port == default_port else f"{name}:{port}"


def default_http(req: HttpRequest) -> HttpResponse:
    """GET, no redirects, bounded body and wall-clock. With `resolve_to` the TCP connection goes to that address while
    the Host header and the TLS SNI/verification name stay the real host (a route probe against one specific backend)."""
    deadline = time.monotonic() + req.timeout_s
    sock = socket.create_connection((req.resolve_to or req.host, req.port), timeout=req.timeout_s)
    try:
        if req.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=req.host)
        default_port = 443 if req.scheme == "https" else 80
        host_header = host_header_for(req.host, req.port, default_port)
        conn = http.client.HTTPConnection(req.host, req.port, timeout=req.timeout_s)
        conn.sock = sock                      # an already-connected socket: http.client will not dial again
        conn.request("GET", req.target, headers={"Host": host_header, "User-Agent": "planet-express-verify",
                                                 "Accept-Encoding": "identity", "Connection": "close"})
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        resp = conn.getresponse()
        chunks, size, truncated = [], 0, False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("deadline")
            try:
                sock.settimeout(remaining)       # tighten the per-read timeout to what is left of the deadline
            except OSError:
                pass                              # http.client closed it after the last byte: the read below returns b""
            chunk = resp.read(4096)
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
            if size >= req.max_body:
                truncated = True
                break
        return HttpResponse(resp.status, b"".join(chunks)[:req.max_body], truncated)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def host_readable(path: str) -> bool:
    """Can Core itself list the directory (or read a byte of the file)? Any error is False."""
    try:
        st = os.stat(path)
        if stat.S_ISDIR(st.st_mode):
            with os.scandir(path) as it:
                next(it, None)
            return True
        if stat.S_ISREG(st.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                os.read(fd, 1)
            finally:
                os.close(fd)
            return True
    except OSError:
        pass
    return False


def default_run(argv: list[str], timeout: int, max_bytes: int) -> tuple[int, str, str, bool]:
    import casa_bender as bender
    return bender.run_argv_bounded(argv, timeout, max_bytes)


@dataclass(frozen=True)
class CheckEnv:
    run: BoundedRun = default_run
    http: Callable[[HttpRequest], HttpResponse] = default_http
    host_readable: Callable[[str], bool] = host_readable
    exec_allowlist: frozenset[str] = frozenset()      # Core config `missions.verify.exec_allowlist`; empty = unavailable
    timeout_cap_s: int = MAX_TIMEOUT_S
    max_retries: int = MAX_RETRIES
    max_output_bytes: int = 64 * 1024
    regex_deadline_s: float = 5.0
    backstop_grace_s: float = 5.0
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    provider: Any = None                              # a checkpoint provider: compose_chain / project_name / compose_argv
    broker: Any = None                                # `tools.ToolBroker`: only its read-only `core_read` is used
    mission_id: str = ""
    python: str = field(default_factory=lambda: sys.executable)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str = ""


def _fail(detail: str) -> Outcome:
    return Outcome(False, detail[:200])


def _pass(detail: str = "ok") -> Outcome:
    return Outcome(True, detail[:200])


# ── the checks ───────────────────────────────────────────────────────────────────────────────────────

def check_http(p: HttpParams, env: CheckEnv, timeout_s: int) -> Outcome:
    parts = urlsplit(p.url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    req = HttpRequest(parts.scheme, parts.hostname or "", port, target, p.resolve_to, float(timeout_s))
    for i in range(p.consecutive):
        if i and p.interval_s:
            env.sleep(p.interval_s)
        try:
            resp = env.http(req)
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            return _fail(f"probe {i + 1}/{p.consecutive}: connection failed ({type(exc).__name__})")
        if resp.status != p.expect_status:
            return _fail(f"probe {i + 1}/{p.consecutive}: status {resp.status}, wanted {p.expect_status}")
        if p.expect_body is not None and p.expect_body.encode("utf-8") not in resp.body:
            return _fail(f"probe {i + 1}/{p.consecutive}: status ok but the body does not match")
    return _pass(f"{p.consecutive} consecutive probes matched")


def _exec_available(env: CheckEnv, argv0: str) -> str | None:
    if not env.exec_allowlist:
        return "exec_probe is unavailable: missions.verify.exec_allowlist is empty"
    if argv0 not in env.exec_allowlist:
        return "binary is not in the operator's exec allowlist"
    return None


def check_exec_probe(p: ExecProbeParams, env: CheckEnv, timeout_s: int) -> Outcome:
    # KNOWN LIMIT: on a timeout the runner kills the `docker exec` CLIENT, not the process it started inside the
    # container, which may keep running. The probes are operator-allowlisted binaries chosen to be short and read-only.
    refusal = _exec_available(env, p.argv[0])
    if refusal:
        return _fail(refusal)
    rc, out, _err, _trunc = env.run(["docker", "exec", p.container, *p.argv], timeout_s, env.max_output_bytes)
    if rc != 0:
        return _fail(f"probe exited {rc}")
    if p.expect_contains is not None and p.expect_contains not in out:
        return _fail("probe exited 0 but its output does not match")
    return _pass("probe exited 0")


def _inspect(env: CheckEnv, container: str, timeout_s: int) -> dict[str, Any] | None:
    rc, out, _err, trunc = env.run(["docker", "inspect", "--type", "container", container], timeout_s, 256 * 1024)
    if rc != 0 or trunc:
        return None
    try:
        doc = json.loads(out)
        doc = doc[0] if isinstance(doc, list) and len(doc) == 1 else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _started_epoch(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z", text)
    if not match:
        return None
    return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc).timestamp()


def _state_problem(doc: dict[str, Any]) -> str | None:
    state = doc.get("State")
    if not isinstance(state, dict):
        return "no State in inspect"
    if state.get("Status") != "running" or state.get("Running") is not True:
        return f"not running ({str(state.get('Status'))[:20]})"
    if state.get("Restarting") or state.get("Paused") or state.get("Dead"):
        return "restarting, paused or dead"
    config = doc.get("Config") if isinstance(doc.get("Config"), dict) else {}
    test = (config.get("Healthcheck") or {}).get("Test") if isinstance(config.get("Healthcheck"), dict) else None
    defined = isinstance(state.get("Health"), dict) or (isinstance(test, list) and test and test[0] != "NONE")
    if defined:
        health = state.get("Health")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            return f"health is {str((health or {}).get('Status'))[:20]}, not healthy"
    return None


def check_container_state(p: ContainerStateParams, env: CheckEnv, timeout_s: int) -> Outcome:
    first = _inspect(env, p.container, timeout_s)
    if first is None:
        return _fail("container could not be inspected")
    problem = _state_problem(first)
    if problem:
        return _fail(problem)
    started = _started_epoch(first["State"].get("StartedAt"))
    if started is None:
        return _fail("start time is unreadable")
    if env.now() - started < p.min_uptime_s:
        return _fail(f"up for less than {p.min_uptime_s}s")
    if p.observe_s:
        env.sleep(p.observe_s)
        second = _inspect(env, p.container, timeout_s)
        if second is None:
            return _fail("container could not be inspected on the second look")
        problem = _state_problem(second)
        if problem:
            return _fail(problem)
        if (second["State"].get("StartedAt") != first["State"].get("StartedAt")
                or second.get("RestartCount") != first.get("RestartCount")):
            return _fail("the container restarted while it was observed")
    return _pass("running, stable" + (", healthy" if isinstance(first["State"].get("Health"), dict) else ""))


def check_mount_readable(p: MountReadableParams, env: CheckEnv, timeout_s: int) -> Outcome:
    if p.container is None:
        return _pass("readable by Core") if env.host_readable(p.path) else _fail("not readable by Core")
    rc, _out, _err, _trunc = env.run(["docker", "exec", p.container, "ls", "--", p.path], timeout_s,
                                     env.max_output_bytes)
    return _pass("readable inside the consumer") if rc == 0 else _fail(f"not readable inside the consumer (exit {rc})")


def check_project_complete(p: ProjectCompleteParams, env: CheckEnv, timeout_s: int) -> Outcome:
    provider = env.provider
    if provider is None:
        return _fail("no compose provider")
    chain = provider.compose_chain(p.stack)
    project = provider.project_name(p.stack)
    if not chain or not project:
        return _fail("the compose project is unknown or unreadable")
    files = [arg for c in chain for arg in ("-f", c.real_path)]
    rc, out, _err, trunc = env.run([*provider.compose_argv, *files, "config", "--services"], timeout_s, 64 * 1024)
    defined = sorted({line.strip() for line in out.splitlines() if line.strip()})
    if rc != 0 or trunc or not defined:
        return _fail("the project's services could not be read")
    fmt = '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}'
    rc, out, _err, trunc = env.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                                    "--format", fmt], timeout_s, env.max_output_bytes)
    if rc != 0 or trunc:
        return _fail("the project's containers could not be listed")
    rows: dict[str, list[tuple[str, str]]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != 3:
            return _fail("container listing is malformed")
        rows.setdefault(cols[0], []).append((cols[1], cols[2]))
    bad = []
    for service in defined:
        seen = rows.get(service)
        if not seen:
            bad.append(f"{service}: absent")
        elif any(state != "running" or "unhealthy" in status or "starting" in status for state, status in seen):
            bad.append(f"{service}: not running and healthy")
    if bad:
        return _fail("; ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""))
    return _pass(f"all {len(defined)} services run")


_REGEX_CHILD = r"""
import re, resource, sys
cpu = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
pattern, _, text = sys.stdin.buffer.read().partition(b"\0")
try:
    found = re.search(pattern.decode("utf-8", "replace"), text.decode("utf-8", "replace"), re.M) is not None
except re.error:
    sys.exit(3)
sys.stdout.write("1" if found else "0")
"""


def regex_search(pattern: str, text: str, deadline_s: float, python: str) -> bool | None:
    """Search in a killable child process with a hard wall-clock and CPU deadline. None means no verdict (timeout, crash,
    bad output): the caller treats that as a failure. The stdlib `re` is never run inside Core's own interpreter."""
    payload = pattern.encode("utf-8") + b"\0" + text.encode("utf-8", "replace")
    try:
        done = subprocess.run(
            [python, "-I", "-c", _REGEX_CHILD, str(max(1, int(deadline_s)))], input=payload, capture_output=True,
            timeout=deadline_s, env={}, close_fds=True, start_new_session=True, check=False)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if done.returncode != 0 or done.stdout not in (b"0", b"1"):
        return None
    return done.stdout == b"1"


def check_log_absent(p: LogAbsentParams, env: CheckEnv, timeout_s: int) -> Outcome:
    rc, out, err, truncated = env.run(["docker", "logs", "--since", f"{p.window_s}s", p.container], timeout_s,
                                      env.max_output_bytes)
    if rc != 0:
        return _fail(f"logs could not be read (exit {rc})")
    text = out + "\n" + err
    if p.regex:
        found = regex_search(p.pattern, text, min(env.regex_deadline_s, float(timeout_s)), env.python)
        if found is None:
            return _fail("the regex search gave no verdict (timeout, limit or error)")
    else:
        found = p.pattern in text
    if found:
        return _fail("the pattern appears in the window")
    if truncated:
        return _fail(f"the window holds more than {env.max_output_bytes} bytes: absence cannot be shown")
    return _pass(f"absent in the last {p.window_s}s")


def check_file_state(p: FileStateParams, env: CheckEnv, timeout_s: int) -> Outcome:
    try:
        fd = os.open(p.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return _fail("file is absent or not openable (a symlink is refused)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _fail("not a regular file")
        if p.mode is not None and stat.S_IMODE(st.st_mode) != int(p.mode, 8):
            return _fail("mode differs")
        if p.sha256 is not None:
            if st.st_size > FILE_MAX_BYTES:
                return _fail("file is too large to hash")
            digest, read, deadline = sha256(), 0, time.monotonic() + timeout_s
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                read += len(chunk)
                digest.update(chunk)
                if read > FILE_MAX_BYTES or time.monotonic() > deadline:
                    return _fail("file is too large or too slow to hash")
            if digest.hexdigest() != p.sha256:
                return _fail("sha256 differs")
    except OSError:
        return _fail("file could not be read")
    finally:
        os.close(fd)
    return _pass("matches")


def check_query(p: QueryParams, env: CheckEnv, timeout_s: int) -> Outcome:
    if env.broker is None or not env.mission_id:
        return _fail("no tool broker")
    args = _query_args(p.arguments_json)
    if args is None:
        return _fail("bad arguments")
    try:
        result = env.broker.core_read(env.mission_id, p.tool, args)
    except Exception as exc:                  # noqa: BLE001 - a refusal or a handler failure is a failed check, never a crash
        return _fail(f"the broker refused or failed ({type(exc).__name__})")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return _fail("the tool did not accept the query")
    if p.expect_contains is not None and p.expect_contains not in str(result.get("output", "")):
        return _fail("the answer does not match")
    return _pass("answer matches")


FUNCS: dict[str, Callable[[Any, CheckEnv, int], Outcome]] = {
    "http": check_http, "exec_probe": check_exec_probe, "container_state": check_container_state,
    "mount_readable": check_mount_readable, "project_complete": check_project_complete,
    "log_absent": check_log_absent, "file_state": check_file_state, "query": check_query,
}


# ── running one check ────────────────────────────────────────────────────────────────────────────────

def _budget_s(params: _P, timeout_s: int, grace_s: float) -> float:
    """A hard ceiling for one attempt: the check's own timeouts are the real bound, this is the backstop."""
    if isinstance(params, HttpParams):
        return (timeout_s + params.interval_s) * params.consecutive + grace_s
    if isinstance(params, ContainerStateParams):
        return timeout_s * 2 + params.observe_s + grace_s
    if isinstance(params, ProjectCompleteParams):
        return timeout_s * 3 + grace_s
    return timeout_s * 2 + grace_s


def _call_with_deadline(fn: Callable[[], Outcome], budget_s: float) -> Outcome:
    box: list[Outcome] = []

    def target() -> None:
        try:
            box.append(fn())
        except Exception as exc:              # noqa: BLE001 - any error inside a check is a failure
            box.append(_fail(f"error ({type(exc).__name__})"))

    thread = threading.Thread(target=target, daemon=True, name="verify-check")
    thread.start()
    thread.join(budget_s)
    return box[0] if box else _fail("timed out")


def run_check(check: VerificationCheck, env: CheckEnv) -> CheckResult:
    """One check, with its retries. Never raises: whatever goes wrong is `ok=False` with a short reason."""
    model, fn = PARAM_MODELS.get(check.check), FUNCS.get(check.check)
    if model is None or fn is None:
        return CheckResult(id=check.id, check=check.check[:32], ok=False, attempts=0, detail="unknown check kind")
    try:
        params = model.model_validate(check.params)
    except ValueError:
        return CheckResult(id=check.id, check=check.check, ok=False, attempts=0, detail="bad params")
    timeout_s = max(1, min(params.timeout_s, env.timeout_cap_s, MAX_TIMEOUT_S))     # type: ignore[attr-defined]
    retries = max(0, min(params.retries, env.max_retries, MAX_RETRIES))              # type: ignore[attr-defined]
    outcome = _fail("not run")
    attempts = 0
    for attempt in range(1 + retries):
        attempts = attempt + 1
        outcome = _call_with_deadline(lambda: fn(params, env, timeout_s), _budget_s(params, timeout_s, env.backstop_grace_s))
        if outcome.ok:
            break
        if attempt < retries:
            env.sleep(1)
    return CheckResult(id=check.id, check=check.check, ok=outcome.ok, attempts=attempts, detail=outcome.detail)


def env_from_config(cfg: Any, *, provider: Any = None, broker: Any = None, run: BoundedRun | None = None) -> CheckEnv:
    """The check environment from `missions.verify` (config_schema.VerifyConfig). The allowlist is the operator's and
    is empty by default, which leaves `exec_probe` unavailable."""
    return CheckEnv(
        run=run or default_run, exec_allowlist=frozenset(cfg.exec_allowlist), timeout_cap_s=cfg.check_timeout_cap_s,
        max_retries=cfg.max_retries, max_output_bytes=cfg.max_output_bytes, provider=provider, broker=broker)
