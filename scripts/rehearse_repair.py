#!/usr/bin/env python3
"""Repair-mission rehearsal (v4 release proof item 7): drives the REAL checkpoint service, tripwire monitor, verifier,
approval flow and supervisor against REAL Docker, on a disposable host, with a synthetic compose project. Prints one JSON record
(steps, timings, check results) and exits non-zero on any failure.

    PE_REHEARSAL_OK=1 python3 scripts/rehearse_repair.py --i-know-this-is-disposable [--scenario all|namespace|canary]
        [--workdir DIR] [--image busybox:1.36] [--timeout-s 300] [--keep] [--allow-foreign-containers] [--json-out FILE]

It is meant to be run as root ON A DISPOSABLE HOST (a throwaway VM, never a machine whose containers matter): see
`scripts/run_privileged_tests.sh --vm HOST` and docs/handoff/v4/rehearsals.md, which run it over ssh. It refuses unless
PE_REHEARSAL_OK=1 is set AND --i-know-this-is-disposable is given AND it runs as root AND docker answers AND no container
outside its own compose project exists (override with --allow-foreign-containers only on a host you built for this).

Scenario "namespace" (release proof 7a): a VPN-gateway-style `compose up --force-recreate --no-deps gateway` orphans the two
containers that join the gateway's network namespace (a plain restart does not rebind it). The scripted agent finds the
orphans and recreates them in dependency order. Core then verifies, with its own pre-agreed checks, an in-namespace
application probe and a compose-project completeness check.

Scenario "canary" (release proof 7b): a failed canary (a bad config change recreating `web`) is rolled back by the operator
(checkpoint restore), then a second mission, approved with a bounded scope, corrects the config and recreates ONLY `web`.

The agent is a deterministic command (this same file, subcommand `agent`): no LLM, no network. It reaches Docker ONLY through
a real per-mission `DockerProxyService` (real Upstream to the host socket, real Policy/Scope from the mission's grant, real
recording of creates into the store, which is what the tripwire reads); its DOCKER_HOST is that proxy socket, and the agent
refuses to run against anything else. What it exercises: the checkpoint service (real compose provider), the tripwire monitor
(real Docker/ss/file scans), the verifier (real checks), the approval flow, the supervisor and the proxy. Every call the proxy
refuses is listed in the record under "proxy" (a finding, never something to work around). What it does NOT exercise, and says
so in the record under "not_exercised": the cgroup launcher and session uids (scripts/mission_containment_rehearsal.py); the
agent here runs as root in a process group, and the proxy's gate recognises it by that group.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
PROJECT = "pe-rehearsal"
DEFAULT_IMAGE = "busybox:1.36"
SCENARIOS = ("namespace", "canary")
OPERATOR = "operator:rehearsal"
MARKER = ".pe-rehearsal-stack"
NOT_EXERCISED = [
    ("cgroup launcher and session uids (scripts/mission_containment_rehearsal.py): the agent runs as root in a process "
     "group, and the proxy recognises it by that group rather than by cgroup and a unique uid"),
    "socket ownership handed to a session uid by the launcher helper (tests/test_dockerproxy_privileged.py)",
]
AGENT_ENV_ALLOW = ["PATH", "HOME"]           # DOCKER_HOST is set by the session's proxy socket and nowhere else
SOCKET_PATH_MAX = 100                        # sun_path is 108 bytes; leave room
LEFTOVER = re.compile(r"^[0-9a-f]{12}_" + re.escape(PROJECT) + r"-")

COMPOSE = """\
name: {project}
services:
  gateway:
    image: {image}
    command: ["sh", "-c", "mkdir -p /www && echo gateway-ok > /www/index.html && exec httpd -f -p 8080 -h /www"]
    restart: unless-stopped
  app1:
    image: {image}
    network_mode: "service:gateway"
    depends_on: [gateway]
    command: ["sh", "-c", "exec sleep 86400"]
    restart: unless-stopped
  app2:
    image: {image}
    network_mode: "service:gateway"
    depends_on: [app1]
    command: ["sh", "-c", "exec sleep 86400"]
    restart: unless-stopped
  web:
    image: {image}
    environment:
      GREETING: web-ok
    command: ["sh", "-c", "mkdir -p /www && echo \\"$$GREETING\\" > /www/index.html && exec httpd -f -p 8081 -h /www"]
    restart: unless-stopped
"""


# ── pure helpers (unit-tested) ───────────────────────────────────────────────────────────────────────────

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rehearse_repair.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--workdir", default=None, help="state, stacks and workspaces (default: a new temp directory)")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--core-user", default="planetexpress",
                        help="the account Core runs as on this host: the side-door gate checks sudoers rules that reach it")
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--keep", action="store_true", help="leave the compose project and the workdir in place")
    parser.add_argument("--allow-foreign-containers", action="store_true")
    parser.add_argument("--i-know-this-is-disposable", action="store_true", dest="disposable")
    parser.add_argument("--json-out", default=None)
    sub = parser.add_subparsers(dest="command")
    agent = sub.add_parser("agent", help="internal: the scripted agent a mission launches")
    agent.add_argument("--agent-scenario", required=True, choices=("namespace", "canary-bad", "canary-fix"))
    agent.add_argument("--stack-dir", required=True)
    agent.add_argument("--project", default=PROJECT)
    args = parser.parse_args(list(argv))
    args.command = args.command or "run"
    if args.command == "run" and not 10 <= args.timeout_s <= 3600:
        parser.error("--timeout-s must be between 10 and 3600")
    return args


def refusals(args: argparse.Namespace, env: Mapping[str, str], euid: int, run: Callable[[Sequence[str]], tuple[int, str]]) -> list[str]:
    """Every reason this host is not acceptable. Empty means go. `run(argv)` returns (rc, stdout) and must not raise for a
    missing binary (return rc 127)."""
    why: list[str] = []
    if env.get("PE_REHEARSAL_OK") != "1":
        why.append("PE_REHEARSAL_OK=1 is not set (set it only inside a disposable environment)")
    if not args.disposable:
        why.append("--i-know-this-is-disposable was not given")
    if euid != 0:
        why.append("must run as root")
    rc, out = run(["docker", "ps", "-a", "--format", '{{.ID}} {{.Label "com.docker.compose.project"}}'])
    if rc != 0:
        why.append("docker does not answer (is the daemon running and the CLI installed?)")
    elif not args.allow_foreign_containers:
        foreign = [line for line in out.splitlines() if line.strip() and line.split(" ", 1)[-1].strip() != PROJECT]
        if foreign:
            why.append(f"{len(foreign)} container(s) outside the {PROJECT} project exist: this is not a disposable host "
                       "(--allow-foreign-containers overrides, on a host built only for this)")
    rc, _ = run(["docker", "compose", "version"])
    if rc != 0:
        why.append("the docker compose plugin is not available")
    return why


def compose_text(image: str = DEFAULT_IMAGE, project: str = PROJECT) -> str:
    return COMPOSE.format(image=image, project=project)


def dependency_order(depends: Mapping[str, Sequence[str]], members: Sequence[str]) -> list[str]:
    """`members` ordered so each comes after whatever it depends on (within the members). Raises ValueError on a cycle."""
    wanted, order, seen, active = set(members), [], set(), set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in active:
            raise ValueError("dependency cycle")
        active.add(name)
        for dep in sorted(depends.get(name, ())):
            visit(dep)
        active.discard(name)
        seen.add(name)
        if name in wanted:
            order.append(name)
    for name in sorted(wanted, key=list(members).index):
        visit(name)
    return order


def agent_argv(scenario: str, stack_dir: str, project: str = PROJECT) -> list[str]:
    """The command a mission launches: this file's `agent` subcommand, isolated mode, absolute paths."""
    return [sys.executable, "-I", str(SELF), "agent", "--agent-scenario", scenario, "--stack-dir", stack_dir, "--project", project]


def verification_checks(scenario: str, project: str = PROJECT) -> list[dict]:
    """The pre-agreed checks Core runs after the agent (never the agent's own word)."""
    if scenario == "namespace":
        return [
            {"id": "in-namespace", "check": "exec_probe",
             "params": {"container": f"{project}-app1-1", "argv": ["wget", "-qO-", "http://127.0.0.1:8080/"],
                        "expect_contains": "gateway-ok", "retries": 2}},
            {"id": "in-namespace-2", "check": "exec_probe",
             "params": {"container": f"{project}-app2-1", "argv": ["wget", "-qO-", "http://127.0.0.1:8080/"],
                        "expect_contains": "gateway-ok", "retries": 2}},
            {"id": "complete", "check": "project_complete", "params": {"stack": project, "retries": 2}},
        ]
    expect = {"canary-bad": "web-ok", "canary-fix": "web-ok-v2"}[scenario]
    return [
        {"id": "web-serves", "check": "exec_probe",
         "params": {"container": f"{project}-web-1", "argv": ["wget", "-qO-", "http://127.0.0.1:8081/"],
                    "expect_contains": expect, "retries": 2}},
        {"id": "complete", "check": "project_complete", "params": {"stack": project, "retries": 2}},
    ]


def proxy_docker_host(env: Mapping[str, str]) -> str | None:
    """DOCKER_HOST if (and only if) it names a session proxy socket: an absolute unix path ending in /proxy.sock."""
    value = env.get("DOCKER_HOST", "")
    if not value.startswith("unix:///"):
        return None
    path = value[len("unix://"):]
    if ".." in path.split("/") or os.path.basename(path) != "proxy.sock":
        return None
    return value


def leftover_ids(rows: Sequence[str]) -> list[str]:
    """Container ids from `<id> <name>` rows whose name is a compose-renamed leftover (`<12 hex>_pe-rehearsal-...`)."""
    out = []
    for row in rows:
        parts = row.split(" ", 1)
        if len(parts) == 2 and LEFTOVER.match(parts[1].strip()):
            out.append(parts[0])
    return out


def remove_leftovers(run: Callable[..., tuple[int, str]] | None = None) -> list[str]:
    """Remove the rehearsal project's renamed half-recreated containers (what a failed recreate leaves behind), by id."""
    run = run or real_run
    _, out = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format",
                  "{{.ID}} {{.Names}}"])
    ids = leftover_ids(out.splitlines())
    for ident in ids:
        run(["docker", "rm", "-f", ident])
    return ids


def networks_left(run: Callable[..., tuple[int, str]] | None = None) -> list[str]:
    run = run or real_run
    _, out = run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"])
    return sorted(out.split())


def proxy_summary(events: Sequence) -> dict:
    """What the Docker proxy recorded for a mission: write count, creates it vouched for, and every refused call."""
    writes = created = 0
    refused: list[dict] = []
    more = False
    for e in events:
        try:
            body = json.loads(e.body_json)
        except (TypeError, ValueError):
            continue
        kind = body.get("event") if isinstance(body, dict) else None
        if kind == "docker.write":
            writes += 1
        elif kind == "docker.created":
            created += 1
        elif kind == "docker.refused":
            if body.get("further_refusals_not_recorded"):
                more = True
            else:
                refused.append({k: body.get(k) for k in ("reason", "route", "method", "path")})
    return {"writes": writes, "created": created, "refused": refused, "further_refusals_not_recorded": more}


def went_through_proxy(summary: Mapping) -> bool:
    """A scenario that recreates containers must have been seen doing so by the proxy, or the agent bypassed it."""
    return bool(summary.get("writes")) and bool(summary.get("created"))


class Recorder:
    """The JSON record: ordered steps with timings, check results, and the overall verdict."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock, self.started = clock, clock()
        self.steps: list[dict] = []
        self.failures: list[str] = []
        self.extra: dict = {}

    def step(self, name: str, ok: bool, detail: str = "", **data) -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "at_s": round(self.clock() - self.started, 3),
                           **({"detail": detail} if detail else {}), **data})
        if not ok:
            self.failures.append(f"{name}: {detail}".strip(": "))
        return bool(ok)

    def document(self, *, scenario: str, refused: list[str] | None = None) -> dict:
        return {"rehearsal": "repair", "scenario": scenario, "ok": not self.failures and not refused,
                "refused": refused or [], "failures": self.failures, "steps": self.steps,
                "elapsed_s": round(self.clock() - self.started, 3), "not_exercised": NOT_EXERCISED, **self.extra}


def real_run(argv: Sequence[str], timeout: int = 120) -> tuple[int, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, euid: int | None = None,
         run: Callable[[Sequence[str]], tuple[int, str]] = real_run, rehearse: Callable | None = None,
         out=None) -> int:
    """Exit 0: every step passed. 1: a step failed. 2: refused to run (nothing was touched) or bad usage."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "agent":
        return agent_main(args)
    env = os.environ if env is None else env
    euid = os.geteuid() if euid is None else euid
    out = sys.stdout if out is None else out
    why = refusals(args, env, euid, run)
    if why:
        print(json.dumps(Recorder().document(scenario=args.scenario, refused=why), indent=2), file=out)
        return 2
    rec = Recorder()
    try:
        (rehearse or rehearse_real)(args, rec)
    except Exception as exc:                                       # noqa: BLE001 - the record says what broke
        rec.step("rehearsal-crashed", False, f"{type(exc).__name__}: {exc}"[:300])
    doc = rec.document(scenario=args.scenario)
    text = json.dumps(doc, indent=2)
    print(text, file=out)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n")
    return 0 if doc["ok"] else 1


# ── the scripted agent (no PE imports: it runs under `python -I`) ───────────────────────────────────────────

def agent_main(ns: argparse.Namespace) -> int:
    stack = Path(ns.stack_dir)
    compose_file = stack / "docker-compose.yml"
    if ns.project != PROJECT or not (stack / MARKER).is_file():
        print("refusing: not a rehearsal stack directory", file=sys.stderr)
        return 2
    if proxy_docker_host(os.environ) is None:
        print("refusing: DOCKER_HOST is not a session proxy socket (the agent never talks to the host Docker socket)",
              file=sys.stderr)
        return 2
    steps: list[str] = []

    def dc(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "compose", "-f", str(compose_file), "-p", ns.project, *a],
                              capture_output=True, text=True, timeout=240, check=False)

    def cid(service: str) -> str:
        done = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={ns.project}",
                               "--filter", f"label=com.docker.compose.service={service}"],
                              capture_output=True, text=True, timeout=30, check=False)
        return done.stdout.split()[0] if done.stdout.split() else ""

    def netmode(service: str) -> str:
        done = subprocess.run(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", cid(service)],
                              capture_output=True, text=True, timeout=30, check=False)
        return done.stdout.strip()

    def recreate(service: str) -> None:
        steps.append(f"recreate {service}")
        done = dc("up", "-d", "--force-recreate", "--no-deps", service)
        if done.returncode != 0:
            raise SystemExit(f"recreate {service} failed")

    def set_greeting(value: str) -> None:
        text = compose_file.read_text()
        lines = [f"      GREETING: {value}" if line.strip().startswith("GREETING:") else line
                 for line in text.splitlines()]
        compose_file.write_text("\n".join(lines) + "\n")
        steps.append(f"set GREETING={value}")

    if ns.agent_scenario == "namespace":
        recreate("gateway")                                           # the change that orphans its dependents
        new_gateway = cid("gateway")
        config = json.loads(dc("config", "--format", "json").stdout or "{}")
        depends = {name: list(svc.get("depends_on") or {}) for name, svc in (config.get("services") or {}).items()}
        orphans = [s for s in dependency_order(depends, ["app1", "app2"]) if netmode(s) != f"container:{new_gateway}"]
        steps.append(f"orphans {','.join(orphans) or 'none'}")
        for service in orphans:
            recreate(service)
        diagnosis = "The gateway was recreated; its dependents were rebound to the new network namespace in dependency order."
    elif ns.agent_scenario == "canary-bad":
        set_greeting("broken")
        recreate("web")
        diagnosis = "Applied the canary configuration to web."
    else:
        set_greeting("web-ok-v2")
        recreate("web")
        diagnosis = "Corrected the web configuration and recreated only web."
    result = {"diagnosis": diagnosis, "evidence": steps, "suggested_actions": [], "unknowns": [],
              "memory_candidates": [], "confidence": "medium"}
    Path("result.json").write_text(json.dumps(result))
    print("agent done:", "; ".join(steps))
    return 0


# ── the real rehearsal (imports the PE stack lazily) ─────────────────────────────────────────────────────────

def rehearse_real(args: argparse.Namespace, rec: Recorder) -> None:
    import tempfile
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))
    work = Path(args.workdir or tempfile.mkdtemp(prefix="pe-rehearsal-"))
    work.mkdir(parents=True, exist_ok=True)
    stacks = work / "stacks"
    stack_dir = stacks / PROJECT
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(compose_text(args.image))
    (stack_dir / MARKER).write_text("created by scripts/rehearse_repair.py\n")
    rec.extra["workdir"] = str(work)
    rig = None
    try:
        up = real_run(["docker", "compose", "-f", str(stack_dir / "docker-compose.yml"), "-p", PROJECT, "up", "-d"], 300)
        if not rec.step("project-up", up[0] == 0, "docker compose up failed" if up[0] else ""):
            return
        rig = Rig(work, stacks, args)
        rec.step("controls-built", True, repair_gate="skipped" if rig.gate_skipped else "real")
        for name, run_scenario in (("namespace", scenario_namespace), ("canary", scenario_canary)):
            if args.scenario not in ("all", name):
                continue
            if not prepare_scenario(rec, name, stack_dir, args.image):
                continue
            try:
                run_scenario(rig, rec, stack_dir)
            except Exception as exc:                                # noqa: BLE001 - still release below, then the record says so
                rec.step(f"{name}:crashed", False, f"{type(exc).__name__}: {str(exc)[:200]}")
            finally:
                rig.release(rec, name)                              # BEFORE the next scenario's prepare
    finally:
        if rig is not None:
            rig.close()
        if not args.keep:
            removed = remove_leftovers()
            down = real_run(["docker", "compose", "-f", str(stack_dir / "docker-compose.yml"), "-p", PROJECT, "down", "-v",
                             "--remove-orphans", "-t", "2"], 120)
            left, nets = container_ids(), networks_left()
            clean = down[0] == 0 and not left and not nets
            rec.step("teardown", clean, "" if clean else "the rehearsal project was not fully removed",
                     remaining=sorted(left), networks=nets, leftovers_removed=removed)


def prepare_scenario(rec: Recorder, scenario: str, stack_dir: Path, image: str,
                     run: Callable[..., tuple[int, str]] | None = None) -> bool:
    """Start every scenario from the same place: renamed leftovers of an earlier (failed) scenario removed, the compose file
    as shipped, and the whole project up. Runs as the harness on the host socket, never as the agent. A baseline that is not
    reached is a failed step that says what is missing, and the scenario does not run."""
    run = run or real_run
    removed = remove_leftovers(run)
    (stack_dir / "docker-compose.yml").write_text(compose_text(image))
    up = run(["docker", "compose", "-f", str(stack_dir / "docker-compose.yml"), "-p", PROJECT, "up", "-d"], 300)
    missing = sorted({"gateway", "app1", "app2", "web"} - set(container_ids(run=run)))
    why = ("docker compose up failed" if up[0] != 0
           else f"baseline not reached: missing {', '.join(missing)}" if missing else "")
    return rec.step(f"{scenario}:prepared", not why, why, leftovers_removed=removed)


def release_missions(rec: Recorder, label: str, mids: Sequence[str], *, status_of: Callable[[str], str],
                     cancel: Callable[[str, str], None], tick: Callable[[], None], sleep: Callable[[float], None] = time.sleep,
                     terminal: Sequence[str] = ("completed", "failed", "rolled_back", "cancelled"), timeout_s: float = 30) -> bool:
    """End a scenario cleanly: any of its missions that is not terminal is cancelled through the supervisor's real cancel path
    (session killed, leases and proxy socket released, tripwire stopped). Recorded as `<label>:released` with every final
    status. Never raises: a mission that cannot be ended is a failed step, and the next scenario must not start on top of it."""
    errors: list[str] = []
    for mid in mids:
        try:
            if status_of(mid) not in terminal:
                cancel(mid, OPERATOR)
        except Exception as exc:                                    # noqa: BLE001 - the record says what broke
            errors.append(f"{mid}: cancel failed: {type(exc).__name__}: {str(exc)[:120]}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and any(status_of(m) not in terminal for m in mids):
        try:
            tick()
        except Exception as exc:                                    # noqa: BLE001
            errors.append(f"tick failed: {type(exc).__name__}: {str(exc)[:120]}")
            break
        sleep(0.5)
    final = {m: status_of(m) for m in mids}
    stuck = {m: s for m, s in final.items() if s not in terminal}
    if stuck:
        errors.append("still holding its session and leases: " + ", ".join(f"{m} is {s}" for m, s in stuck.items()))
    return rec.step(f"{label}:released", not errors, "; ".join(errors), final=final)


def through_proxy_detail(summary: Mapping) -> str:
    """The one-line cause when the proxy saw no container create: the first refused calls, if there were any."""
    base = "the proxy recorded no container create: the agent did not reach Docker through it"
    refused = list(summary.get("refused") or [])[:3]
    if not refused:
        return f"{base} (no refused calls recorded)"
    calls = "; ".join(f"{r.get('method')} {r.get('path')}: {r.get('reason')}" for r in refused)
    return f"{base}; first refused: {calls}"


def container_ids(project: str = PROJECT, run: Callable[..., tuple[int, str]] | None = None) -> dict[str, str]:
    _, out = (run or real_run)(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format",
                       '{{.ID}} {{.Label "com.docker.compose.service"}}'])
    return {svc: cid for cid, svc in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


def created_at(cid: str) -> str:
    return real_run(["docker", "inspect", "--format", "{{.Created}}", cid])[1].strip()


class Rig:
    """The real stack, assembled from the same public builders the daemon uses, with a process-group containment."""

    def __init__(self, work: Path, stacks: Path, args: argparse.Namespace):
        import threading
        from types import SimpleNamespace

        from config_schema import MissionsConfig
        from planet_express.core.store import Store, write_snapshot_marker
        from planet_express.missions.adapters.command import CommandRuntime
        from planet_express.missions.approval_flow import ApprovalFlow
        from planet_express.missions.assembly import RepairWiring, build_repair_controls
        from planet_express.missions.checkpoint import SystemdCheckpointProvider
        from planet_express.missions.dockerproxy.service import (
            DEFAULT_DOCKER_SOCKET,
            DockerProxyService,
        )
        from planet_express.missions.launcher.preflight import Check, PreflightReport
        from planet_express.missions.store import MissionStore
        from planet_express.missions.supervisor import Supervisor
        from planet_express.missions.tripwire.build import build_protected_set

        self.args, self.work, self.stacks = args, work, str(stacks)
        db = work / "state" / "pe.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        write_snapshot_marker(db, now=time.time())
        self.store = Store(db)
        self.store.init()
        self.ms = MissionStore(self.store)
        self.cfg = MissionsConfig(enabled=True, tripwire_interval_s=5, verify={"exec_allowlist": ["wget"]},
                                  checkpoint={"root": str(work / "checkpoints")}, workspace_root=str(work / "ws"),
                                  runtime={"argv": [sys.executable, "-I", str(SELF)]})
        self.containment = ProcessContainment()
        provider = SystemdCheckpointProvider(SimpleNamespace(stacks_root=stacks))
        wiring = RepairWiring(provider=provider, scan=lambda: project_scan(PROJECT), core_user=args.core_user)
        protected = build_protected_set(self.cfg, stacks_root=self.stacks)
        # The agent's only road to Docker: the daemon's own per-session proxy (real Upstream to the host socket, real Policy and
        # Scope from the mission's grant, real recording of creates). Built BEFORE the controls so the tripwire's pause reaches it.
        sock_root = work / "sock"
        sock_root.mkdir(parents=True, exist_ok=True)
        self.proxy = DockerProxyService(self.store, authenticator=RehearsalAuthenticator(self.store, self.containment),
                                        docker_socket=DEFAULT_DOCKER_SOCKET, sock_root=str(sock_root), stacks_root=self.stacks,
                                        protected=lambda: protected.resources, protected_gids=(),
                                        label_allow=self.cfg.docker_label_allow)
        self.controls = build_repair_controls(self.store, self.cfg, wiring, containment=self.containment, broker=None,
                                              protected=protected, stacks_root=self.stacks, enabled=lambda: True,
                                              proxy=self.proxy)
        real_gate = self.controls.repair_gate
        # The side-door gate is a property of the HOST (sudoers, docker group, TCP socket). On a throwaway VM that has not been
        # through the launcher setup it fails for reasons unrelated to this rehearsal; the operator may skip it knowingly.
        self.gate_skipped = os.environ.get("PE_REHEARSAL_SKIP_GATE") == "1"
        if self.gate_skipped:
            skipped = PreflightReport("skipped")
            skipped.checks.append(Check("side-doors", True, "skipped by PE_REHEARSAL_SKIP_GATE=1"))
            gate = lambda: skipped
        else:
            gate = real_gate
        self.scenario_of: dict[str, str] = {}
        self.started: list[str] = []                 # missions of the scenario in progress (see release)
        self.next_scenario = "namespace"

        def factory(mission):
            argv = agent_argv(self.scenario_of.get(mission.id, "namespace"), str(stacks / PROJECT))
            return CommandRuntime(self.containment, argv=argv, worker_socket=str(work / "unused.sock"),
                                  env_allow=list(AGENT_ENV_ALLOW))
        self.sup = Supervisor(self.store, self.containment, factory, enabled=lambda: True, artifact_dir=work / "artifacts",
                              workspace_dir=work / "ws", repair_gate=gate, verifier=self.controls.verifier,
                              checkpoint=self.controls.checkpoint, tripwire=self.controls.tripwire, docker_proxy=self.proxy,
                              socket_provider=ProxySockets(str(sock_root), self.proxy))
        self.flow = ApprovalFlow(self.store, supervisor=self.sup, validator=self.controls.validator,
                                 backup_status=self.controls.backup_status, protected=lambda: self.controls.protected,
                                 runtime="command")
        self.controls.checkpoint.startup_scan()
        self.controls.tripwire.start()
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()
        try:
            self.controls.tripwire.stop()
        finally:
            try:
                self.sup.shutdown()
            finally:
                self.proxy.shutdown()

    def release(self, rec: Recorder, label: str) -> bool:
        """End the scenario: cancel any mission of it that is not terminal so nothing carries leases into the next one."""
        mids, self.started = self.started, []
        return release_missions(rec, label, mids, status_of=self.status, cancel=self.sup.cancel, tick=self.sup.tick)

    def status(self, mission_id: str) -> str:
        return self.ms.get_mission(mission_id).status

    def drive(self, mission_id: str, until: Sequence[str], timeout_s: int) -> str:
        deadline = time.monotonic() + timeout_s
        status = self.status(mission_id)
        while time.monotonic() < deadline and status not in until:
            self.sup.tick()
            time.sleep(0.5)
            status = self.status(mission_id)
        return status

    def run_mission(self, scenario: str, rec: Recorder, *, title: str, scope: dict, on_verify_fail: str,
                    until: Sequence[str]) -> str | None:
        try:
            made = self.flow.propose_repair(OPERATOR, title=title, goal=title, scope=scope,
                                            checks=verification_checks(scenario), on_verify_fail=on_verify_fail,
                                            acknowledged_no_backup=True)
        except Exception as exc:                                    # noqa: BLE001
            rec.step(f"{scenario}:propose", False, f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        mid = made["mission_id"]
        self.scenario_of[mid] = scenario
        self.started.append(mid)
        rec.step(f"{scenario}:proposed", made["status"] == "awaiting_decision", mission=mid)
        card = self.flow.card(mid)
        for item in card["required"]:
            if item["state"] == "pending":
                self.flow.approve(OPERATOR, mid, item["phase"], item["bound"], elevated=True)
        rec.step(f"{scenario}:approved", True)
        status = self.drive(mid, until, self.args.timeout_s)
        results = [json.loads(e.body_json) for e in self.ms.events(mid)
                   if json.loads(e.body_json).get("event") == "verify.result"]
        rec.step(f"{scenario}:ended", status in until, status=status, verify=results[-1:] or None)
        summary = proxy_summary(self.ms.events(mid))
        rec.extra.setdefault("proxy", {})[scenario] = summary
        seen = went_through_proxy(summary)
        rec.step(f"{scenario}:through-proxy", seen, "" if seen else through_proxy_detail(summary),
                 writes=summary["writes"], created=summary["created"], refused=len(summary["refused"]))
        return mid


def scenario_namespace(rig: "Rig", rec: Recorder, stack_dir: Path) -> None:
    before = container_ids()
    rec.step("namespace:baseline", {"gateway", "app1", "app2", "web"} <= set(before), containers=sorted(before))
    mid = rig.run_mission("namespace", rec, title="rehearsal: recreate the gateway and its dependents",
                          scope={"stacks": [PROJECT], "hosts": ["local"]}, on_verify_fail="needs_operator",
                          until=("completed", "needs_operator", "rollback_pending", "recovery_required", "failed",
                                 "paused_tripwire"))
    after = container_ids()
    changed = {s for s in ("gateway", "app1", "app2") if before.get(s) != after.get(s)}
    rec.step("namespace:all-rebound", mid is not None and rig.status(mid) == "completed"
             and changed == {"gateway", "app1", "app2"}, changed=sorted(changed))
    order = [created_at(after[s]) for s in ("gateway", "app1", "app2") if s in after]
    rec.step("namespace:dependency-order", order == sorted(order) and len(order) == 3, created=order)


def scenario_canary(rig: "Rig", rec: Recorder, stack_dir: Path) -> None:
    compose = stack_dir / "docker-compose.yml"
    original = compose.read_text()
    before = container_ids()
    mid = rig.run_mission("canary-bad", rec, title="rehearsal: canary configuration change",
                          scope={"stacks": [PROJECT], "hosts": ["local"]}, on_verify_fail="rollback",
                          until=("rollback_pending", "completed", "needs_operator", "recovery_required", "failed",
                                 "paused_tripwire"))
    if mid is None:
        return
    if not rec.step("canary:failed-as-planned", rig.status(mid) == "rollback_pending", status=rig.status(mid)):
        return
    try:
        out = rig.flow.roll_back(OPERATOR, mid)
        rec.step("canary:rolled-back", out.get("status") == "rolled_back" and compose.read_text() == original,
                 status=out.get("status"))
    except Exception as exc:                                        # noqa: BLE001
        rec.step("canary:rolled-back", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    mid2 = rig.run_mission("canary-fix", rec, title="rehearsal: bounded configuration correction",
                           scope={"services": [f"{PROJECT}/web"], "hosts": ["local"]}, on_verify_fail="needs_operator",
                           until=("completed", "needs_operator", "rollback_pending", "recovery_required", "failed",
                                  "paused_tripwire"))
    after = container_ids()
    others = {s for s in ("gateway", "app1", "app2") if s in before and before[s] != after.get(s)}
    rec.step("canary:only-web-recreated", mid2 is not None and rig.status(mid2) == "completed" and not others,
             unexpectedly_recreated=sorted(others))


def project_scan(project: str) -> dict:
    """A Leela-shaped snapshot of the rehearsal project built from real Docker state (enough for baseline/verification)."""
    _, out = real_run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format",
                       '{{.Names}}|{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Status}}'])
    services, containers, missing = {}, [], []
    for line in out.splitlines():
        name, service, state, status = (line.split("|") + ["", "", "", ""])[:4]
        up = state == "running"
        services[service] = {"status": "healthy" if up else "failing", "state": state, "container": name}
        containers.append({"name": name, "status": status, "health": "", "restart_count": 0,
                           **({} if up else {"issue": f"not running ({status})"})})
        if not up:
            missing.append(service)
    row = {"stack": project, "status": "complete" if not missing and services else "incomplete",
           "missing_services": missing, "services": services}
    if missing or not services:
        row["alert"] = "HIGH"
    return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent": "rehearsal", "mode": "full",
            "containers": containers, "stack_completeness": [row], "disk": [], "docker_disk": {"rows": []},
            "mounts": {"active_count": 0, "missing": []}, "unraid_exports": {}, "nfs_mount_health": [],
            "vpn_port_forwarding": {}, "system": {}, "backups": {}, "services": {}, "image_candidates": [], "certs": []}


class RehearsalAuthenticator:
    """The proxy gate's authenticator for the rehearsal. The real one binds a session to its cgroup AND unique uid and refuses
    root; the rehearsal agent is root in a process group, so identity here is: the connecting pid is in the session's process
    group, and the session row (live, unexpired, bound as the supervisor recorded it) is then resolved by the same
    `resolve_session_by_id` the daemon uses. Tokens are not accepted (the docker CLI sends none)."""

    def __init__(self, store, containment: "ProcessContainment"):
        self.store, self.containment = store, containment

    def by_id(self, peer, session_id: str):
        from planet_express.missions.sessions import resolve_session_by_id
        from planet_express.missions.store import MissionStore
        if not self.containment.contains(session_id, peer.pid):
            return None
        row = MissionStore(self.store).get_session(session_id)
        if row is None:
            return None
        return resolve_session_by_id(self.store, session_id, peer_cgroup=row["cgroup_path"], peer_uid=row["session_uid"])

    def __call__(self, peer, request) -> None:
        return None


class ProxySockets:
    """The supervisor's socket provider for the rehearsal: a 0700 directory per session holding the real proxy's `proxy.sock`.
    There is no worker socket here. A session that would get no proxy socket fails to launch; it never falls back to the host."""

    def __init__(self, sock_root: str, proxy):
        self.sock_root, self.proxy = sock_root, proxy

    def path(self, session_id: str) -> str:
        return f"{self.sock_root}/{session_id}/worker.sock"

    def start(self, cs):
        directory = f"{self.sock_root}/{cs.session_id}"
        if len(directory) + len("/proxy.sock") > SOCKET_PATH_MAX:
            raise RuntimeError("the proxy socket path is too long for a unix socket: use a shorter --workdir")
        os.mkdir(directory, 0o700)
        try:
            stop = self.proxy.start(cs)
            if stop is None:
                raise RuntimeError("no Docker proxy socket for this session (its grant does not ask for the proxy)")
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        def stopper() -> None:
            try:
                stop()
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        return stopper

    def env(self, cs) -> dict[str, str]:
        return self.proxy.env(cs)


class ProcessContainment:
    """A minimal session containment over process groups, for the rehearsal only. It implements the same protocol as the real
    cgroup launcher (launch/freeze/thaw/kill/is_empty/cleanup) well enough for one agent command per session. It does NOT
    give isolation or a distinct uid: that is what scripts/mission_containment_rehearsal.py proves."""

    uid_range = (61000, 61010)

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}

    def cgroup_path(self, session_id: str) -> str:
        return f"/rehearsal/{session_id}"

    def launch(self, session_id, spec):
        from planet_express.missions.containment import ContainedSession
        with open(os.path.join(spec.cwd, "agent.log"), "ab") as log:
            proc = subprocess.Popen(list(spec.argv), cwd=spec.cwd, env=dict(spec.env), start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        self._procs[session_id] = proc
        return ContainedSession(session_id, "local_cgroup", self.cgroup_path(session_id), spec.uid)

    def contains(self, session_id: str, pid: int, getpgid: Callable[[int], int] = os.getpgid) -> bool:
        """Is `pid` in this session's process group (every child of the agent, `docker compose` plugin included)?"""
        proc = self._procs.get(session_id)
        if proc is None:
            return False
        try:
            return getpgid(pid) == proc.pid
        except (ProcessLookupError, PermissionError):
            return False

    def uid_in_use(self, uid: int) -> bool:
        return False

    def _alive(self, cs) -> bool:
        proc = self._procs.get(cs.session_id)
        if proc is None:
            return False
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def pids(self, cs) -> list[int]:
        return [self._procs[cs.session_id].pid] if self._alive(cs) else []

    def _signal(self, cs, sig: int) -> None:
        proc = self._procs.get(cs.session_id)
        if proc is not None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass

    def freeze(self, cs) -> None:
        import signal
        self._signal(cs, signal.SIGSTOP)

    def thaw(self, cs) -> None:
        import signal
        self._signal(cs, signal.SIGCONT)

    def kill(self, cs) -> None:
        import signal
        self._signal(cs, signal.SIGKILL)
        proc = self._procs.get(cs.session_id)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def is_empty(self, cs) -> bool:
        return not self._alive(cs)

    def cleanup(self, cs) -> None:
        self._procs.pop(cs.session_id, None)


if __name__ == "__main__":
    sys.exit(main())
