"""The other hosts, as the Hosts page and the Overview show them (docs/designs/multi-host.md).

Core reads every host with an `ssh:` alias every minute and writes `state/hosts.json` (application/fleet_collector.py); this
module turns that file into cards. It does no I/O but reading that file, and never reaches a host itself. Beszel, the collector
before release C, is gone: an entry without an alias is shown with its link only.
"""

import json
import time
from dataclasses import dataclass

from config_schema import MultiHostConfig
from planet_express.core.hosts import (
    NOT_PERMITTED,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    liveness,
    unknown,
)


@dataclass(frozen=True)
class HostCard:
    """One deliberately presentation-friendly, observation-only host row."""

    id: str
    name: str
    link: str | None
    configured: bool
    liveness: Liveness
    details: HostDetails | None = None
    metrics: HostMetrics | None = None
    containers: tuple[RemoteContainer, ...] | None = None
    containers_liveness: Liveness | None = None
    status: str | None = None
    source: str = "ssh"                  # or "link": an entry PE does not read
    ssh: str | None = None
    problem: str | None = None           # ssh: the collector's fixed cause word (this_host, not_permitted, no_contact, not_trying)
    locality_checked: bool = True        # ssh: whether the reading was compared with this host's own Docker engine
    docker: bool = True                  # False: a metrics-only host (`docker: false` in config); it has no containers to show
    routes: tuple = ()                   # release D: the routes PE wrote for this host ({container, hosts, url})
    unrouted: tuple = ()                 # release D: labelled routers PE did not write, with why ({container, router, reason})
    local_containers: int | None = None  # this host's card: its container count from Leela's snapshot (None: not read)


@dataclass(frozen=True)
class FleetView:
    cards: tuple[HostCard, ...]


# ── the SSH collector's hosts (release A): Core writes state/hosts.json, the page reads it ──────────────────────────────────────

NOT_COLLECTED = "Core has not read this host yet (the collector runs every minute while Core is up)"
# A reading older than this is not drawn at all, even as "last known" (review, release A: stale for ever hid a dead host).
SSH_UNKNOWN_AFTER = 15 * 60
# Core tries every host every minute; no try for this long means the collector itself is not running.
SSH_NOT_TRYING_AFTER = 5 * 60


def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _port_text(ports) -> str | None:
    if not isinstance(ports, list):
        return None
    text = ", ".join(f"{p['host_port']}→{p['container']}" for p in ports
                     if isinstance(p, dict) and isinstance(p.get("host_port"), int) and isinstance(p.get("container"), str))
    return text[:256] or None


def ssh_card(entry, record: dict | None, now: float) -> HostCard:
    """One SSH host's card from its record in hosts.json (or none yet)."""
    def blank(reason, problem=None, cause=None):
        return HostCard(entry.ssh, entry.name, entry.link, True, unknown(reason, cause), containers_liveness=unknown(reason, cause),
                        source="ssh", ssh=entry.ssh, problem=problem, docker=entry.docker)

    if not isinstance(record, dict) or record.get("ssh") != entry.ssh:
        return blank(NOT_COLLECTED)
    error = record.get("error") if isinstance(record.get("error"), str) else None
    problem = record.get("cause") if isinstance(record.get("cause"), str) else None
    cause = NOT_PERMITTED if problem in ("not_permitted", "this_host") else None
    attempted = _f(record.get("attempted_at"))
    if attempted is None or now - attempted > SSH_NOT_TRYING_AFTER:
        return blank("Core has not tried this host for " + (f"{int((now - attempted) // 60)} min" if attempted else "a while")
                     + ": is Core running?", "not_trying")
    read_at = _f(record.get("read_at"))
    if read_at is None:
        return blank(error or "no reading", problem, cause)
    if now - read_at > SSH_UNKNOWN_AFTER:
        return blank(f"no good reading for {int((now - read_at) // 60)} min" + (f": {error}" if error else ""), problem, cause)
    live = liveness(read_at, now=now, reason=error)
    engine = record.get("engine") if isinstance(record.get("engine"), dict) else {}
    m = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
    load = m.get("load")
    temp = _f(m.get("temp_c"))
    metrics = HostMetrics(cpu_pct=_f(m.get("cpu_pct")), mem_pct=_f(m.get("mem_pct")), mem_used_gib=_f(m.get("mem_used_gib")),
                          mem_total_gib=_f(m.get("mem_total_gib")), disk_pct=_f(m.get("disk_pct")),
                          disk_used_gib=_f(m.get("disk_used_gib")), disk_total_gib=_f(m.get("disk_total_gib")),
                          load=tuple(_f(x) for x in load) if isinstance(load, list) and load and all(_f(x) is not None for x in load) else None,
                          temps={"hottest zone": temp} if temp is not None else None)
    text = lambda v: v if isinstance(v, str) else None          # noqa: E731
    integer = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None          # noqa: E731
    arch = text(engine.get("arch")) or text(m.get("arch"))
    os_name = text(engine.get("os")) or text(m.get("os_name"))
    if os_name and arch and os_name.endswith(" " + arch):          # Unraid's "Unraid OS 7.3 x86_64": the arch is shown on its own
        os_name = os_name[: -len(arch) - 1]
    details = HostDetails(cores=None, threads=integer(m.get("threads")) or integer(engine.get("ncpu")), arch=arch,
                          kernel=text(engine.get("kernel")) or text(m.get("kernel")), cpu_model=text(m.get("cpu_model")),
                          memory_bytes=integer(engine.get("mem_bytes")), os_name=os_name)
    raw = record.get("containers")
    containers = None if not isinstance(raw, list) else tuple(
        RemoteContainer(name=c["name"], image=text(c.get("image")), status=text(c.get("state")), health=text(c.get("health")),
                        ports=_port_text(c.get("ports")), restarts=integer(c.get("restarts")))
        for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str))
    return HostCard(entry.ssh, entry.name, entry.link, True, live, details=details, metrics=metrics,
                    containers=containers if entry.docker else None, containers_liveness=live, status=error, source="ssh",
                    ssh=entry.ssh, problem=problem if error else None,
                    # A metrics-only host has no engine to compare: nothing to say about it.
                    locality_checked=record.get("locality_checked") is not False or not entry.docker, docker=entry.docker)


def load_hosts_file(path) -> dict[str, dict]:
    """hosts.json by SSH alias (unique in config); {} when it is absent or unreadable (every SSH host then reads as not collected yet)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    return {h["ssh"]: h for h in hosts or [] if isinstance(h, dict) and isinstance(h.get("ssh"), str)}


def _safe_card(entry, record, now) -> HostCard:
    """A record the page cannot read is that host's problem, never the page's (review, release A)."""
    try:
        return ssh_card(entry, record, now)
    except Exception:  # noqa: BLE001
        return HostCard(entry.ssh, entry.name, entry.link, True, unknown("Core's record for this host could not be read"),
                        containers_liveness=unknown("Core's record for this host could not be read"), source="ssh", ssh=entry.ssh)


def load_routes_file(path) -> dict[str, dict]:
    """state/remote_routes.json (release D) by alias: {"routes": [...], "unrouted": [...]}; {} when absent or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    for key, kind in (("routes", "routes"), ("skipped", "unrouted")):
        for item in ((data.get(key) or []) if isinstance(data, dict) else []):
            if isinstance(item, dict) and isinstance(item.get("host"), str):
                out.setdefault(item["host"], {"routes": [], "unrouted": []})[kind].append(
                    {k: item.get(k) for k in ("container", "router", "hosts", "url", "reason", "kept")})
    return out


class Fleet:
    """The Hosts page's view: every configured host in inventory order, SSH hosts from Core's file, the rest as links."""

    def __init__(self, inventory: MultiHostConfig, hosts_file, *, wall=time.time, local_containers=None):
        self._inventory, self._file, self._wall = inventory, hosts_file, wall
        self._local_containers = local_containers          # () -> this host's container count (Leela's snapshot), or None

    def view(self) -> FleetView:
        import dataclasses
        import socket
        records = load_hosts_file(self._file) if any(e.ssh for e in self._inventory.hosts) else {}
        routes = load_routes_file(self._file.parent / "remote_routes.json") if self._inventory.ingress.enabled else {}
        now = self._wall()
        try:
            local = json.loads(self._file.read_text()).get("local")
        except (OSError, ValueError, AttributeError):
            local = None
        count = None
        if self._local_containers is not None:
            try:
                count = self._local_containers()
            except Exception:  # noqa: BLE001 -- a count is never worth the page
                count = None
        engine_name = ((local or {}).get("engine") or {}).get("name") if isinstance(local, dict) else None
        cards = [local_card(local, now, name=engine_name if isinstance(engine_name, str) and engine_name else socket.gethostname(),
                            containers=count)]
        for entry in self._inventory.hosts:
            if entry.ssh:
                card = _safe_card(entry, records.get(entry.ssh), now)
                mine = routes.get(entry.ssh)
                if mine:
                    card = dataclasses.replace(card, routes=tuple(mine["routes"]), unrouted=tuple(mine["unrouted"]))
                cards.append(card)
            else:
                cards.append(HostCard(entry.name, entry.name, entry.link, True,
                                      unknown("PE does not read this host: add an ssh alias to read its containers"),
                                      source="link"))
        return FleetView(tuple(cards))


def remote_summary(inventory: MultiHostConfig, hosts_file, *, wall=time.time) -> dict | None:
    """For the Overview tile: containers on the SSH hosts with a fresh reading, their problems by the same rule as the alerts and
    incidents (remote_health.container_issue), and how many hosts are not reading now."""
    from planet_express.application.remote_health import container_issue
    entries = [e for e in inventory.hosts if e.ssh]
    if not entries:
        return None
    records, now = load_hosts_file(hosts_file), wall()
    hosts = containers = problems = reading_metrics_only = 0
    cells: list[str] = []
    for entry in entries:
        card = _safe_card(entry, records.get(entry.ssh), now)
        raw = (records.get(entry.ssh) or {}).get("containers")
        if card.liveness.state != "current":
            continue
        if not entry.docker:
            reading_metrics_only += 1
            continue
        if not isinstance(raw, list):
            continue
        hosts += 1
        rows = [c for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str)]
        containers += len(rows)
        for c in rows:
            issue, crash = container_issue(c, now)
            problems += 1 if issue else 0
            # The Overview's dot buckets, as for this host's containers: a finished one-shot (exited 0) is "paused".
            cells.append("down" if crash or (issue and c.get("state") != "running") else "degraded" if issue
                         else "paused" if c.get("state") == "exited" else "online")
    return {"hosts": hosts, "unknown": len(entries) - hosts - reading_metrics_only, "containers": containers, "problems": problems,
            "cells": cells}


MAX_OTHER_HOSTS_TEXT = 6000


def other_hosts_text(inventory=None, hosts_file=None, *, wall=time.time) -> str:
    """The other hosts for a Console turn's INVENTORY.md (release C): what Core last read, read-only. PE does not change other
    hosts, so the text says a plan cannot name them (and a plan's scope is checked against this host's stacks only)."""
    if inventory is None or hosts_file is None:
        import config
        inventory, hosts_file = config.MULTI_HOST, config.STATE_HOSTS
    entries = [e for e in inventory.hosts if e.ssh]
    if not entries:
        return ""
    records, now = load_hosts_file(hosts_file), wall()
    out = ["", "Other hosts (read over SSH by PE every minute; READ-ONLY: PE does not change them, and a plan must not name them):"]
    for entry in entries:
        card = _safe_card(entry, records.get(entry.ssh), now)
        if card.liveness.state == "unknown":
            out.append(f"- {entry.name}: not reading ({card.liveness.reason or 'no reading'})")
            continue
        if not entry.docker:
            m = card.metrics
            bits = [f"{label} {v:.0f}%" for label, v in (("CPU", m.cpu_pct), ("memory", m.mem_pct), ("disk", m.disk_pct))
                    if m is not None and v is not None]
            out.append(f"- {entry.name}: no Docker, metrics only ({', '.join(bits) or 'none reported'})")
            continue
        if card.containers is None:
            out.append(f"- {entry.name}: not reading (no container list)")
            continue
        age = f", read {int(card.liveness.age or 0)}s ago" if card.liveness.age is not None else ""
        out.append(f"- {entry.name} ({len(card.containers)} containers{age}):")
        for c in card.containers:
            bits = [c.status or "state not reported"]
            if c.health:
                bits.append(c.health)
            if c.restarts:
                bits.append(f"{c.restarts} restarts")
            if c.ports:
                bits.append(f"ports {c.ports}")
            out.append(f"  - {c.name}: {', '.join(bits)}")
    text = "\n".join(out) + "\n"
    return text if len(text) <= MAX_OTHER_HOSTS_TEXT else text[:MAX_OTHER_HOSTS_TEXT].rsplit("\n", 1)[0] + "\n  (cut short)\n"



# ── this host on the Hosts tab, and the other hosts' stacks on Overview (rc14.12, operator) ─────────────────────────────────────

LOCAL = "local"


def local_card(record: dict | None, now: float, *, name: str, containers: int | None) -> HostCard:
    """PE's own host as a card: the same metrics, read the same way (Core runs the metrics read locally, rc14.12), and the count of
    its containers from Leela's snapshot. Its containers and controls are on Overview; the card says so rather than repeating 87
    chips."""
    entry = type("Entry", (), {"ssh": LOCAL, "name": name, "link": None, "docker": True})()
    if not isinstance(record, dict):
        return HostCard(LOCAL, name, None, True, unknown(NOT_COLLECTED), source=LOCAL)
    card = _safe_card(entry, {**record, "ssh": LOCAL, "containers": []}, now)
    import dataclasses
    return dataclasses.replace(card, source=LOCAL, containers=None, ssh=None, local_containers=containers)


def remote_stacks(inventory: MultiHostConfig, hosts_file, *, wall=time.time) -> list[dict]:
    """The other hosts' containers grouped as Overview's stack cards: by Compose project, else one card per container. Only hosts
    reading now; worst first. Problems by the alerts' rule (remote_health.container_issue)."""
    from planet_express.application.remote_health import container_issue
    records, now, out = load_hosts_file(hosts_file), wall(), []
    for entry in inventory.hosts:
        if not (entry.ssh and entry.docker):
            continue
        record = records.get(entry.ssh)
        card = _safe_card(entry, record, now)
        if card.liveness.state != "current" or not isinstance((record or {}).get("containers"), list):
            continue
        groups: dict[str, list] = {}
        for c in record["containers"]:
            if not isinstance(c, dict) or not isinstance(c.get("name"), str):
                continue
            labels = c.get("labels") if isinstance(c.get("labels"), dict) else {}
            project = labels.get("com.docker.compose.project") or c["name"]
            issue, crash = container_issue(c, now)
            # The Overview's own words for a dot (crit / warn / idle / ok), so these cards read like this host's.
            level = ("crit" if crash or (issue and c.get("state") != "running") else "warn" if issue
                     else "idle" if c.get("state") == "exited" else "ok")
            groups.setdefault(str(project), []).append({"name": c["name"], "level": level, "issue": issue})
        for project, members in groups.items():
            members.sort(key=lambda x: ({"crit": 0, "warn": 1, "idle": 2, "ok": 3}[x["level"]], x["name"]))
            up = sum(1 for x in members if x["level"] in ("ok", "idle"))
            level = "crit" if any(x["level"] == "crit" for x in members) else "warn" if any(x["level"] == "warn" for x in members) else "ok"
            out.append({"name": project, "host": entry.name, "ssh": entry.ssh, "up": up, "total": len(members), "level": level,
                        "members": members})
    rank = {"crit": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda x: (rank[x["level"]], x["host"], x["name"]))
