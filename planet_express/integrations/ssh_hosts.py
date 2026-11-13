"""Other Docker hosts over SSH, read-only (docs/designs/multi-host.md, release A).

The one channel PE has to another host is SSH to an alias in Core's ``~/.ssh/config``. Docker access over SSH is root-equivalent
on that host, so what PE may send is not left to callers: ``READ_COMMANDS`` is a closed table of fixed remote command strings,
and ``run`` takes a NAME from it, never a command. Nothing a host returns, nothing in config but the alias (validated in
config_schema), and nothing a caller passes can become part of what runs there. Adding a command is an addition to the reviewed
surface (second review, like the step catalogue).

What is read, and what is deliberately not:
  * ``docker info``: the engine ID (locality: an alias that answers with this host's own engine is refused), version, OS, CPUs.
  * containers: ``docker inspect --format`` with named fields only. ``Config.Env`` is never read (it holds secrets); labels are
    kept only under ``traefik.`` and ``com.docker.compose.`` (routing and stack, what later releases need).
  * metrics: load, memory, CPU over a short sample, the disk Docker lives on, the hottest thermal zone. Linux and macOS.

Every answer is bounded (time, bytes) and parsed defensively: a field that is missing or malformed is None ("not reported"),
never 0, the T48 rule.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

TIMEOUT_S = 20
MAX_OUTPUT = 4 * 1024 * 1024
LABEL_PREFIXES = ("traefik.", "com.docker.compose.", "planetexpress.")   # routing, stack, and the icon override
MAX_CONTAINERS = 500

_SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR", "-T")

# A non-interactive SSH shell on macOS does not have Docker Desktop's bin directories on PATH.
_PATH = 'PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin"; '

_INSPECT = ('{"name":{{json .Name}},"image":{{json .Config.Image}},"state":{{json .State.Status}},'
            '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
            '"restarts":{{json .RestartCount}},"started":{{json .State.StartedAt}},"exit":{{json .State.ExitCode}},'
            '"labels":{{json .Config.Labels}},"ports":{{json .NetworkSettings.Ports}}}')

_INFO = ('{"id":{{json .ID}},"name":{{json .Name}},"version":{{json .ServerVersion}},"os":{{json .OperatingSystem}},'
         '"arch":{{json .Architecture}},"kernel":{{json .KernelVersion}},"ncpu":{{json .NCPU}},"mem":{{json .MemTotal}},'
         '"root":{{json .DockerRootDir}}}')

# Metrics as `key value` lines. Disk is the filesystem holding Docker's data root where it can be read (on macOS Docker runs
# in a VM, so the host's own / is reported instead).
_METRICS = r'''
os=$(uname -s)
echo "os $os"
if [ "$os" = Linux ]; then
  read l1 l5 l15 _ < /proc/loadavg; echo "load $l1 $l5 $l15"
  awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if(t) print "mem_kib", t, a}' /proc/meminfo
  c1=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); sleep 1
  c2=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); echo "cpu $c1 $c2"
  root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null); [ -d "$root" ] || root=/
  df -Pk "$root" | awk 'NR==2{print "disk_kib", $2, $3}'
  t=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1); [ -n "$t" ] && echo "temp_milli $t"
  awk -F: '/^model name/{gsub(/^ +/,"",$2); print "cpu_model", $2; exit}' /proc/cpuinfo
  awk -F= '/^PRETTY_NAME=/{gsub(/"/,"",$2); print "os_name", $2; exit}' /etc/os-release 2>/dev/null
  echo "threads $(getconf _NPROCESSORS_ONLN 2>/dev/null)"
elif [ "$os" = Darwin ]; then
  sysctl -n vm.loadavg | awk '{print "load", $2, $3, $4}'
  echo "mem_bytes $(sysctl -n hw.memsize)"
  vm_stat | awk -v ps="$(sysctl -n hw.pagesize)" '/Pages free/{f=$3} /Pages inactive/{i=$3} /Pages speculative/{s=$3} END{gsub(/\./,"",f); gsub(/\./,"",i); gsub(/\./,"",s); print "mem_avail_bytes", (f+i+s)*ps}'
  top -l 2 -n 0 -s 1 | awk '/CPU usage/{u=$3; s=$5} END{gsub(/%/,"",u); gsub(/%/,"",s); print "cpu_pct", u+s}'
  df -Pk / | awk 'NR==2{print "disk_kib", $2, $3}'
  echo "cpu_model $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
  echo "threads $(sysctl -n hw.logicalcpu 2>/dev/null)"
  echo "os_name $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"
fi
echo "uname_m $(uname -m)"
echo "uname_r $(uname -r)"
'''

READ_COMMANDS: dict[str, str] = {
    "info": _PATH + "docker info --format '" + _INFO + "'",
    # The list's own failure must fail the read: in a plain pipeline `head` and `xargs -r` would exit 0 with nothing, and a host
    # whose Docker just went away would read as "no containers" over its last good list (Codex, release A).
    # A container removed between the list and the inspect makes `docker inspect` exit 1 for the whole batch while printing the
    # rest: that is still a reading. No output at all for a non-empty list is not (review, release A).
    "containers": _PATH + "ids=$(docker ps -aq --no-trunc) || exit 3; [ -z \"$ids\" ] && exit 0; out=$(printf '%s\\n' \"$ids\" | head -n "
                  + str(MAX_CONTAINERS) + " | xargs docker inspect --format '" + _INSPECT + "' 2>/dev/null); [ -n \"$out\" ] || exit 4; "
                  "printf '%s\\n' \"$out\"",
    "metrics": _PATH + _METRICS,
}


class RemoteReadError(RuntimeError):
    """A read that did not produce an answer. `cause` is a fixed word for the renderer; the message is for the operator."""

    def __init__(self, message: str, cause: str = "no_contact"):
        super().__init__(message)
        self.cause = cause


def ssh_argv(alias: str, name: str) -> list[str]:
    """The exact argv for one read. `name` must be a key of READ_COMMANDS; `alias` was validated by config_schema."""
    command = READ_COMMANDS[name]          # KeyError for anything else: there is no other path to a remote command
    return ["ssh", *_SSH_OPTIONS, "--", alias, command]


def _env() -> dict[str, str]:
    # Core's environment holds the model key and other secrets; ssh needs HOME (for ~/.ssh) and PATH only.
    return {"HOME": os.environ.get("HOME", "/"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}


def run(alias: str, name: str, *, timeout: float = TIMEOUT_S, runner=subprocess.run) -> str:
    """Run one named read on `alias` and return its stdout, or raise RemoteReadError with a reason."""
    try:
        proc = runner(ssh_argv(alias, name), capture_output=True, timeout=timeout, env=_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise RemoteReadError(f"no answer within {int(timeout)}s") from exc
    except OSError as exc:
        raise RemoteReadError(f"could not run ssh: {exc.strerror or exc}") from exc
    if len(proc.stdout or b"") > MAX_OUTPUT:
        # Cut short, the last lines would be dropped as unparseable and a partial list would replace a whole one (Codex, release A).
        raise RemoteReadError(f"the answer was larger than {MAX_OUTPUT // (1024 * 1024)} MiB: not used")
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"")[-2000:].decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise RemoteReadError(*_ssh_failure(proc.returncode, err))
    return out


def _ssh_failure(code: int, err: str) -> tuple[str, str]:
    low = err.lower()
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return "the host key does not match the one pinned in known_hosts: collection stopped", "not_permitted"
    # Docker's own refusals first: "permission denied while trying to connect to the Docker daemon" also says "permission denied".
    if "cannot connect to the docker daemon" in low or "permission denied while trying to connect" in low:
        return "the SSH user cannot reach Docker on that host", "not_permitted"
    if "docker: command not found" in low or "docker: not found" in low:
        return "docker is not on that host's PATH", "no_contact"
    if code == 255 and "permission denied" in low:
        return "the SSH key was refused", "not_permitted"
    if "could not resolve hostname" in low:
        return "no such SSH alias or host name", "no_contact"
    if code == 255:
        return f"no SSH contact ({err.splitlines()[-1][:200] if err else 'exit 255'})", "no_contact"
    return f"the read failed (exit {code}{': ' + err.splitlines()[-1][:200] if err else ''})", "no_contact"


# ── parsing ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Engine:
    id: str | None
    name: str | None
    version: str | None
    os: str | None
    arch: str | None
    kernel: str | None
    ncpu: int | None
    mem_bytes: int | None


def _str(v, n: int = 256) -> str | None:
    return v[:n] if isinstance(v, str) and v else None


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def parse_info(text: str) -> Engine:
    try:
        d = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
    except (json.JSONDecodeError, IndexError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    return Engine(id=_str(d.get("id"), 128), name=_str(d.get("name")), version=_str(d.get("version"), 64), os=_str(d.get("os")),
                  arch=_str(d.get("arch"), 32), kernel=_str(d.get("kernel"), 128), ncpu=_int(d.get("ncpu")),
                  mem_bytes=_int(d.get("mem")))


def _ports(raw) -> list[dict]:
    """Published ports only: {"container": "8080/tcp", "host_port": 8080, "host_ip": "0.0.0.0"}."""
    out: list[dict] = []
    if not isinstance(raw, dict):
        return out
    for cport, binds in raw.items():
        if not isinstance(cport, str) or not isinstance(binds, list):
            continue
        for b in binds:
            if not isinstance(b, dict):
                continue
            hp = b.get("HostPort")
            if isinstance(hp, str) and hp.isdigit():
                out.append({"container": cport[:32], "host_port": int(hp), "host_ip": _str(b.get("HostIp"), 64)})
    return out[:64]


def parse_containers(text: str) -> list[dict]:
    """One JSON object per line from READ_COMMANDS['containers']. A line that does not parse is skipped, not guessed."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or not _str(d.get("name")):
            continue
        labels = d.get("labels") if isinstance(d.get("labels"), dict) else {}
        out.append({
            "name": d["name"].lstrip("/")[:256],
            "image": _str(d.get("image")),
            "state": _str(d.get("state"), 32),
            "health": _str(d.get("health"), 32),
            "restarts": _int(d.get("restarts")),
            "started": _str(d.get("started"), 64),
            "exit_code": d.get("exit") if isinstance(d.get("exit"), int) and not isinstance(d.get("exit"), bool) else None,
            "labels": {k[:256]: str(v)[:1024] for k, v in labels.items()
                       if isinstance(k, str) and k.startswith(LABEL_PREFIXES)},
            "ports": _ports(d.get("ports")),
        })
        if len(out) >= MAX_CONTAINERS:
            break
    return sorted(out, key=lambda c: c["name"])


def _num(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def parse_metrics(text: str) -> dict:
    """`key value` lines into the fields HostMetrics and HostDetails take. Anything unreadable is left out (None)."""
    kv: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            kv[parts[0]] = parts[1].split() if parts[0] not in ("cpu_model", "os_name") else [parts[1].strip()]
    m: dict = {"os_kind": (kv.get("os") or [None])[0]}
    load = [_num(x) for x in kv.get("load", [])[:3]]
    if len(load) == 3 and None not in load:
        m["load"] = load
    gib = 1024 ** 3
    if len(kv.get("mem_kib", [])) == 2:
        t, a = (_num(x) for x in kv["mem_kib"])
        if t and a is not None and 0 <= a <= t:
            m.update(mem_total_gib=t * 1024 / gib, mem_used_gib=(t - a) * 1024 / gib, mem_pct=100 * (t - a) / t)
    elif kv.get("mem_bytes") and kv.get("mem_avail_bytes"):
        t, a = _num(kv["mem_bytes"][0]), _num(kv["mem_avail_bytes"][0])
        if t and a is not None and 0 <= a <= t:
            m.update(mem_total_gib=t / gib, mem_used_gib=(t - a) / gib, mem_pct=100 * (t - a) / t)
    cpu = [_num(x) for x in kv.get("cpu", [])]
    if len(cpu) == 4 and None not in cpu and cpu[2] > cpu[0]:
        total, idle = cpu[2] - cpu[0], cpu[3] - cpu[1]
        m["cpu_pct"] = max(0.0, min(100.0, 100 * (total - idle) / total))
    elif kv.get("cpu_pct"):
        v = _num(kv["cpu_pct"][0])
        if v is not None and 0 <= v <= 100:
            m["cpu_pct"] = v
    if len(kv.get("disk_kib", [])) == 2:
        t, u = (_num(x) for x in kv["disk_kib"])
        if t and u is not None and 0 <= u <= t:
            m.update(disk_total_gib=t * 1024 / gib, disk_used_gib=u * 1024 / gib, disk_pct=100 * u / t)
    if kv.get("temp_milli"):
        v = _num(kv["temp_milli"][0])
        if v is not None and 0 < v < 150_000:
            m["temp_c"] = v / 1000
    if kv.get("cpu_model") and kv["cpu_model"][0]:
        m["cpu_model"] = kv["cpu_model"][0][:128]
    # What the host says it is, for a host with no Docker engine to ask (a metrics-only host, live: SolarAssistant).
    for key, out in (("os_name", "os_name"), ("uname_m", "arch"), ("uname_r", "kernel")):
        if kv.get(key) and kv[key][0]:
            m[out] = kv[key][0][:128]
    if kv.get("threads"):
        v = _num(kv["threads"][0])
        if v and v == int(v):
            m["threads"] = int(v)
    return m
