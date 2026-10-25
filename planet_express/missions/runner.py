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

import contextlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from planet_express.core.redact import redact
from planet_express.core.store import Store
from planet_express.missions.adapters.command import CommandRuntime
from planet_express.missions.containment import SessionContainment
from planet_express.missions.containment_cgroup import (
    CgroupContainment,
    cgroup2_mountpoint,
)
from planet_express.missions.discovery import (
    ACKNOWLEDGEABLE,
    DiscoveredInputs,
    discover_protected_inputs,
)
from planet_express.missions.hooks import claude_code_argv, install_claude_code, policy_for
from planet_express.missions.launcher.cgroup_tree import ensure_tree
from planet_express.missions.launcher.config import HELPER_PATH
from planet_express.missions.launcher.preflight import PreflightReport, run_preflight
from planet_express.missions.launcher.strategies import DirectLauncher, SudoLauncher
from planet_express.missions.model_broker import BrokerLimits, ModelBroker
from planet_express.missions.store import MissionStore
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
        self.console = None                     # `console.service.ConsoleService` when missions.console is enabled
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
        """Order matters: stop accepting starts, kill the sessions and prove them gone, and only THEN stop the tripwire. A
        session that is still running while nothing watches the host is exactly the window the tripwire exists to close."""
        self._stop.set()                                  # no new starts: the loop is the only thing that starts sessions
        getattr(self.supervisor, "begin_shutdown", lambda: None)()
        if self.tripwire is not None:
            getattr(self.tripwire, "quiesce", lambda: None)()      # no pause/freeze of a session that is being killed
        if self._thread is not None:
            self._thread.join(timeout=30)
        try:
            self._stop_server()
            self.supervisor.shutdown()                    # sessions killed and proven gone (or kept tracked, never dropped)
            shutdown = getattr(getattr(self.supervisor, "docker_proxy", None), "shutdown", None)
            if shutdown is not None:
                shutdown()
        finally:
            # In its own finally: a failure above must never leave a loopback listener spending Core's key for a stopped session.
            broker_shutdown = getattr(getattr(self.supervisor, "model_broker", None), "shutdown", None)
            if broker_shutdown is not None:
                try:
                    broker_shutdown()
                except Exception:                                # noqa: BLE001
                    log.exception("the model broker did not shut down cleanly")
            if self.tripwire is not None:
                if getattr(self.supervisor, "tracked_sessions", 0):
                    # A session could not be killed or proven empty: it is still on the host. Keep watching (and pausing)
                    # rather than stop the one thing that would notice what it does.
                    log.error("%d session(s) not proven gone at shutdown: the tripwire stays on", self.supervisor.tracked_sessions)
                    getattr(self.tripwire, "unquiesce", lambda: None)()
                else:
                    self.tripwire.stop()


def _cgroup_mount(root: str) -> str:
    """The cgroup2 mount point that holds `root`: peers' /proc/<pid>/cgroup paths are relative to it."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return cgroup2_mountpoint(root, handle.read()) or CGROUP_MOUNT
    except OSError:
        return CGROUP_MOUNT


def _protected_gids() -> tuple[int, ...]:
    """The gids a session may never join: root's and the docker group's (a member reaches the daemon without the proxy)."""
    import grp
    gids = [0]
    try:
        gids.append(grp.getgrnam("docker").gr_gid)
    except KeyError:
        pass
    return tuple(gids)


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


def without_broker_key(policy, broker):
    """Whatever else decides a session's environment, the model API key the broker holds never reaches it: by name or by value.
    Independent of stack discovery, so a config that skips the environment builder is still safe."""
    def wrapped(grant, mission_id, workspace, env):
        out = dict(policy(grant, mission_id, workspace, env)) if policy is not None else dict(env)
        secrets = broker.secret_values()
        return {k: v for k, v in out.items() if k != broker.key_env and not any(s in v for s in secrets)}
    return wrapped


class SessionSockets(SocketProvider):
    """One worker RpcServer per session, bound in `<sock_root>/<session_id>/`, which Core makes (it owns sock_root).
    The launcher then hands the socket to the session's uid and the directory to the session's private group (0750),
    so only that session can reach it. The server is built with no group: Core cannot chgrp to a uid it is not.
    With a Docker proxy service, a Repair session whose grant says `docker: proxy` also gets `proxy.sock` in the same
    directory: bound by Core (0600), handed over by the helper like the worker socket, never chown'd or widened by Core."""

    def __init__(self, sock_root: str, backend, authenticator, build=build_worker_server, proxy=None, model_broker=None):
        self.sock_root, self.backend, self.authenticator, self._build = sock_root, backend, authenticator, build
        self.proxy = proxy
        self.model_broker = model_broker      # the per-session model broker: Core holds the API key, the session an access code

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
        stops: list[Callable[[], None]] = [server.stop]
        try:
            for service in (self.proxy, self.model_broker):
                if service is not None:
                    stop_service = service.start(cs)
                    if stop_service is not None:
                        stops.append(stop_service)
        except BaseException:
            for stop_one in reversed(stops):
                with contextlib.suppress(Exception):
                    stop_one()
            with contextlib.suppress(OSError):
                os.rmdir(directory)
            raise

        if len(stops) == 1:
            return server.stop

        def stop() -> None:
            failure: BaseException | None = None
            for stop_one in reversed(stops):          # services first, the worker socket last; every one runs
                try:
                    stop_one()
                except BaseException as exc:          # noqa: BLE001 -- the first failure is raised after the rest ran
                    failure = failure or exc
            if failure is not None:
                raise failure
        return stop

    def env(self, cs) -> dict[str, str]:
        env: dict[str, str] = {}
        for service in (self.proxy, self.model_broker):
            if service is not None:
                env.update(service.env(cs))
        return env


def held_session_uids(store: Store) -> list[int]:
    """Uids that sessions still hold (not yet released by the reaper)."""
    with store._connect() as conn:
        return [r[0] for r in conn.execute("SELECT session_uid FROM mission_sessions "
                                           "WHERE session_uid IS NOT NULL AND uid_released_at IS NULL")]


def make_preflight(cfg, containment: CgroupContainment, workspace_dir, *, euid: int | None = None, held_uids=None) -> Callable[[], PreflightReport]:
    strategy = choose_strategy(cfg.launcher, os.geteuid() if euid is None else euid)
    low, high = cfg.session_uid_min, cfg.session_uid_max

    def run() -> PreflightReport:
        return run_preflight(
            containment, strategy=strategy, workspace_root=str(workspace_dir), sock_root=cfg.sock_root,
            probe_uid=high, helper=HELPER_PATH, conf=cfg.launch_conf, uid_range=(low, high), sudo=strategy == "sudo",
            check_reach=True, held_uids=held_uids)
    return run


def build_mission_runner(
    store: Store, cfg, *, containment: SessionContainment | None = None,
    interval_s: float = POLL_INTERVAL_S, enabled: Callable[[], bool] | None = None,
    preflight: Callable[[], PreflightReport] | None = None, socket_provider: SocketProvider | None = None,
    stacks_root: str | None = None, tool_broker=None, tripwire=None, repair_gate=None, env_policy=None, verifier=None,
    repair_wiring=None, docker_proxy=None, checkpoint=None, discovery=None, model_broker=None,
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
    mb = cfg.model_broker
    if model_broker is None and real and mb.enabled:
        model_broker = ModelBroker(
            upstream_host=mb.upstream_host, key_env=mb.key_env, models=list(mb.models), store=MissionStore(store),
            limits=BrokerLimits(max_requests=mb.max_requests, max_tokens=mb.max_tokens, timeout_s=float(mb.timeout_s)),
            path_prefix=mb.upstream_path_prefix, auth_scheme=mb.auth_scheme, session_env=mb.session_env,
            allow_body_keys=mb.allow_body_keys)
    if model_broker is not None and not model_broker.secret_values():
        log.error("missions.model_broker is enabled but %s is not set in Core's environment: no mission session can start until it is",
                  cfg.model_broker.key_env)
    protected = None
    found = DiscoveredInputs()
    if stacks_root is not None and (tool_broker is None or env_policy is None or repair_wiring is not None):
        # Slice 6 controls that need only the store and the config: the brokered tools (keys stay in Core's environment)
        # and the grant-to-environment policy. The tripwire monitor and the side-door gate need the checkpoint service
        # and the host's sudoers generator output, which the daemon supplies with slice 7 (`tripwire=`, `repair_gate=`).
        from planet_express.missions.grants import make_env_policy
        from planet_express.missions.tools import broker_from_config, default_registry
        from planet_express.missions.tripwire.build import (
            build_protected_set,
            protected_override_recorder,
        )
        try:
            found = (discovery or discover_protected_inputs)(cfg, stacks_root=stacks_root)
        except Exception as exc:       # discovery must not take mission control down; it does withhold Repair
            found = DiscoveredInputs(failures={d: f"discovery raised {type(exc).__name__}" for d in ACKNOWLEDGEABLE})
        protected = build_protected_set(cfg, stacks_root=stacks_root, record=protected_override_recorder(store),
                                        **found.kwargs())
        if tool_broker is None:
            tool_broker = broker_from_config(cfg, missions=MissionStore(store), stacks_root=stacks_root,
                                             protected=protected.resources)
        if env_policy is None:
            refs = dict(cfg.tool_secret_env)
            env_policy = make_env_policy(
                protected=protected, stacks_root=stacks_root, known_tools=default_registry().names(),
                secret_values=lambda: [v for v in (os.environ.get(var) for var in refs.values()) if v]
                + (model_broker.secret_values() if model_broker is not None else []))
    authenticator = WorkerAuthenticator(store, mount=_cgroup_mount(cfg.cgroup_root)) if real else None
    if docker_proxy is None and real and repair_wiring is not None and stacks_root is not None:
        # Built BEFORE the controls: the tripwire's pause closes the proxy through the callback `build_repair_controls` captures,
        # so the service has to exist when that is assembled (a pause that never reached the proxy would leave it open).
        from planet_express.missions.dockerproxy.service import DockerProxyService
        docker_proxy = DockerProxyService(
            store, authenticator=authenticator, docker_socket=cfg.docker_socket, sock_root=cfg.sock_root,
            stacks_root=stacks_root, protected=lambda: protected.resources if protected is not None else (),
            protected_gids=_protected_gids(), label_allow=cfg.docker_label_allow)
    controls = None
    if repair_wiring is not None and protected is not None and found.failures:
        # G4-F1: a required protected domain could not be established, so the tripwire and the proxy would be partly blind.
        # Observe and Propose still run; Repair needs the whole safety net.
        reasons = "; ".join(f"{d}: {w}" for d, w in sorted(found.failures.items()))
        log.warning("Repair missions are not available: protected-set discovery incomplete (%s)", reasons)
        try:
            store.record_event("mission.protected_discovery_incomplete", domains=sorted(found.failures),
                               reason=redact(reasons)[:1024])
        except Exception as exc:
            log.warning("could not record the discovery failure: %s", exc)
    elif repair_wiring is not None and protected is not None:
        # Slice 7a: the checkpoint service, tripwire monitor, verifier and side-door gate, all or none. A failure to build any of
        # them leaves the host without Repair capability (observe/propose still run); it never yields a partial safety net.
        from planet_express.missions.assembly import build_repair_controls
        try:
            controls = build_repair_controls(store, cfg, repair_wiring, containment=containment, broker=tool_broker,
                                             protected=protected, stacks_root=stacks_root,
                                             enabled=enabled or (lambda: cfg.enabled), proxy=docker_proxy,
                                             model_broker=model_broker)
        except Exception as exc:
            log.warning("Repair missions are not available: %s: %s", type(exc).__name__, exc)
        else:
            tripwire, verifier, repair_gate, checkpoint = (controls.tripwire, controls.verifier, controls.repair_gate,
                                                           controls.checkpoint)
            if tool_broker is not None and tool_broker.write_gate is None:
                from planet_express.missions.assembly import mission_write_gate
                tool_broker.write_gate = mission_write_gate(store)
    if model_broker is not None:
        env_policy = without_broker_key(env_policy, model_broker)
    runtime = CommandRuntime(
        containment, argv=cfg.runtime.argv, env_allow=cfg.runtime.env_allow, worker_socket=cfg.worker_socket,
        env_policy=env_policy)
    console_store = console_runtime = None
    if cfg.console.enabled:
        from planet_express.console.runtime import ConsoleRuntime
        from planet_express.console.store import ConsoleStore
        console_store = ConsoleStore(store)
        console_runtime = ConsoleRuntime(containment, console_store, cfg.console, worker_socket=cfg.worker_socket,
                                         env_policy=env_policy, broker=model_broker)

    def runtime_for(mission):
        if getattr(mission, "runtime", None) != "console":
            return runtime
        if console_runtime is None:
            raise ValueError("this is a Console conversation and the Console is off (missions.console.enabled)")
        return console_runtime
    supervisor = Supervisor(
        store, containment, runtime_for, enabled=enabled or (lambda: cfg.enabled),
        workspace_dir=Path(cfg.workspace_root) if cfg.workspace_root else None, tool_broker=tool_broker,
        repair_gate=repair_gate, verifier=verifier, checkpoint=checkpoint, tripwire=tripwire, docker_proxy=docker_proxy,
        hook_policy=lambda mission: policy_for(cfg, mission.grant().protected.resources).to_json(),
        hook_install=((lambda hook_dir: install_claude_code(hook_dir, cfg.hooks.command))
                      if cfg.hooks.install_claude_code else None),
        hook_argv=claude_code_argv if cfg.hooks.install_claude_code else None,
        hook_root=Path(cfg.hooks.root) if cfg.hooks.root else None, model_broker=model_broker)
    if real:
        supervisor.preflight_fn = preflight or make_preflight(cfg, containment, supervisor.workspace_dir,
                                                       held_uids=lambda: held_session_uids(store))
        supervisor.socket_provider = socket_provider or SessionSockets(
            cfg.sock_root, supervisor, authenticator, proxy=docker_proxy if hasattr(docker_proxy, "start") else None,
            model_broker=model_broker)
    else:
        supervisor.preflight_fn = preflight
        supervisor.socket_provider = socket_provider
    prepare = (lambda: ensure_tree(cfg.cgroup_root)) if real else None
    runner = MissionRunner(supervisor, None, interval_s, prepare, tripwire=tripwire,
                           startup=[checkpoint.startup_scan] if checkpoint is not None else [])
    runner.controls = controls
    if console_store is not None:
        from planet_express.console.service import ConsoleService
        runner.console = ConsoleService(MissionStore(store), console_store, supervisor, cfg.console,
                                        uid_range=lambda: containment.uid_range, clock=store._clock)
        supervisor.turn_ended, supervisor.console_tick = runner.console.turn_ended, runner.console.tick
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
