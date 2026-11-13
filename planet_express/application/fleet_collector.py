"""Core's collector for the other Docker hosts (docs/designs/multi-host.md, release A).

Runs in Core, not the web process: the dashboard's Beszel cache refreshed only while someone looked, and release B's findings need
the data when nobody is looking. Every COLLECT_INTERVAL_S each host listed with an `ssh:` alias is read in its own thread (one slow
host never delays another) and the whole answer is written atomically to `state/hosts.json`, which the dashboard reads the way it
reads Leela's snapshot.

A read that fails keeps the host's last good values with their own time, plus the reason: the dashboard then shows them as stale
and says why, and once they are too old, as unknown. Absent is not zero.

Locality is derived: an alias whose Docker engine ID is this host's own is refused ("that is this host"), never shown twice.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from planet_express.integrations import ssh_hosts

log = logging.getLogger("planetexpress.fleet")

COLLECT_INTERVAL_S = 60
HOST_BUDGET_S = 3 * ssh_hosts.TIMEOUT_S + 5      # three reads of at most TIMEOUT_S each (in practice ~3 s in all)
THIS_HOST = "this alias reaches PE's own Docker engine: it is this host, not another one. Remove it from multi_host.hosts."


def local_engine_id(runner=subprocess.run) -> str | None:
    try:
        proc = runner(["docker", "info", "--format", "{{.ID}}"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return (value or None) if proc.returncode == 0 else None


def read_host(alias: str, *, local_id: str | None, read=ssh_hosts.run, now: Callable[[], float] = time.time,
              docker: bool = True) -> dict:
    """One host's reading: {"read_at", "engine", "metrics", "containers"} or {"error", "cause"}. A host configured with
    `docker: false` is read for its metrics only, and then the metrics are the reading: their failure is the host's."""
    if not docker:
        try:
            metrics = ssh_hosts.parse_metrics(read(alias, "metrics"))
        except ssh_hosts.RemoteReadError as exc:
            return {"error": str(exc), "cause": exc.cause}
        # The metrics are this host's whole reading: an answer with none of them (empty output, an OS the script does not
        # know) is a failed read, never "reading" for ever with nothing behind it (Codex, rc14.10.1).
        if not any(k in metrics for k in ("cpu_pct", "mem_pct", "disk_pct", "load")):
            return {"error": f"the metrics read gave no usable values (os {metrics.get('os_kind') or 'not reported'})",
                    "cause": "no_contact"}
        return {"read_at": now(), "engine": None, "metrics": metrics, "containers": None, "docker": False}
    try:
        engine = ssh_hosts.parse_info(read(alias, "info"))
        if engine.id is None:
            return {"error": "docker info gave no engine ID", "cause": "no_contact"}
        if local_id is not None and engine.id == local_id:
            return {"error": THIS_HOST, "cause": "this_host"}
        containers = ssh_hosts.parse_containers(read(alias, "containers"))
        try:
            metrics = ssh_hosts.parse_metrics(read(alias, "metrics"))
        except ssh_hosts.RemoteReadError as exc:          # metrics are secondary: containers without them are still a reading
            metrics = {"error": str(exc)}
    except ssh_hosts.RemoteReadError as exc:
        return {"error": str(exc), "cause": exc.cause}
    return {"read_at": now(), "engine": engine.__dict__, "metrics": metrics, "containers": containers}


def merge(previous: dict | None, entry, reading: dict, now: float) -> dict:
    """The host's record: a fresh reading replaces everything; a failure keeps the last good values and adds why."""
    base = {"name": entry.name, "ssh": entry.ssh, "link": entry.link, "attempted_at": now, "docker": entry.docker}
    if "error" not in reading:
        return {**base, **reading, "error": None, "cause": None}
    if reading.get("cause") == "this_host" or not previous or previous.get("ssh") != entry.ssh:
        return {**base, "read_at": None, "engine": None, "metrics": None, "containers": None, **reading}
    kept = {k: previous.get(k) for k in ("read_at", "engine", "metrics", "containers")}
    return {**base, **kept, "error": reading["error"], "cause": reading["cause"]}


def write_text_atomic(path: Path, text: str, mode: int = 0o640) -> None:
    """Write `text` so a reader sees the whole file or the previous one. The temporary name ends in `.tmp` (never `.yml`: a
    directory Traefik watches never loads it). `mode` is set explicitly: mkstemp's 0600 would also mask a directory's inherited
    ACL, and the dashboard (another user) could not read the file (Codex, release A)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_atomic(path: Path, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, separators=(",", ":")))


class FleetCollector:
    def __init__(self, inventory, path: Path, *, read=ssh_hosts.run, local_id: Callable[[], str | None] = local_engine_id,
                 clock: Callable[[], float] = time.time, after: Callable[[dict], object] | None = None):
        self.entries = [e for e in inventory.hosts if e.ssh]
        self.path, self._read, self._local_id, self._clock = path, read, local_id, clock
        self.after = after                     # () after each pass: release D's route forwarding (remote_ingress.RemoteIngress.sync)
        self._local: str | None = None

    def previous(self) -> dict[str, dict]:
        from planet_express.application.multi_host import load_hosts_file      # one reader for the format, the page's
        return load_hosts_file(self.path)

    def collect(self) -> dict:
        if self._local is None:
            # Asked again every pass until it answers: without it an alias that reaches this host cannot be refused, and the
            # record says the comparison was not made (review, release A).
            self._local = self._local_id()
            if self._local is None:
                log.warning("could not read this host's Docker engine ID: other hosts are not checked against it this pass")
        prev = self.previous()
        results: dict[str, dict] = {}

        def one(entry):
            try:
                results[entry.ssh] = read_host(entry.ssh, local_id=self._local, read=self._read, now=self._clock,
                                               docker=entry.docker)
            except Exception as exc:  # noqa: BLE001 -- one host's surprise must not stop the others
                log.exception("fleet read of %s failed", entry.ssh)
                results[entry.ssh] = {"error": f"the read failed unexpectedly ({type(exc).__name__})", "cause": "no_contact"}

        threads = [threading.Thread(target=one, args=(e,), daemon=True, name=f"fleet-{e.ssh}") for e in self.entries]
        for t in threads:
            t.start()
        deadline = time.monotonic() + HOST_BUDGET_S
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        now = self._clock()
        # Keyed by the SSH alias, which config keeps unique; two entries may share a display name (Codex, release A).
        hosts = [{**merge(prev.get(e.ssh), e, results.get(e.ssh, {"error": f"no answer within {HOST_BUDGET_S}s",
                                                                 "cause": "no_contact"}), now),
                  "locality_checked": self._local is not None} for e in self.entries]
        data = {"collected_at": now, "hosts": hosts, "local": self._local_reading(prev_local=self._previous_local())}
        write_atomic(self.path, data)
        return data

    def _previous_local(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return data.get("local") if isinstance(data, dict) and isinstance(data.get("local"), dict) else None

    def _local_reading(self, prev_local: dict | None) -> dict:
        """This host's metrics, by the same fixed metrics read run locally (no SSH), so its Hosts card matches the others
        (rc14.12, operator: "we surface none of this for casamediaserver"). Its containers are Leela's."""
        now = self._clock()
        try:
            metrics = ssh_hosts.parse_metrics(self._run_local(ssh_hosts.READ_COMMANDS["metrics"]))
        except Exception as exc:  # noqa: BLE001 -- this host's card is never worth a pass
            kept = {k: (prev_local or {}).get(k) for k in ("read_at", "metrics", "engine")}
            return {**kept, "attempted_at": now, "error": f"the local metrics read failed ({type(exc).__name__})", "cause": "no_contact"}
        engine = None
        try:
            engine = ssh_hosts.parse_info(self._run_local(ssh_hosts.READ_COMMANDS["info"])).__dict__
        except Exception:  # noqa: BLE001
            pass
        return {"read_at": now, "attempted_at": now, "metrics": metrics, "engine": engine, "error": None, "cause": None}

    def _run_local(self, command: str) -> str:
        proc = subprocess.run(["sh", "-c", command], capture_output=True, timeout=ssh_hosts.TIMEOUT_S, stdin=subprocess.DEVNULL,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"), "LANG": "C"})
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}")
        return (proc.stdout or b"")[: ssh_hosts.MAX_OUTPUT].decode("utf-8", "replace")

    def loop(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("Fleet collector started: %d host(s) over SSH, every %ds", len(self.entries), COLLECT_INTERVAL_S)
        while not stop.is_set():
            started = time.monotonic()
            try:
                data = self.collect()
                if self.after is not None:
                    self.after(data)
            except Exception:  # noqa: BLE001 -- the loop must outlive any one pass
                log.exception("fleet collection pass failed")
            # Every COLLECT_INTERVAL_S from the start of a pass, not from its end: a slow host must not stretch every other host's
            # interval past the two minutes after which a reading reads as stale (Codex, release A).
            stop.wait(max(1.0, COLLECT_INTERVAL_S - (time.monotonic() - started)))
