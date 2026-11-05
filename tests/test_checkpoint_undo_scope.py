"""A rollback reverses only what THIS run changed (rc14.3, from live M-22 / M-25 on the operator's host).

M-22, a Repair, appended a comment to the media stack's `.env` and recreated prowlarr with the same configuration, then completed.
M-25, a later mission, legitimately updated sonarr to a new image. "Undo this change" on M-22 then wrote `.env` back correctly, but
compared EVERY service of M-22's checkpoint with the host, saw sonarr differ (M-25's change), ran `compose up -d --no-deps sonarr`
and ended `needs_operator` with "media/sonarr: image_id, labels still differ from the checkpoint".

The rule: what a run changed is its checkpoint against its end-of-run post snapshot. Changed by the run and as the run left it: put
back. Changed by the run and changed again since: drift (refused until acknowledged). Not changed by the run but changed since:
someone else's, left alone and reported, never acted on and never counted against the outcome. No snapshot of services (a post
manifest from before rc14.3): compared with the checkpoint, as before.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_checkpoint_rollback_paths import (
    DOTENV,
    media_env,
    recorded,
    recreate_same_config,
)

from planet_express.missions import recovery

NEW_SONARR = "sha256:" + "5" * 64


def m25_updates_sonarr(env):
    """What M-25 did after M-22 ended: sonarr recreated on a new image (Compose relabels it too)."""
    doc = recorded("sonarr", cid="sonarr-2".ljust(64, "0"), config_hash="5050505050505050",
                   labels={"build_version": "Linuxserver.io version:- 4.0.15.2941-ls290"})
    doc["Image"] = NEW_SONARR
    env.docker.containers[("media", "sonarr")] = doc


def m22(env):
    """M-22's run: a comment appended to `.env`, prowlarr recreated with the same configuration; the post snapshot at its end."""
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# m22 was here\n")
    recreate_same_config(env, "prowlarr")
    env.svc.capture_post(cid)
    return cid


def done_event(env):
    bodies = [json.loads(e.body_json) for e in env.ms.events(env.mission.id)]
    return [b for b in bodies if b.get("event") == "checkpoint.restore_done"][-1]


def test_m22_undo_after_m25_restores_env_and_leaves_sonarr_alone(tmp_path):
    env = media_env(tmp_path)
    cid = m22(env)
    m25_updates_sonarr(env)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok, (report.unrestored, report.reason)
    assert report.written == [str(env.dotenv.resolve())] and env.dotenv.read_text() == DOTENV
    # sonarr was never named: no up, no restart, no stop, and it still runs M-25's image
    assert report.actions == {} and env.docker.up_calls == [] and env.docker.restarted == []
    assert env.docker.containers[("media", "sonarr")]["Image"] == NEW_SONARR
    assert report.unrestored == [] and report.why() is None
    assert report.left_alone == [{"service": "media/sonarr", "note": "changed since this run, left as it is"}]
    assert done_event(env)["left_alone"] == [{"service": "media/sonarr", "note": "changed since this run, left as it is"}]


def test_the_post_snapshot_records_services_and_which_the_run_changed(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.docker.containers[("media", "sonarr")]["Image"] = NEW_SONARR
    env.svc.capture_post(cid)
    post = env.svc.post(cid)
    assert set(post["services"]) == {"media/prowlarr", "media/sonarr", "media/archivist-es", "media/lidarr"}
    assert post["services"]["media/lidarr"] == {"sig": None}                      # no container then or at the end
    captured = [json.loads(e.body_json) for e in env.ms.events(env.mission.id)]
    assert [b for b in captured if b.get("event") == "checkpoint.post_captured"][-1]["services_changed"] == ["media/sonarr"]


def test_a_service_the_run_changed_and_someone_changed_again_is_drift(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# run\n")
    env.docker.containers[("media", "sonarr")]["Image"] = "sha256:" + "4" * 64     # the run moved sonarr to a new image
    env.svc.capture_post(cid)
    m25_updates_sonarr(env)                                                        # and a later mission moved it again
    report = env.svc.restore(cid)
    assert report.status == "refused" and report.written == [] and env.dotenv.read_text().endswith("# run\n")
    assert {"service": "media/sonarr", "expected": "as this run left it", "current": "changed since"} in report.drifted
    assert "changed again since" in report.reason and env.docker.up_calls == []
    assert env.ms.get_mission(env.mission.id).status == "needs_operator"
    # the operator's explicit acknowledgement puts it back, as before
    report = env.svc.restore(cid, ack_drift=True, actor="operator:alice")
    assert report.ok, report.unrestored
    assert report.actions.get("up") == ["media/sonarr"] and env.dotenv.read_text() == DOTENV


def test_a_file_the_run_did_not_change_but_someone_did_is_left_alone(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# run\n")                                    # the run's change
    env.svc.capture_post(cid)
    compose_after = env.compose.read_text() + "# a later mission's edit\n"
    env.compose.write_text(compose_after)                                          # not the run's
    report = env.svc.restore(cid)
    assert report.ok, (report.reason, report.unrestored)
    assert report.written == [str(env.dotenv.resolve())] and env.compose.read_text() == compose_after
    assert {"path": str(env.compose.resolve()), "note": "changed since this run, left as it is"} in report.left_alone
    assert report.drifted == []


def test_a_file_the_run_changed_and_someone_changed_again_is_still_drift(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# run\n")
    env.svc.capture_post(cid)
    env.dotenv.write_text(DOTENV + "\n# someone else, later\n")
    report = env.svc.restore(cid)
    assert report.status == "refused" and report.reason == "files changed since the post-change manifest"
    assert report.left_alone == [] and report.written == []


def test_without_a_service_snapshot_services_compare_with_the_checkpoint_as_before(tmp_path):
    env = media_env(tmp_path)
    cid = m22(env)
    legacy = env.svc.post(cid)
    del legacy["services"]                                                         # a post manifest taken before rc14.3
    env.svc.store.write_post(cid, legacy)
    m25_updates_sonarr(env)
    report = env.svc.restore(cid)
    assert report.left_alone == []
    assert report.actions.get("up") == ["media/sonarr"]                            # nothing to tell the run's changes apart
    assert env.dotenv.read_text() == DOTENV


def test_a_service_unreadable_at_the_post_is_treated_as_the_runs(tmp_path):
    env = media_env(tmp_path)
    cid = m22(env)
    post = env.svc.post(cid)
    post["services"]["media/sonarr"] = {"unknown": True}
    env.svc.store.write_post(cid, post)
    m25_updates_sonarr(env)
    report = env.svc.restore(cid)
    assert report.left_alone == [] and report.actions.get("up") == ["media/sonarr"]


def test_cancel_is_judged_on_what_this_run_changed(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.svc.capture_post(cid)                                                       # the run changed nothing it covers
    m25_updates_sonarr(env)                                                         # another mission did, later
    manifest = env.svc.manifest(cid)
    host = recovery.collect_host_state(env.svc, manifest)
    assert not recovery.matches_checkpoint(host, manifest)                          # no post: compared with the checkpoint
    assert recovery.matches_checkpoint(host, manifest, env.svc.post(cid))           # scoped: nothing of this run differs
    # ... but something THIS run changed and left changed still holds it
    env.dotenv.write_text(DOTENV + "\n# run\n")
    env.svc.capture_post(cid)
    m25_updates_sonarr(env)
    host = recovery.collect_host_state(env.svc, manifest)
    assert not recovery.matches_checkpoint(host, manifest, env.svc.post(cid))


def test_a_service_that_cannot_be_read_at_the_end_is_recorded_unknown_and_the_file_snapshot_is_kept(tmp_path, monkeypatch):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# run\n")
    real = env.provider.inspect
    monkeypatch.setattr(env.provider, "inspect",
                        lambda name: (_ for _ in ()).throw(RuntimeError("docker hung")) if name == "media/sonarr" else real(name))
    env.svc.capture_post(cid)
    post = env.svc.post(cid)
    assert post["services"]["media/sonarr"] == {"unknown": True} and post["files"][str(env.dotenv.resolve())]["sha256"]


def test_an_incomplete_restore_still_lists_what_it_left_alone(tmp_path):
    from planet_express.console.execution import rollback_view
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# run\n")
    env.docker.containers[("media", "prowlarr")]["Image"] = "sha256:" + "4" * 64       # the run changed prowlarr
    env.svc.capture_post(cid)
    env.compose.write_text(env.compose.read_text() + "# later, not the run's\n")
    env.svc.provider.up = lambda services: (_ for _ in ()).throw(RuntimeError("boom"))
    report = env.svc.restore(cid)
    assert report.status == "failed" and report.left_alone
    mission = env.ms.get_mission(env.mission.id)
    env.ms.append_event(mission.id, "decision", "core", {"event": "mission.rollback_incomplete", "reason": "boom"})
    view = rollback_view(env.ms, mission, False)
    assert view["state"] == "incomplete" and view["left_alone"] == [str(env.compose.resolve())]
