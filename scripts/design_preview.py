#!/usr/bin/env python3
"""Serve the real dashboard on localhost against fixture state, for design work.

    venv/bin/python scripts/design_preview.py                # generated fixtures
    venv/bin/python scripts/design_preview.py --state ./snap  # a copied state directory

Why this exists: checking a layout change against the live host means either deploying it or
reading a screenshot someone else took. This runs the actual templates, the actual stylesheet
and the actual `dashboard_data` summarisers, with core's RPC replaced by a stub and the live
network pollers replaced by fixtures. What you see is what the release will render.

It is a development tool and it is built to be unusable as anything else:

  * binds 127.0.0.1 only;
  * mints a random passphrase and TOTP secret at startup and prints them, so there is no
    credential in this file and no shared secret between two runs;
  * never writes to the state directory it reads, and defaults to a temporary one;
  * serves static files with `no-store`, because a design loop that screenshots the previous
    stylesheet is worse than no design loop.

The fixtures are deliberately shaped like the awkward cases rather than the happy path: a
stack with a service down, a mount past its threshold, a merged LAN twin, a file-provider
router, a cert close to expiry. A layout that only holds together on good news is not done.
"""

import argparse
import base64
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))

import config
import web_auth

OPERATOR = "design"


def _iso(when):
    return when.astimezone(timezone.utc).isoformat()


def _systemd_local(when):
    return when.strftime("%a %Y-%m-%d %H:%M:%S %Z") or when.strftime("%a %Y-%m-%d %H:%M:%S WEST")


# Three different vocabularies describe the same container, and a fixture that mixes them up
# produces a dashboard that disagrees with itself rather than one that looks broken. All three
# come straight from casa_leela.py:
#
#   containers[].status        the raw `docker ps` string. _container_state() buckets on
#                              .startswith("Up"), so "running (healthy)" reads as DOWN.
#   stack_completeness[]
#     .services[].state        "running(healthy)" / "running(unhealthy)" / "exited(1)" /
#                              "absent" -- no space, and _service_component_level() buckets on
#                              .startswith("running"), so "Up 2 days" reads as PAUSED.
#     .services[].status       "healthy" | "unknown" | "failing". Not the docker string.
#   stack_completeness[].status  "complete" | "incomplete" | "unknown".
CONTAINER_STATUS = {
    "ok": "Up 2 days (healthy)",
    "degraded": "Up 12 minutes (unhealthy)",
    "down": "Exited (1) 4 minutes ago",
}
SERVICE_STATE = {
    "ok": {"status": "healthy", "state": "running(healthy)"},
    "degraded": {"status": "failing", "state": "running(unhealthy)"},
    "down": {"status": "failing", "state": "exited(1)"},
}


# Images a shipped widget names, so the stack drawer shows its ◉ hint on real rows.
FIXTURE_IMAGES = {"sonarr": "lscr.io/linuxserver/sonarr:latest",
                  "adguard": "adguard/adguardhome:latest"}
FIXTURE_STACKS = {
    "media": ["plex", "sonarr", "radarr", "bazarr", "prowlarr", "tautulli"],
    "services": ["web", "db", "cache", "worker"],
    "network": ["traefik", "adguard", "gluetun"],
    "subwave": ["api", "stream", "web"],
    "immich": ["server", "machine-learning", "postgres"],
    "vikunja": ["api", "frontend"],
    "linktool": ["app"],
}


def write_fixtures(state: Path) -> None:
    """A snapshot with something wrong in every panel that can be wrong."""
    now = datetime.now(timezone.utc)
    stacks = FIXTURE_STACKS
    containers, completeness = [], []
    for stack, services in stacks.items():
        members = {}
        for index, service in enumerate(services):
            # One real fault and one degraded service, so no panel renders all-green.
            if stack == "media" and service == "bazarr":
                kind = "down"
            elif stack == "services" and service == "worker":
                kind = "degraded"
            else:
                kind = "ok"
            containers.append({
                "name": f"CASA_{stack.upper()}_{service.upper()}",
                "stack": stack, "service": service,
                "image": FIXTURE_IMAGES.get(service, f"example/{service}:latest"),
                "status": CONTAINER_STATUS[kind],
                "issue": kind != "ok",
                "crash_looping": kind == "down",
            })
            members[service] = {**SERVICE_STATE[kind],
                                "container": f"CASA_{stack.upper()}_{service.upper()}"}
        stack_status = "incomplete" if any(
            m["status"] == "failing" for m in members.values()) else "complete"
        completeness.append({"stack": stack, "status": stack_status, "services": members})

    (state / "latest_monitor.json").write_text(json.dumps({
        "timestamp": _iso(now - timedelta(minutes=4)),
        "mode": "full",
        "containers": containers,
        "stack_completeness": completeness,
        "disk": [
            {"mount": "/", "source": "/dev/nvme0n1p2", "used_pct": 74},
            {"mount": "/immichPhotos", "source": "nfs:/immich", "used_pct": 91},
            {"mount": "/casamedia", "source": "/dev/sda1", "used_pct": 56},
            {"mount": "/erugo", "source": "/dev/sdb1", "used_pct": 47},
            {"mount": "/casa_scratch", "source": "tmpfs", "used_pct": 1},
            {"mount": "/unreadable", "source": "nfs:/gone", "used_pct": None},
        ],
        "backups": {
            "weekly": {
                "result": "success", "exit_code": "0",
                "last_run": _systemd_local(now - timedelta(days=5, hours=8)),
                "next_run": _systemd_local(now + timedelta(days=1, hours=15)),
                "cadence_hours": 168,
            },
        },
        "certs": [
            {"domain": "*.casalan.com", "resolver": "casalan-internal",
             "issuer": "casalan Internal CA", "sans": ["*.casalan.com", "casalan.com"],
             "expires": "Mar 21 12:00:00 2028 GMT", "days_left": 542,
             "tier": "valid", "life_pct": 100},
            {"domain": "*.casaalmida.com", "resolver": "cloudflare-origin",
             "issuer": "Cloudflare, Inc.", "sans": ["*.casaalmida.com", "casaalmida.com"],
             "expires": "Jan 8 12:00:00 2040 GMT", "days_left": 4849,
             "tier": "valid", "life_pct": 100},
            {"domain": "vpn.casalan.com", "resolver": "vpn-leaf",
             "issuer": "casalan Internal CA", "sans": ["vpn.casalan.com"],
             "expires": "Oct 1 12:00:00 2026 GMT", "days_left": 5,
             "tier": "expiring", "life_pct": 1},
        ],
        "system": {
            "hostname": "casamediaserver",
            "uptime": "20:11:04 up 15 days, 14:58,  2 users,  load average: 4.50, 4.28, 4.17",
            "memory_summary": "Mem:           15Gi       9.4Gi       556Mi       662Mi       5.5Gi       5.1Gi",
            "recent_errors": [],
        },
    }, indent=2))

    (state / "latest_findings.json").write_text(json.dumps({
        "analyzed_at": _iso(now - timedelta(minutes=3)),
        "has_critical": False, "has_high": True,
        "findings": [
            {"id": "f1", "severity": "HIGH", "resource": "media/bazarr",
             "description": "bazarr has exited 4 times in the last hour and is not staying up."},
            {"id": "f2", "severity": "MEDIUM", "resource": "/immichPhotos",
             "description": "The immich photo mount is 91% full."},
            {"id": "f3", "severity": "LOW", "resource": "services/worker",
             "description": "worker is running but its healthcheck has not passed since restart."},
        ],
    }, indent=2))

    (state / "run_status.json").write_text(json.dumps({
        "state": "idle", "pending_plan_id": None, "pending_msg_id": None,
        "updated_at": _iso(now - timedelta(minutes=4)),
    }, indent=2))

    entries = []
    for day, (stack, services) in enumerate([
        ("services", ["postgres", "wiki-go", "bentopdf", "beszel-agent", "mealie2", "planka",
                      "web", "lubelogger"]),
        ("subwave", ["api", "stream", "web", "redis", "worker"]),
        ("vikunja", ["api", "frontend"]),
        ("tgbot", ["renderd", "superfeed"]),
        ("airwave", ["mcb"]),
        ("transmute", ["app"]),
    ]):
        start = now - timedelta(days=day + 2, hours=13)
        for index, service in enumerate(services):
            entries.append({
                "ts": _iso(start + timedelta(minutes=index * 2)),
                "stack": stack, "service": service,
                "old_id": "sha256:old", "new_id": "sha256:new",
                # One alert in the history, so a red bar has somewhere to appear.
                "status": "failed" if (stack == "subwave" and service == "worker") else "updated",
            })
    (state / "update_history.json").write_text(
        json.dumps({"schema_version": 1, "entries": entries}, indent=2))


ROUTER_ZONES = {
    "casalan.com": [
        ("actual", "docker"), ("adguard", "docker"), ("adguard-secondary", "file"),
        ("adventurelog", "docker"), ("airwave", "docker"), ("audiomuse", "docker"),
        ("bazarr", "docker"), ("beszel", "docker"), ("dockge", "docker"),
        ("dozzle", "docker"), ("homepage", "docker"), ("immich", "docker"),
        ("jellyfin", "file"), ("lidarr", "docker"), ("linktool", "docker"),
        ("mealie", "docker"), ("opnsense", "file"), ("planetexpress", "file"),
        ("plex", "file"), ("prowlarr", "docker"), ("qbit", "file"),
        ("radarr", "docker"), ("sonarr", "docker"), ("tautulli", "docker"),
        ("traefik", "docker"), ("transmute", "docker"), ("unraid", "file"),
        ("vikunja", "docker"), ("wiki", "docker"),
    ],
    # subwave-api and subwave-stream are deliberately absent: on the live host they exist
    # only as path routes (`/api`, `/stream.mp3`) and so have no launch link. They are added
    # below, so the UI is exercised against services that legitimately have no button.
    "casaalmida.com": [
        ("erugo", "docker"), ("navidrome", "docker"), ("overseerr", "docker"),
        ("subwave-web", "docker"),
    ],
    "chrisalmida.com": [("blog", "docker"), ("sovereign", "docker")],
}
LAN_TWINS = [("casalan.com", "navidrome"), ("casalan.com", "subwave-web")]
DOWN_ROUTERS = {"bazarr@docker"}


def fixture_routers():
    routers = []
    for zone, entries in ROUTER_ZONES.items():
        for name, provider in entries:
            full = f"{name}@{provider}"
            # Half the docker services are reachable on both domains from one router, which
            # is how the live host actually does LAN/public for most things -- 15 of its 70.
            rule = (f"Host(`{name}.{zone}`) || Host(`{name}.casaalmida.com`)"
                    if provider == "docker" and zone == "casalan.com" and name[0] < "m"
                    else f"Host(`{name}.{zone}`)")
            routers.append({
                "name": full, "service": name,
                "rule": rule,
                "status": "disabled" if full in DOWN_ROUTERS else "enabled",
                "entry_points": ["websecure"],
            })
    for zone, name in LAN_TWINS:
        routers.append({"name": f"{name}-lan@docker", "service": name,
                        "rule": f"Host(`{name}.{zone}`)", "status": "enabled",
                        "entry_points": ["websecure"]})
    for name, path in (("subwave-api", "/api"), ("subwave-stream", "/stream.mp3")):
        routers.append({"name": f"{name}@docker", "service": name,
                        "rule": f"Host(`radio.casaalmida.com`) && PathPrefix(`{path}`)",
                        "status": "enabled", "entry_points": ["websecure"]})
    for name in ("api", "dashboard", "web-to-mobsec"):
        routers.append({"name": f"{name}@internal", "service": name,
                        "rule": f"PathPrefix(`/{name}`)", "status": "enabled",
                        "entry_points": ["traefik"]})
    return {"available": True, "routers": routers}


FIXTURE_CONFIG = """# Migrated from hardcoded config.py values on this host, 2026-07-15.
stacks_root: /home/casaroot/stacks
forbidden_stacks:
  - clawbot
  - ai
  - pinepods
paused_containers:
  - CASA_ADGUARD
mounts:
  casamedia.mount: /casamedia
  casa_scratch.mount: /casa_scratch
  immichPhotos.automount: /immichPhotos
exclude_services:
  - stack: services
    service: backend
sudo_allowlist:
  units:
    - unit: casa-stacks.service
      actions: [start, stop, restart]
  globs:
    - glob: "*.mount"
      actions: [start, stop]

# Daily borg backup is disabled on purpose (2026-09-17); only weekly runs.
backup_jobs: [weekly]
"""


def fixture_owners() -> dict:
    """core's containers.routers, for the fixtures: a docker router that shares a name with a
    stack service belongs to that service's container. Enough for the drawer and pills to
    show real links; the rest of the fixture routers stay unjoined, as some do live."""
    containers = {service: f"CASA_{stack.upper()}_{service.upper()}"
                  for stack, services in FIXTURE_STACKS.items() for service in services}
    routers, services = {}, {}
    for router in fixture_routers()["routers"]:
        short, _, provider = router["name"].partition("@")
        base = short[:-4] if short.endswith("-lan") else short
        if provider == "docker" and base in containers:
            routers[short] = containers[base]
            services[router["service"]] = containers[base]
    return {"routers": routers, "services": services}


def _ago(seconds):
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat(timespec="seconds")


# One container per widget state, so each can be looked at: ok with rows, ok with a line,
# needs a key, and an error with stale values. Everything else has no widget. Ages are in
# seconds and become timestamps when asked, so a preview left open stays true to the design.
FIXTURE_WIDGETS = {
    ("media", "sonarr"): {
        "state": "ok", "widget": "sonarr", "via": "/api/v3/queue?pageSize=3", "fetched_at": 18,
        "stats": [{"k": "QUEUE", "v": 3}, {"k": "WANTED", "v": 12, "level": "warn"},
                  {"k": "HEALTH", "v": "ok", "level": "ok"}],
        "rows": [{"title": "Severance · S02E08", "pct": 84}, {"title": "The Bear · S04E02", "pct": 41},
                 {"title": "Andor · S02E11 <b>not markup</b>", "pct": 7}],
        "rows_label": "DOWNLOADING", "line": None},
    ("network", "adguard"): {
        "state": "ok", "widget": "adguard", "via": "/control/stats", "fetched_at": 4,
        "stats": [{"k": "QUERIES", "v": "35,591"}, {"k": "BLOCKED", "v": "22.4%"}, {"k": "AVG", "v": "41ms"}],
        "rows": [], "rows_label": "", "line": "most blocked: ads.example.com"},
    ("media", "radarr"): {"state": "needs_key", "widget": "radarr", "via": "/api/v3/queue",
                          "env": ["RADARR_API_KEY"]},
    ("media", "prowlarr"): {
        "state": "error", "widget": "prowlarr", "via": "/api/v1/indexer", "error": "answered 401",
        "status": 401, "stale_at": 240,
        "stale": {"stats": [{"k": "INDEXERS", "v": 14}, {"k": "FAILING", "v": 1, "level": "warn"},
                            {"k": "GRABS 24H", "v": 37}], "rows": [], "rows_label": "", "line": None}},
}


class FixtureWidgets:
    """Stands in for the WidgetFetcher: answers from FIXTURE_WIDGETS, never the network."""

    def __init__(self, **_kwargs):
        pass

    def now(self):
        return time.monotonic()

    def cached(self, key):
        answer = dict(FIXTURE_WIDGETS.get(tuple(key), {"state": "none"}))
        for field in ("fetched_at", "stale_at"):
            if field in answer:
                answer[field] = _ago(answer[field])
        return answer

    def fetch(self, target, key, asked=None):
        return self.cached(key)


def fixture_container(params):
    """core's query.container, enough for the detail view to render its verdict and vitals."""
    service = params.get("service", "?")
    down = service == "bazarr"
    return {
        "target": {"stack": params.get("stack"), "service": service,
                   "container": f"CASA_{str(params.get('stack', '')).upper()}_{service.upper()}"},
        "paused": False,
        "vitals": {"ok": not down, "stats": {"cpu_percent": 1.8, "memory_percent": 3.4,
                                              "memory_used_bytes": 286 * 1048576}},
        "facts": {"ok": True, "facts": {
            "state": "restarting" if down else "running", "started_at": _ago(6 * 86400),
            "health": "unhealthy" if down else "healthy", "failing_streak": 0,
            "restart_count": 7 if down else 0,
            "restart_policy": {"name": "unless-stopped", "max_retries": 0},
            "ports": [], "image_id": FIXTURE_IMAGES.get(service, f"example/{service}:latest")}},
        "actions": {"docker.restart_service": {"abortable": False, "rollbackable": False,
                                                "resumable": False}},
    }


class OfflineRpc:
    """Core is not running. Every method the dashboard calls answers empty but well-formed,
    so an unavailable panel is the panel's own empty state and never a traceback."""

    EMPTY: ClassVar[dict] = {
        "canary.candidates": [],
        "containers.routers": {"routers": {}, "services": {}},
        "config.enforced": {"paused_containers": ["CASA_ADGUARD"], "backup_jobs": ["weekly"], "links": []},
        "proposal.list_pending": [],
        "incident.list": [],
        "chat.quota": {"used": 3, "limit": 100, "resets_at": time.time() + 3600},
        "auth.status": {"locked": False, "remaining_attempts": 3, "locked_until": None,
                        "accepted": True, "epoch": 0, "sent": True},
    }

    def __call__(self, method, params):
        if method == "config.get":
            return {"ok": True, "result": {
                "text": FIXTURE_CONFIG,
                "path": "/home/casaroot/apps/planetexpress/config.yaml",
                "sha256": "b88ab75c84a3", "loaded_sha256": "b88ab75c84a3",
                "editable_fields": ["backup_jobs", "exclude_services", "paused_containers"],
                "sensitive_fields": ["forbidden_stacks", "sudo_allowlist", "autostop"],
                "sensitive_edits_enabled": False,
            }}
        if method.startswith("auth."):
            return {"ok": True, "result": self.EMPTY["auth.status"]}
        if method == "containers.routers":
            return {"ok": True, "result": fixture_owners()}
        if method == "query.container":
            return {"ok": True, "result": fixture_container(params)}
        if method == "logs.tail":
            # Three lines on the first poll, then nothing new: the well does not fill with copies.
            first = not params.get("cursor")
            return {"ok": True, "result": {
                "ok": True, "cursor": "2026-09-27T12:00:02Z", "cursor_hashes": [], "skipped": False,
                "started_at": None,
                "lines": [{"ts": "2026-09-27T12:00:0%dZ" % i, "stream": "stdout",
                           "text": f"fixture log line {i}"} for i in range(3 if first else 0)]}}
        return {"ok": True, "result": self.EMPTY.get(method, {})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path,
                        help="a state directory to read (copied, never written); "
                             "omit to generate fixtures")
    parser.add_argument("--port", type=int, default=8773)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pe-design-preview-"))
    state = workdir / "state"
    state.mkdir()
    if args.state:
        # Copied, so a preview can never scribble on a real snapshot.
        for name in ("latest_monitor.json", "latest_findings.json", "run_status.json",
                     "update_history.json"):
            source = args.state / name
            if source.exists():
                shutil.copy2(source, state / name)
        print(f"state: copied from {args.state}")
    else:
        write_fixtures(state)
        print("state: generated fixtures (a fault in every panel that can have one)")

    config.STATE_MONITOR = state / "latest_monitor.json"
    config.STATE_FINDINGS = state / "latest_findings.json"
    config.STATE_STATUS = state / "run_status.json"
    config.UPDATE_HISTORY_FILE = state / "update_history.json"

    import casa_scruffy
    import casa_scruffy_net

    casa_scruffy_net.fetch_traefik_routers = fixture_routers
    # Widgets from fixtures: the real fetcher would try the fixture containers' addresses.
    casa_scruffy.WidgetFetcher = FixtureWidgets
    casa_scruffy_net.fetch_adguard_stats = lambda: {
        "available": True, "configured": True, "num_dns_queries": 35591,
        "num_blocked_filtering": 7982, "avg_processing_time": 0.041,
    }

    # Minted per run: nothing here is a credential anyone could reuse, and two runs never
    # share one.
    passphrase = f"design-{secrets.token_urlsafe(9)}"
    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    env = {
        "PE_DASHBOARD_SECRET_KEY": secrets.token_urlsafe(48),
        "PE_OPERATORS": OPERATOR,
        f"PE_OPERATOR_{OPERATOR.upper()}_PASSPHRASE_HASH": web_auth.hash_passphrase(passphrase),
        f"PE_OPERATOR_{OPERATOR.upper()}_TOTP_SECRET": totp_secret,
    }

    app = casa_scruffy.create_app(env, rpc_call=OfflineRpc())
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def _no_store(response):
        # Flask serves static files with a long max-age. In a design loop that means editing
        # cockpit.css and then screenshotting the previous version, which looks exactly like a
        # rule that did not work.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    code = web_auth.totp_at(totp_secret, int(time.time() // 30))
    print(f"\n  http://127.0.0.1:{args.port}")
    print(f"  operator:   {OPERATOR}")
    print(f"  passphrase: {passphrase}")
    print(f"  auth code:  {code}  (30s; next with: "
          f"venv/bin/python -c \"import time, web_auth; "
          f"print(web_auth.totp_at('{totp_secret}', int(time.time()//30)))\")\n")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
