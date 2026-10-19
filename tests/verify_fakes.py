"""Fakes for the verification and recovery tests (slice 8): a scripted bounded runner, a ready mission in `verifying`
with a real checkpoint and baseline, and a Verifier wired to them."""
import copy
import json
import time

from checkpoint_fakes import make_env
from missions_helpers import repair_grant

from planet_express.missions.approvals import record_approval
from planet_express.missions.models import VerificationPlan
from planet_express.missions.state import transition
from planet_express.missions.verify.baseline import normalize
from planet_express.missions.verify.checks import CheckEnv, HttpResponse
from planet_express.missions.verify.runner import Verifier

CLEAN = {
    "timestamp": "t", "agent": "casa_leela", "mode": "full",
    "containers": [{"name": "media-web-1", "status": "Up 2 days", "health": "healthy", "restart_count": 0}],
    "stack_completeness": [{"stack": "media", "status": "complete", "missing_services": [],
                            "services": {"web": {"status": "healthy", "state": "running",
                                                 "container": "media-web-1"}}},
                           {"stack": "photos", "status": "complete", "missing_services": [],
                            "services": {"app": {"status": "healthy", "state": "running",
                                                 "container": "photos-app-1"}}}],
    "disk": [{"mount": "/", "source": "/dev/sda", "used_pct": 40}],
    "docker_disk": {"rows": []}, "mounts": {"active_count": 1, "missing": []}, "unraid_exports": {},
    "nfs_mount_health": [], "vpn_port_forwarding": {}, "system": {"uptime": "x"}, "backups": {},
    "services": {"casa-stacks": "active"}, "image_candidates": [], "certs": [],
}


def snapshot(**over):
    snap = copy.deepcopy(CLEAN)
    snap.update(over)
    return snap


def media_down():
    """The media/web service missing and its container not running."""
    return snapshot(
        containers=[{"name": "media-web-1", "status": "Exited (1)", "health": "", "issue": "not running (Exited (1))"}],
        stack_completeness=[{"stack": "media", "status": "incomplete", "missing_services": ["web"], "alert": "HIGH",
                             "services": {"web": {"status": "failing", "state": "exited(1)",
                                                  "container": "media-web-1"}}}, CLEAN["stack_completeness"][1]])


def photos_down():
    return snapshot(stack_completeness=[CLEAN["stack_completeness"][0],
                                        {"stack": "photos", "status": "incomplete", "missing_services": ["app"],
                                         "alert": "HIGH", "services": {"app": {"status": "failing", "state": "absent"}}}])


class FakeRun:
    """A bounded runner answering by argv prefix: `script` is a list of (prefix, response) tried in order, where a
    response is (rc, out, err, truncated) or a callable(argv) returning one."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls: list[list[str]] = []
        self.delay = 0.0

    def on(self, prefix, response):
        self.script.insert(0, (list(prefix), response))

    def __call__(self, argv, timeout, max_bytes):
        self.calls.append(list(argv))
        if self.delay:
            time.sleep(self.delay)
        for prefix, response in self.script:
            if argv[:len(prefix)] == prefix:
                out = response(argv) if callable(response) else response
                if len(out) == 3:
                    out = (*out, False)
                return out
        return 127, "", "no such command", False


def http_ok(body=b"hello"):
    return lambda req: HttpResponse(200, body)


def check_env(run=None, **over):
    kw = dict(run=run or FakeRun(), http=http_ok(), exec_allowlist=frozenset(), sleep=lambda s: None,
              backstop_grace_s=0.5, regex_deadline_s=2.0)
    kw.update(over)
    return CheckEnv(**kw)


def plan_of(*checks):
    return VerificationPlan.model_validate({"catalogue_v": 1, "checks": list(checks)})


HTTP = {"id": "route", "check": "http", "params": {"url": "http://app.example/health", "consecutive": 1}}


class Rig:
    pass


def build(tmp_path, checks=(HTTP,), *, snap=None, run=None, env_over=None, grant_over=None, status="verifying",
          on_verify_fail="needs_operator"):
    """A repair mission over stack `media` with its plan attached, approved, checkpointed (baseline from `snap`) and,
    by default, sitting in `verifying`. `rig.fresh` is what the next scan returns: change it after the checkpoint."""
    plan = plan_of(*checks)
    grant = repair_grant(verification={"checks": [c["id"] for c in checks], "plan_sha256": plan.hash()},
                         on_verify_fail=on_verify_fail, **(grant_over or {}))
    rig = Rig()
    rig.env = make_env(tmp_path, grant=grant)
    rig.ms, rig.store, rig.svc, rig.mid = rig.env.ms, rig.env.store, rig.env.svc, rig.env.mission.id
    rig.plan, rig.grant = plan, grant
    rig.base = snap or snapshot()
    rig.fresh = copy.deepcopy(rig.base)
    rig.run = run or FakeRun()
    rig.svc.baseline_provider = lambda: normalize(rig.base)
    rig.ms.set_verification_plan(rig.mid, plan)
    rig.cid = rig.svc.create(rig.mid, rig.env.scope)
    rig.check_env = check_env(rig.run, **(env_over or {}))
    rig.verifier = Verifier(missions=rig.ms, checkpoint=rig.svc, scan=lambda: rig.fresh, env=rig.check_env,
                            stacks_root=str(rig.env.stacks), enabled=True, scan_timeout_s=5)
    for state in ("queued", "investigating", "awaiting_decision"):
        transition(rig.store, rig.mid, state, "operator:me")
    mission = rig.ms.get_mission(rig.mid)
    record_approval(rig.store, rig.mid, "grant", mission.grant_hash, "approve", "operator:me")
    if status in ("executing", "verifying"):
        transition(rig.store, rig.mid, "executing", "core")
    if status == "verifying":
        transition(rig.store, rig.mid, "verifying", "core")
    return rig


def result_events(rig):
    return [json.loads(e.body_json) for e in rig.ms.events(rig.mid)
            if e.kind == "note" and json.loads(e.body_json).get("event", "").startswith("verify.")]
