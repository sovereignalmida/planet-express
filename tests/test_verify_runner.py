"""The baseline normaliser, the checkpoint's scan baseline, the Verifier and the supervisor's close-out (slice 8)."""
import copy
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify_fakes import CLEAN, HTTP, FakeRun, build, http_ok, media_down, photos_down, result_events, snapshot

from planet_express.missions.checkpoint import CheckpointCorrupt, CheckpointError
from planet_express.missions.supervisor import Supervisor
from planet_express.missions.verify.baseline import (
    MAX_FINDINGS,
    UNSCOPED,
    baseline_document,
    in_scope,
    new_findings,
    normalize,
)
from planet_express.missions.verify.checks import HttpResponse
from planet_express.missions.verify.runner import Verifier, VerifierDisabled, VerifyRefused

MEDIA = ["host:local/stack:media"]


# ── the normaliser ──────────────────────────────────────────────────────────
def test_same_host_state_gives_identical_records():
    a, b = normalize(snapshot()), normalize(copy.deepcopy(snapshot()))
    assert a == b and a == normalize(snapshot(timestamp="later"))
    assert baseline_document(a) == baseline_document(b)
    assert normalize(CLEAN) == []


def test_findings_are_keyed_by_what_and_where_not_by_volatile_numbers():
    one = snapshot(containers=[{"name": "media-web-1", "status": "Up", "health": "unhealthy",
                                "issue": "healthcheck failing", "restart_count": 1}])
    two = snapshot(containers=[{"name": "media-web-1", "status": "Up 9 minutes", "health": "unhealthy",
                                "issue": "healthcheck failing", "restart_count": 7}])
    assert normalize(one) == normalize(two)
    assert normalize(one)[0].resource_id == "host:local/service:media/web"        # via the stack's service/container map


def test_sections_map_to_canonical_resources_or_unscoped():
    snap = snapshot(
        disk=[{"mount": "/mnt/data", "used_pct": 95, "alert": "CRITICAL"}],
        mounts={"missing": ["/mnt/nas"]},
        nfs_mount_health=[{"container": "c", "path": "/mnt/nfs", "status": "stale", "alert": "HIGH"}],
        vpn_port_forwarding={"alert": "HIGH"}, unraid_exports={"duplicate_fsids": ["12"]},
        services={"casa-stacks": "inactive"}, backups={"daily": {"result": "failed"}},
        certs=[{"kind": "error", "domain": "x"}],
        containers=[{"name": "orphan", "status": "Exited", "issue": "not running (Exited (1))"}])
    found = {(f.kind, f.resource_id) for f in normalize(snap)}
    assert ("disk_alert", "host:local/path:/mnt/data") in found
    assert ("mount_missing", "host:local/path:/mnt/nas") in found
    assert ("nfs_mount", "host:local/path:/mnt/nfs") in found
    assert ("unit_not_active", "host:local/unit:casa-stacks") in found
    for kind in ("vpn_port", "duplicate_fsid", "backup_failed", "cert_error", "container_issue"):
        assert (kind, UNSCOPED) in found, kind                                       # cannot be mapped: unscoped


def test_unknown_sections_are_scanned_generically_and_informational_ones_skipped():
    assert normalize(snapshot(brand_new={"rows": [{"alert": "HIGH"}]}))[0].resource_id == UNSCOPED
    assert normalize(snapshot(brand_new={"rows": [{"fine": 1}]})) == []
    assert normalize(snapshot(system={"alert": "x"}, image_candidates=[{"alert": "x"}])) == []
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert normalize(snapshot(brand_new=deep))                                       # too deep to read: a finding, not a skip


def test_records_are_redacted_and_bounded():
    snap = snapshot(containers=[{"name": "web", "status": "Exited",
                                 "issue": "not running (API_KEY=sk-live-SECRET-9 exited)"}])
    dump = json.dumps([f.model_dump() for f in normalize(snap)])
    assert "SECRET-9" not in dump and "API_KEY" not in dump
    many = snapshot(disk=[{"mount": f"/m{i}", "alert": "HIGH"} for i in range(MAX_FINDINGS + 50)])
    out = normalize(many)
    assert len(out) <= MAX_FINDINGS and any(f.kind == "overflow" for f in out)


def test_baseline_vs_fresh_and_scope():
    base = normalize(media_down())
    assert new_findings(base, normalize(media_down())) == []                         # fault present at baseline: ignored
    assert in_scope(normalize(media_down())[0], MEDIA)
    new = new_findings(normalize(snapshot()), normalize(photos_down()))
    assert new and not any(in_scope(f, MEDIA) for f in new)                          # out of scope: ignored
    unscoped = new_findings([], normalize(snapshot(vpn_port_forwarding={"alert": "HIGH"})))
    assert unscoped[0].resource_id == UNSCOPED and in_scope(unscoped[0], MEDIA)
    assert in_scope(unscoped[0], [])                                                 # in scope for ANY mission


def test_a_scan_that_is_not_a_mapping_refuses():
    with pytest.raises(TypeError):
        normalize([])


# ── the checkpoint's baseline ───────────────────────────────────────────────
def test_checkpoint_persists_an_immutable_baseline_and_records_its_hash(tmp_path):
    rig = build(tmp_path, snap=media_down())
    manifest = rig.svc.manifest(rig.cid)
    text, sha = baseline_document(normalize(media_down()))
    assert manifest["baseline"] == {"sha256": sha, "findings": len(normalize(media_down()))}
    assert rig.svc.store.read_baseline(rig.cid).decode() == text
    assert rig.svc.baseline(rig.cid) == normalize(media_down())
    path = rig.svc.store.dir_for(rig.cid) / "baseline.json"
    path.chmod(0o600)
    path.write_text(text.replace("HIGH", "LOW"))
    with pytest.raises(CheckpointCorrupt):
        rig.svc.baseline(rig.cid)


def test_a_failed_baseline_scan_means_no_checkpoint(tmp_path):
    from checkpoint_fakes import make_env
    env = make_env(tmp_path)

    def boom():
        raise RuntimeError("scan died")

    env.svc.baseline_provider = boom
    with pytest.raises(CheckpointError, match="baseline scan failed"):
        env.svc.create(env.mission.id, env.scope)
    assert env.svc.store.list_ids() == [] and env.ms.get_mission(env.mission.id).checkpoint_id is None


def test_without_a_baseline_provider_the_checkpoint_has_none(tmp_path):
    from checkpoint_fakes import make_env
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.manifest(cid)["baseline"] is None and env.svc.baseline(cid) is None


# ── the verifier ────────────────────────────────────────────────────────────
def verified(rig):
    return rig.verifier.run(rig.mid)


def test_passes_only_when_every_check_passes_and_the_scan_is_clean(tmp_path):
    rig = build(tmp_path)
    result = verified(rig)
    assert result.status == "passed" and result.reason == ""
    assert result.grant_hash == rig.ms.get_mission(rig.mid).grant_hash and result.checkpoint_id == rig.cid
    assert result.plan_sha256 == rig.plan.hash() and result.scan.ok and result.scan.new_in_scope_count == 0
    stored = rig.ms.get_mission(rig.mid).verification_result_json
    assert json.loads(stored)["status"] == "passed"


def test_one_failing_check_fails_the_whole_result(tmp_path):
    checks = [HTTP, {"id": "mnt", "check": "mount_readable", "params": {"path": "/data", "container": "app"}}]
    rig = build(tmp_path, checks, run=FakeRun([(["docker", "exec"], (1, "", "nope"))]))
    result = verified(rig)
    assert result.status == "failed" and [c.ok for c in result.checks] == [True, False]
    assert "mnt" in result.reason and result.scan.ok                      # the scan was still taken: full evidence


def test_a_scan_failure_fails(tmp_path):
    rig = build(tmp_path)

    def broken():
        raise OSError("docker is down")

    rig.verifier.scan = broken
    result = verified(rig)
    assert result.status == "failed" and result.scan.ok is False and "scan failed" in result.scan.error


def test_a_scan_that_hangs_fails_within_the_deadline(tmp_path):
    import time
    rig = build(tmp_path)
    rig.verifier.scan = lambda: time.sleep(5)
    rig.verifier.scan_timeout_s = 0.3
    t0 = time.monotonic()
    assert verified(rig).status == "failed" and time.monotonic() - t0 < 3


def test_a_new_in_scope_finding_fails(tmp_path):
    rig = build(tmp_path)
    rig.fresh = media_down()
    result = verified(rig)
    assert result.status == "failed" and result.scan.new_in_scope_count >= 1
    assert result.scan.new_in_scope[0].resource_id.startswith("host:local/service:media/")
    assert "in scope" in result.reason


def test_a_finding_outside_the_scope_is_ignored(tmp_path):
    rig = build(tmp_path)
    rig.fresh = photos_down()
    result = verified(rig)
    assert result.status == "passed" and result.scan.new_out_of_scope >= 1 and result.scan.new_in_scope_count == 0


def test_a_baseline_finding_is_ignored(tmp_path):
    rig = build(tmp_path, snap=media_down())
    rig.fresh = media_down()
    assert verified(rig).status == "passed"


def test_a_new_unscoped_finding_fails_every_mission(tmp_path):
    rig = build(tmp_path)
    rig.fresh = snapshot(vpn_port_forwarding={"alert": "HIGH"})
    result = verified(rig)
    assert result.status == "failed" and result.scan.new_in_scope[0].resource_id == UNSCOPED


def test_a_missing_plan_or_baseline_or_checkpoint_fails(tmp_path):
    rig = build(tmp_path)
    with sqlite3.connect(rig.store.path) as conn:
        conn.execute("UPDATE missions SET verification_plan_json=NULL WHERE id=?", (rig.mid,))
    result = verified(rig)
    assert result.status == "failed" and result.checks == [] and "no verification plan" in result.reason
    rig = build(tmp_path / "b")
    rig.svc.store.dir_for(rig.cid).joinpath("baseline.json").unlink()
    result = verified(rig)
    assert result.status == "failed" and "baseline" in result.reason
    rig = build(tmp_path / "c")
    with sqlite3.connect(rig.store.path) as conn:
        conn.execute("UPDATE missions SET checkpoint_id=NULL WHERE id=?", (rig.mid,))
    assert verified(rig).status == "failed"


def test_a_tampered_plan_in_the_row_fails(tmp_path):
    rig = build(tmp_path)
    plan = json.loads(rig.ms.get_mission(rig.mid).verification_plan_json)
    plan["checks"][0]["params"]["expect_status"] = 500
    with sqlite3.connect(rig.store.path) as conn:
        conn.execute("UPDATE missions SET verification_plan_json=? WHERE id=?", (json.dumps(plan), rig.mid))
    result = verified(rig)
    assert result.status == "failed" and "plan_sha256" in result.reason


def test_an_exception_inside_the_verifier_is_a_failure_never_a_pass(tmp_path):
    rig = build(tmp_path)
    rig.verifier.checkpoint = type("Boom", (), {"baseline": lambda self, c: 1 / 0})()
    assert verified(rig).status == "failed"
    rig = build(tmp_path / "b", env_over={"http": lambda req: 1 / 0})
    assert verified(rig).status == "failed"


def test_disabled_and_wrong_state_run_nothing(tmp_path):
    rig = build(tmp_path)
    rig.verifier._enabled = lambda: False
    with pytest.raises(VerifierDisabled):
        verified(rig)
    assert rig.run.calls == [] and rig.ms.get_mission(rig.mid).verification_result_json is None
    rig = build(tmp_path / "b", status="executing")
    with pytest.raises(VerifyRefused):
        verified(rig)
    assert Verifier(missions=rig.ms, checkpoint=rig.svc, scan=lambda: {}, env=rig.check_env).enabled is False


def test_results_are_events_redacted_and_bounded(tmp_path):
    secret_http = lambda req: HttpResponse(200, b"API_KEY=sk-live-HUNTER2 token")       # noqa: E731
    rig = build(tmp_path, env_over={"http": secret_http})
    verified(rig)
    events = result_events(rig)
    assert [e["event"] for e in events] == ["verify.check", "verify.result"]
    with sqlite3.connect(rig.store.path) as conn:
        dump = "\n".join(r[0] for r in conn.execute("SELECT body_json FROM mission_events"))
        stored = conn.execute("SELECT verification_result_json FROM missions").fetchone()[0]
    assert "HUNTER2" not in dump and "HUNTER2" not in stored
    assert len(stored) < 5000


def test_a_failing_check_message_never_carries_command_output(tmp_path):
    checks = [{"id": "p", "check": "exec_probe",
               "params": {"container": "app", "argv": ["/usr/bin/curl", "x"], "expect_contains": "ok"}}]
    run = FakeRun([(["docker", "exec"], (0, "PASSWORD=hunter2-leak", ""))])
    rig = build(tmp_path, checks, run=run, env_over={"exec_allowlist": frozenset({"/usr/bin/curl"})})
    result = verified(rig)
    assert result.status == "failed"
    with sqlite3.connect(rig.store.path) as conn:
        dump = "\n".join(r[0] for r in conn.execute("SELECT body_json FROM mission_events")) + (
            conn.execute("SELECT verification_result_json FROM missions").fetchone()[0])
    assert "hunter2" not in dump


def test_http_env_default_is_not_used_when_a_fake_is_given(tmp_path):
    rig = build(tmp_path, env_over={"http": http_ok(b"fine")})
    assert verified(rig).status == "passed"


# ── the supervisor's close-out ──────────────────────────────────────────────
def supervisor(rig, verifier="default", enabled=True):
    return Supervisor(rig.store, None, None, enabled=enabled,
                      verifier=rig.verifier if verifier == "default" else verifier)


def test_verify_and_close_completes_only_on_a_passed_result(tmp_path):
    rig = build(tmp_path)
    assert supervisor(rig).verify_and_close(rig.mid) == "completed"
    m = rig.ms.get_mission(rig.mid)
    assert m.status == "completed" and m.outcome == "completed" and m.outcome_flags is None
    assert json.loads(m.verification_result_json)["status"] == "passed"


def test_verify_and_close_failed_goes_to_the_operator_with_the_summary(tmp_path):
    rig = build(tmp_path)
    rig.fresh = media_down()
    assert supervisor(rig).verify_and_close(rig.mid) == "needs_operator"
    m = rig.ms.get_mission(rig.mid)
    assert m.status == "needs_operator" and m.outcome == "needs_operator" and m.outcome_flags == "partially_applied"
    closed = [e for e in result_events(rig) if e["event"] == "verify.closed"]
    assert closed and closed[0]["status"] == "failed"


def test_verify_and_close_rolls_back_when_the_grant_says_so(tmp_path):
    rig = build(tmp_path, on_verify_fail="rollback")
    rig.fresh = media_down()
    assert supervisor(rig).verify_and_close(rig.mid) == "rollback_pending"
    assert rig.ms.get_mission(rig.mid).outcome == "rollback_pending"


def test_verify_and_close_without_a_verifier_or_with_a_broken_one_never_completes(tmp_path):
    rig = build(tmp_path)
    assert supervisor(rig, verifier=None).verify_and_close(rig.mid) == "needs_operator"
    rig = build(tmp_path / "b")
    rig.verifier.run = lambda m: 1 / 0
    assert supervisor(rig).verify_and_close(rig.mid) == "needs_operator"
    assert rig.ms.get_mission(rig.mid).status == "needs_operator"


def test_a_verifier_that_claims_pass_without_storing_a_result_is_stopped_by_the_gate(tmp_path):
    rig = build(tmp_path)

    class Liar:
        def run(self, mission):
            from planet_express.missions.verify.plan import VerificationResult
            return VerificationResult(status="passed", grant_hash=mission.grant_hash, checkpoint_id=mission.checkpoint_id,
                                      plan_sha256=rig.plan.hash(), at=1)       # returned, never stored

    assert supervisor(rig, verifier=Liar()).verify_and_close(rig.mid) == "needs_operator"
    assert rig.ms.get_mission(rig.mid).status == "needs_operator"


def test_missions_disabled_means_nothing_runs(tmp_path):
    rig = build(tmp_path)
    with pytest.raises(Exception, match="disabled"):
        supervisor(rig, enabled=False).verify_and_close(rig.mid)
    assert rig.ms.get_mission(rig.mid).status == "verifying" and rig.run.calls == []
