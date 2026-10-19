"""The daemon-side assembly of mission control (v4 slice 3c, launcher wiring in slice 3d).

`start_mission_runner(store, config)` is the single call the daemon makes. With `missions.enabled` false it
builds nothing and returns None. Enabled, it assembles, from `config.MISSIONS`:

    CgroupContainment (missions.cgroup_root, the launcher strategy, the session uid range)
    CommandRuntime    (missions.runtime.argv / env_allow)
    Supervisor        (the store, the above, missions.enabled read at use time, the preflight gate)
    per-session worker sockets (one RpcServer in each session's own directory, the Supervisor as backend)

and a poll thread that calls `Supervisor.tick()` (start queued Observe/Propose missions, advance running
ones). The launcher preflight runs first (and again every few minutes while it fails): a host that fails any step
is proposal-only and starts no session. `stop()` stops the thread first, then kills every session.
Missions interrupted by a stop stay `investigating`; `recover()` at the next start deals with them.
"""

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from planet_express.core.store import Store
from planet_express.missions.adapters.command import CommandRuntime
from planet_express.missions.containment import SessionContainment
from planet_express.missions.containment_cgroup import (
    CgroupContainment,
    cgroup2_mountpoint,
)
from planet_express.missions.hooks import install_claude_code, policy_for
from planet_express.missions.launcher.cgroup_tree import ensure_tree
from planet_express.missions.launcher.config import CONF_PATH, HELPER_PATH
from planet_express.missions.launcher.preflight import PreflightReport, run_preflight
from planet_express.missions.launcher.strategies import DirectLauncher, SudoLauncher
from planet_express.missions.supervisor import SocketProvider, Supervisor
from planet_express.missions.worker_rpc import (
    CGROUP_MOUNT,
    WorkerAuthenticator,
    build_worker_server,
)

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0


class MissionRunner:
    def __init__(self, supervisor: Supervisor, worker_server=None, interval_s: float = POLL_INTERVAL_S,
                 prepare: Callable[[], object] | None = None, tripwire=None, startup: list[Callable[[], object]] | None = None):
        self.supervisor = supervisor
        self.startup = list(startup or [])      # e.g. the checkpoint store's startup scan; each is isolated and logged
        self.controls = None                    # `assembly.RepairControls` when the daemon wired the Repair tier
        self.tripwire = tripwire          # `tripwire.TripwireMonitor` or None; it has its own thread and failure isolation
        self.prepare = prepare            # host preparation (Core's half of the cgroup layout), before anything runs
        self.worker_server = worker_server
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("mission runner already started")
        if self.prepare is not None:
            try:
                log.info("Mission cgroup tree: %s", self.prepare() or "already in place")
            except Exception as exc:         # the preflight reports what is then missing and the host is proposal-only
                log.warning("Could not prepare the mission cgroup tree: %s", exc)
        # The socket first: a session restarted by recover() may call it straight away.
        if self.worker_server is not None:
            self.worker_server.start()
        for step in self.startup:
            try:
                log.info("Mission startup step: %s", step())
            except Exception:
                log.exception("a mission startup step failed")
        try:
            recovered = self.supervisor.recover()
            if recovered:
                log.warning("Reconciled %d in-flight mission(s) at startup", len(recovered))
            if self.tripwire is not None:
                # Re-freeze every paused session and finish a pause that was begun but not recorded, then watch.
                acted = self.tripwire.recover()
                if acted:
                    log.warning("Tripwire: %d paused mission(s) re-frozen at startup", len(acted))
                self.tripwire.start()
        except Exception:
            self._stop_server()
            raise
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mission-runner")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.supervisor.tick()
            except Exception:
                log.exception("mission runner tick failed")
            if self.tripwire is not None:
                try:
                    self.tripwire.ensure_running()        # a dead monitor thread pauses everything and restarts
                except Exception:
                    log.exception("tripwire monitor check failed")
            self._stop.wait(self.interval_s)

    def _stop_server(self) -> None:
        if self.worker_server is not None:
            try:
                self.worker_server.stop()
            except Exception:
                log.exception("worker socket did not stop cleanly")

    def stop(self) -> None:
        if self.tripwire is not None:
            self.tripwire.stop()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._stop_server()
        self.supervisor.shutdown()


def _cgroup_mount(root: str) -> str:
    """The cgroup2 mount point that holds `root`: peers' /proc/<pid>/cgroup paths are relative to it."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return cgroup2_mountpoint(root, handle.read()) or CGROUP_MOUNT
    except OSError:
        return CGROUP_MOUNT


def choose_strategy(mode: str, euid: int) -> str:
    """`auto` is direct for a root Core and sudo otherwise. An explicit choice is returned as is, never downgraded."""
    if mode == "auto":
        return "direct" if euid == 0 else "sudo"
    return mode


def build_containment(cfg, *, euid: int | None = None) -> CgroupContainment:
    strategy = choose_strategy(cfg.launcher, os.geteuid() if euid is None else euid)
    launcher = DirectLauncher(HELPER_PATH, euid=euid) if strategy == "direct" else SudoLauncher(HELPER_PATH)
    return CgroupContainment(cfg.cgroup_root, launcher=launcher, uid_range=(cfg.session_uid_min, cfg.session_uid_max),
                             sock_root=cfg.sock_root)


class SessionSockets(SocketProvider):
    """One worker RpcServer per session, bound in `<sock_root>/<session_id>/`, which Core makes (it owns sock_root).
    The launcher then hands the socket to the session's uid and the directory to the session's private group (0750),
    so only that session can reach it. The server is built with no group: Core cannot chgrp to a uid it is not."""

    def __init__(self, sock_root: str, backend, authenticator, build=build_worker_server):
        self.sock_root, self.backend, self.authenticator, self._build = sock_root, backend, authenticator, build

    def path(self, session_id: str) -> str:
        return f"{self.sock_root}/{session_id}/worker.sock"

    def start(self, cs) -> Callable[[], None]:
        directory = os.path.dirname(self.path(cs.session_id))
        os.mkdir(directory, 0o700)
        server = self._build(self.path(cs.session_id), self.backend, self.authenticator, group=None)
        try:
            server.start()
        except BaseException:
            os.rmdir(directory)
            raise
        return server.stop


def make_preflight(cfg, containment: CgroupContainment, workspace_dir, *, euid: int | None = None) -> Callable[[], PreflightReport]:
    strategy = choose_strategy(cfg.launcher, os.geteuid() if euid is None else euid)
    low, high = cfg.session_uid_min, cfg.session_uid_max

    def run() -> PreflightReport:
        return run_preflight(
            containment, strategy=strategy, workspace_root=str(workspace_dir), sock_root=cfg.sock_root,
            probe_uid=high, helper=HELPER_PATH, conf=CONF_PATH, uid_range=(low, high), sudo=strategy == "sudo",
            check_reach=True)
    return run


def build_mission_runner(
    store: Store, cfg, *, containment: SessionContainment | None = None,
    interval_s: float = POLL_INTERVAL_S, enabled: Callable[[], bool] | None = None,
    preflight: Callable[[], PreflightReport] | None = None, socket_provider: SocketProvider | None = None,
    stacks_root: str | None = None, tool_broker=None, tripwire=None, repair_gate=None, env_policy=None, verifier=None,
    repair_wiring=None, docker_proxy=None, checkpoint=None,
) -> MissionRunner | None:
    """None when missions are off or no agent command is configured (nothing could run). `enabled` is the live
    switch the Supervisor consults (the daemon passes the same source `MissionService` reads); it defaults to
    the config object's own flag. With no injected `containment` the real one is built and the real preflight gates
    it; an injected containment (tests) is trusted and only gated if a `preflight` is passed too."""
    if not cfg.enabled:
        return None
    if not cfg.runtime.argv:
        log.warning("missions.enabled is true but missions.runtime.argv is empty: no mission will run")
        return None
    real = containment is None
    if real:
        containment = build_containment(cfg)
    protected = None
    if stacks_root is not None and (tool_broker is None or env_policy is None or repair_wiring is not None):
        # Slice 6 controls that need only the store and the config: the brokered tools (keys stay in Core's environment)
        # and the grant-to-environment policy. The tripwire monitor and the side-door gate need the checkpoint service
        # and the host's sudoers generator output, which the daemon supplies with slice 7 (`tripwire=`, `repair_gate=`).
        from planet_express.missions.grants import make_env_policy
        from planet_express.missions.store import MissionStore
        from planet_express.missions.tools import broker_from_config, default_registry
        from planet_express.missions.tripwire.build import (
            build_protected_set,
            protected_override_recorder,
        )
        protected = build_protected_set(cfg, stacks_root=stacks_root, record=protected_override_recorder(store))
        if tool_broker is None:
            tool_broker = broker_from_config(cfg, missions=MissionStore(store), stacks_root=stacks_root,
                                             protected=protected.resources)
        if env_policy is None:
            refs = dict(cfg.tool_secret_env)
            env_policy = make_env_policy(
                protected=protected, stacks_root=stacks_root, known_tools=default_registry().names(),
                secret_values=lambda: [v for v in (os.environ.get(var) for var in refs.values()) if v])
    controls = None
    if repair_wiring is not None and protected is not None:
        # Slice 7a: the checkpoint service, tripwire monitor, verifier and side-door gate, all or none. A failure to build any of
        # them leaves the host without Repair capability (observe/propose still run); it never yields a partial safety net.
        from planet_express.missions.assembly import build_repair_controls
        try:
            controls = build_repair_controls(store, cfg, repair_wiring, containment=containment, broker=tool_broker,
                                             protected=protected, stacks_root=stacks_root,
                                             enabled=enabled or (lambda: cfg.enabled), proxy=docker_proxy)
        except Exception as exc:
            log.warning("Repair missions are not available: %s: %s", type(exc).__name__, exc)
        else:
            tripwire, verifier, repair_gate, checkpoint = (controls.tripwire, controls.verifier, controls.repair_gate,
                                                           controls.checkpoint)
            if tool_broker is not None and tool_broker.write_gate is None:
                from planet_express.missions.assembly import mission_write_gate
                tool_broker.write_gate = mission_write_gate(store)
    runtime = CommandRuntime(
        containment, argv=cfg.runtime.argv, env_allow=cfg.runtime.env_allow, worker_socket=cfg.worker_socket,
        env_policy=env_policy)
    supervisor = Supervisor(
        store, containment, lambda mission: runtime, enabled=enabled or (lambda: cfg.enabled),
        workspace_dir=Path(cfg.workspace_root) if cfg.workspace_root else None, tool_broker=tool_broker,
        repair_gate=repair_gate, verifier=verifier, checkpoint=checkpoint, tripwire=tripwire, docker_proxy=docker_proxy,
        hook_policy=lambda mission: policy_for(cfg, mission.grant().protected.resources).to_json(),
        hook_install=((lambda workspace: install_claude_code(workspace, cfg.hooks.command))
                      if cfg.hooks.install_claude_code else None))
    if real:
        supervisor.preflight_fn = preflight or make_preflight(cfg, containment, supervisor.workspace_dir)
        supervisor.socket_provider = socket_provider or SessionSockets(
            cfg.sock_root, supervisor, WorkerAuthenticator(store, mount=_cgroup_mount(cfg.cgroup_root)))
    else:
        supervisor.preflight_fn = preflight
        supervisor.socket_provider = socket_provider
    prepare = (lambda: ensure_tree(cfg.cgroup_root)) if real else None
    runner = MissionRunner(supervisor, None, interval_s, prepare, tripwire=tripwire,
                           startup=[checkpoint.startup_scan] if checkpoint is not None else [])
    runner.controls = controls
    return runner


def start_mission_runner(store: Store, cfg, **kwargs) -> MissionRunner | None:
    """Build and start, or log and return None: mission control failing to start never stops the daemon."""
    try:
        runner = build_mission_runner(store, cfg, **kwargs)
        if runner is not None:
            runner.start()
        return runner
    except Exception as exc:
        log.warning("Mission control not started: %s", exc)
        return None
