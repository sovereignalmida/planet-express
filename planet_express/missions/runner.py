"""The daemon-side assembly of mission control (v4 slice 3c).

`start_mission_runner(store, config)` is the single call the daemon makes. With `missions.enabled` false it
builds nothing and returns None. Enabled, it assembles, from `config.MISSIONS`:

    CgroupContainment (missions.cgroup_root, the worker account)
    CommandRuntime    (missions.runtime.argv / env_allow)
    Supervisor        (the store, the above, missions.enabled read at use time)
    worker RpcServer  (missions.worker_socket: the five worker methods, the Supervisor as backend)

and a poll thread that calls `Supervisor.tick()` (start queued Observe/Propose missions, advance running
ones). `stop()` stops the thread and the socket first (no new worker calls), then kills every session.
Missions interrupted by a stop stay `investigating`; `recover()` at the next start deals with them.
"""

import grp
import logging
import os
import pwd
import threading
from collections.abc import Callable

from planet_express.core.store import Store
from planet_express.missions.adapters.command import CommandRuntime
from planet_express.missions.containment import SessionContainment
from planet_express.missions.containment_cgroup import (
    CgroupContainment,
    cgroup2_mountpoint,
)
from planet_express.missions.supervisor import Supervisor
from planet_express.missions.worker_rpc import (
    CGROUP_MOUNT,
    WorkerAuthenticator,
    build_worker_server,
)

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0


class MissionRunner:
    def __init__(self, supervisor: Supervisor, worker_server=None, interval_s: float = POLL_INTERVAL_S):
        self.supervisor = supervisor
        self.worker_server = worker_server
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("mission runner already started")
        # The socket first: a session restarted by recover() may call it straight away.
        if self.worker_server is not None:
            self.worker_server.start()
        try:
            recovered = self.supervisor.recover()
            if recovered:
                log.warning("Reconciled %d in-flight mission(s) at startup", len(recovered))
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
            self._stop.wait(self.interval_s)

    def _stop_server(self) -> None:
        if self.worker_server is not None:
            try:
                self.worker_server.stop()
            except Exception:
                log.exception("worker socket did not stop cleanly")

    def stop(self) -> None:
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


def build_mission_runner(
    store: Store, cfg, *, containment: SessionContainment | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] = pwd.getpwnam,
    getgrgid: Callable[[int], grp.struct_group] = grp.getgrgid,
    interval_s: float = POLL_INTERVAL_S, enabled: Callable[[], bool] | None = None,
) -> MissionRunner | None:
    """None when missions are off or no agent command is configured (nothing could run). `enabled` is the live
    switch the Supervisor consults (the daemon passes the same source `MissionService` reads); it defaults to
    the config object's own flag."""
    if not cfg.enabled:
        return None
    if not cfg.runtime.argv:
        log.warning("missions.enabled is true but missions.runtime.argv is empty: no mission will run")
        return None
    account = getpwnam(cfg.worker_user)
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise ValueError("missions.worker_user must not be root")
    if account.pw_uid == os.geteuid():
        raise ValueError("missions.worker_user must not be the Core user")
    if containment is None:
        containment = CgroupContainment(cfg.cgroup_root, worker_uid=account.pw_uid, worker_gid=account.pw_gid)
    runtime = CommandRuntime(
        containment, argv=cfg.runtime.argv, env_allow=cfg.runtime.env_allow, worker_socket=cfg.worker_socket,
        worker_uid=account.pw_uid, worker_gid=account.pw_gid)
    supervisor = Supervisor(
        store, containment, lambda mission: runtime, enabled=enabled or (lambda: cfg.enabled))
    server = build_worker_server(cfg.worker_socket, supervisor, WorkerAuthenticator(store, mount=_cgroup_mount(cfg.cgroup_root)),
                                 group=getgrgid(account.pw_gid).gr_name)
    return MissionRunner(supervisor, server, interval_s)


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
