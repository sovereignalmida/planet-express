"""Fakes for the supervisor tests: a scripted AgentRuntime over FakeContainment."""
import os
from pathlib import Path

from missions_helpers import advance_to, make_store

from planet_express.missions.containment import FakeContainment
from planet_express.missions.models import Grant
from planet_express.missions.runtime import AgentResult, AgentUpdate, RunHandle
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import Supervisor

GOOD_RESULT = {"diagnosis": "NFS mount is stale", "evidence": ["mount shows stale handle"],
               "suggested_actions": ["remount the share"], "unknowns": [], "memory_candidates": [],
               "confidence": "medium"}


def update(state="running", events=(), output_bytes=0) -> AgentUpdate:
    return AgentUpdate(state=state, events=list(events), output_bytes=output_bytes)


def tool_event(name="read_file"):
    return {"kind": "tool", "body": {"tool": name}}


class FakeRuntime:
    def __init__(self, containment: FakeContainment, script=None, result=None, fail_start=None, raw_result=None,
                 fail_after_spawn=None):
        self.containment = containment
        self.script = list(script or [])
        self.result = AgentResult(**(result or GOOD_RESULT)) if not isinstance(result, Exception) else result
        self.fail_start = fail_start
        self.fail_after_spawn = fail_after_spawn      # the session IS launched, then the runtime raises
        self.raw_result = raw_result
        self.token = None
        self.context = None
        self.handle = None
        self.cancelled = False

    def start(self, mission, context, grant):
        if self.fail_start:
            raise self.fail_start
        self.context = context
        self.token = os.read(context.token_fd, 128).decode()
        pid = context.spawn(["/usr/bin/fake-agent", "--mission", mission.id])
        if self.fail_after_spawn:
            raise self.fail_after_spawn
        self.handle = RunHandle(run_id=f"run-{context.session.session_id}", session_id=context.session.session_id,
                                pid=pid)
        return self.handle

    def status(self, handle):
        return self.script.pop(0) if self.script else update()

    def cancel(self, handle):
        self.cancelled = True

    def collect(self, handle):
        if self.raw_result is not None:
            return self.raw_result
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Rig:
    def __init__(self, tmp_path: Path, tier="observe", budgets=None, enabled=True, behaviours=None,
                 memory_provider=None, evidence_provider=None):
        self.store, self.clock = make_store(tmp_path)
        self.ms = MissionStore(self.store)
        self.cont = FakeContainment()
        self.runtimes: list[FakeRuntime] = []
        self.behaviours = list(behaviours or [])
        self.tmp = tmp_path
        self.sup = self.new_supervisor(enabled, memory_provider, evidence_provider)
        body = {"tier": tier, "scope": {"stacks": ["media"], "hosts": ["local"]}}
        if budgets:
            body["budgets"] = budgets
        self.mission = self.ms.create_mission(title="t", origin="operator", grant=Grant.model_validate(body),
                                              goal="find out why media is down", success_criteria="a diagnosis",
                                              runtime="fake")
        advance_to(self.ms, self.mission.id, "queued")

    def add_replacement(self, session_id="S-replacement") -> str:
        """A second, live session for the mission that this supervisor instance did not start (as after a restart)."""
        from planet_express.missions.launcher.spec import LaunchSpec
        from planet_express.missions.sessions import issue_session
        cs, _ = issue_session(self.ms, self.mid, session_id, self.cont.cgroup_path(session_id), session_ttl_s=600,
                              lease_ttl_s=60, uid_range=self.cont.uid_range)
        self.cont.launch(session_id, LaunchSpec(uid=cs.session_uid, cwd="/w", argv=("/usr/bin/x",), token="t" * 40))
        return session_id

    def factory(self, mission):
        kw = self.behaviours.pop(0) if self.behaviours else {}
        rt = FakeRuntime(self.cont, **kw)
        self.runtimes.append(rt)
        return rt

    def new_supervisor(self, enabled=True, memory_provider=None, evidence_provider=None) -> Supervisor:
        return Supervisor(self.store, self.cont, self.factory, self.clock, enabled=enabled,
                          artifact_dir=self.tmp / "artifacts", workspace_dir=self.tmp / "ws",
                          memory_provider=memory_provider, evidence_provider=evidence_provider)

    @property
    def mid(self):
        return self.mission.id

    def status(self):
        return self.ms.get_mission(self.mid).status

    def sessions(self):
        import sqlite3
        with sqlite3.connect(self.store.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM mission_sessions WHERE mission_id=? ORDER BY rowid", (self.mid,))]    # insertion order

    def session_ids(self):
        return [s["id"] for s in self.sessions()]

    def state_path(self):
        return [(e.body_json) for e in self.ms.events(self.mid) if e.kind == "state"]

    def dump(self) -> str:
        """Everything persisted for the mission: every event body and every artifact file."""
        import sqlite3
        out = [e.body_json for e in self.ms.events(self.mid)]
        with sqlite3.connect(self.store.path) as conn:
            for (path,) in conn.execute("SELECT path FROM mission_artifacts WHERE mission_id=?", (self.mid,)):
                out.append(Path(path).read_text())
        return "\n".join(out)
