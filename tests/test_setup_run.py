"""Applying, progress, retry and undo from the browser's side (A4c), against the fake host."""
import copy

import pytest
from test_setup_apply import SECRET, fresh_host, standard_plan
from test_setup_plan import KEY, systemd_report
from test_setup_server import Clock, call, login, post
from test_setup_stages import REPO, put

from planet_express.setup.apply import apply as real_apply
from planet_express.setup.journal import Journal
from planet_express.setup.server import Sessions, create_app
from planet_express.setup.session import SetupSession
from planet_express.setup.undo import undo as real_undo


@pytest.fixture
def rig(tmp_path):
    clock, sessions, exposed = Clock(), None, {"v": False}
    sessions = Sessions(clock=clock)
    plans = {"p": standard_plan()}
    host = fresh_host()
    root = tmp_path / "j"
    session = SetupSession(
        discover_fn=lambda: copy.deepcopy(systemd_report()), plan_fn=lambda *a, **k: plans["p"], repo_root=REPO,
        apply_fn=lambda reviewed, replan: real_apply(reviewed, host=host, journal_root=root, replan=replan,
                                                     evidence_dir="/journal/evidence"),
        undo_fn=lambda reviewed: real_undo(reviewed, host=host, journal_root=root, evidence_dir="/journal/evidence"),
        journal_fn=lambda plan_id: Journal(root, plan_id))
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: exposed["v"], clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    return app, cookie, csrf, session, host, plans, exposed


def review(app, cookie, csrf):
    return post(app, cookie, csrf, "/api/plan").get_json()["plan_id"]


def events(app, cookie, after=0):
    return call(app, "GET", f"/api/events?after={after}", cookie=cookie).get_json()


def test_approving_the_plan_that_was_shown_applies_it_and_the_journal_tells_the_story(rig):
    app, cookie, csrf, session, host, *_ = rig
    plan_id = review(app, cookie, csrf)
    assert post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id}).get_json() == {"started": True}
    session.wait()
    data = events(app, cookie)
    assert data["phase"] == "done" and [s["status"] for s in data["steps"]] == ["ok"] * 3
    assert "/etc/pe/config.yaml" in host.nodes and data["seq"] > 0
    assert events(app, cookie, after=data["seq"])["events"] == []              # nothing new since


def test_a_stale_or_invented_plan_id_is_refused_and_nothing_runs(rig):
    app, cookie, csrf, session, host, *_ = rig
    review(app, cookie, csrf)
    refused = post(app, cookie, csrf, "/api/apply", json={"plan_id": "0123456789abcdef"})
    assert refused.status_code == 409 and "not the plan you were shown" in refused.get_json()["error"]
    assert host.mutations == 0 and session.phase == "idle"
    assert post(app, cookie, csrf, "/api/apply", json={}).status_code == 400


def test_changing_an_answer_after_review_means_the_plan_must_be_reviewed_again(rig):
    app, cookie, csrf, _session, _host, *_ = rig
    plan_id = review(app, cookie, csrf)
    put(app, cookie, csrf, {"tier": "restart"})
    refused = post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    assert refused.status_code == 409 and "review the plan again" in refused.get_json()["error"]


def test_nothing_is_applied_while_setup_is_exposed(rig):
    app, cookie, csrf, _session, host, _plans, exposed = rig
    plan_id = review(app, cookie, csrf)
    exposed["v"] = True
    assert post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id}).status_code == 403
    assert host.mutations == 0


def test_two_applies_are_one_run_and_a_second_is_a_conflict(rig):
    app, cookie, csrf, session, _host, *_ = rig
    plan_id = review(app, cookie, csrf)
    assert post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id}).status_code == 200
    session.wait()
    again = post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    assert again.status_code == 409 and "already been applied" in again.get_json()["error"]


def test_a_failure_stops_shows_the_reason_and_retry_finishes_the_job(rig):
    app, cookie, csrf, session, host, _plans, _ = rig
    host.add_file("/etc/pe/config.yaml.blocker", b"")
    host.nodes["/etc"].mode = 0o777                                    # untrusted parent: the first write refuses
    plan_id = review(app, cookie, csrf)
    post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    session.wait()
    data = events(app, cookie)
    failed = next(s for s in data["steps"] if s["status"] == "failed")
    assert data["phase"] == "stopped" and failed["reason"] and data["outcome"]["step"] == failed["id"]
    assert post(app, cookie, csrf, "/api/retry", json={"step": "s99"}).status_code == 409      # not the step that stopped
    host.nodes["/etc"].mode = 0o755                                    # the operator fixes the cause
    assert post(app, cookie, csrf, "/api/retry", json={"step": failed["id"]}).status_code == 200
    session.wait()
    assert events(app, cookie)["phase"] == "done"


def test_undo_reverts_and_the_progress_says_what_was_left(rig):
    app, cookie, csrf, session, host, *_ = rig
    before = {k for k in host.nodes if not k.startswith("/journal")}
    plan_id = review(app, cookie, csrf)
    post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    session.wait()
    assert post(app, cookie, csrf, "/api/undo").status_code == 200
    session.wait()
    data = events(app, cookie)
    assert data["phase"] == "undone" and {k for k in host.nodes if not k.startswith("/journal")} == before
    assert all(s["status"] == "undone" for s in data["steps"])


def test_no_secret_reaches_any_progress_response(rig):
    app, cookie, csrf, session, _host, *_ = rig
    plan_id = review(app, cookie, csrf)
    post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    session.wait()
    body = call(app, "GET", "/api/events?after=0", cookie=cookie).get_data(as_text=True)
    assert SECRET not in body and KEY not in body


def test_a_server_not_run_as_root_shows_the_plan_but_will_not_install(tmp_path):
    clock = Clock()
    sessions = Sessions(clock=clock)
    session = SetupSession(discover_fn=lambda: copy.deepcopy(systemd_report()), plan_fn=lambda *a, **k: standard_plan(),
                           repo_root=REPO, cannot_apply="This setup was not started as root.")
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: False, clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    plan_id = post(app, cookie, csrf, "/api/plan").get_json()["plan_id"]
    refused = post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    assert refused.status_code == 409 and "not started as root" in refused.get_json()["error"]
    assert "not started as root" in call(app, "GET", "/stage/review", cookie=cookie).get_data(as_text=True)


def test_install_and_done_pages_follow_the_run(rig):
    app, cookie, csrf, session, _host, *_ = rig
    assert call(app, "GET", "/stage/install", cookie=cookie).headers["Location"].endswith("/stage/review")
    assert call(app, "GET", "/stage/done", cookie=cookie).headers["Location"].endswith("/stage/review")
    plan_id = review(app, cookie, csrf)
    post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    session.wait()
    assert call(app, "GET", "/stage/install", cookie=cookie).status_code == 200
    done = call(app, "GET", "/stage/done", cookie=cookie).get_data(as_text=True)
    assert "OPEN DASHBOARD" in done and "NEXT THREE THINGS" in done


def test_the_run_routes_are_the_only_new_mutations_and_take_no_path_or_command(rig):
    app, *_ = rig
    rules = sorted(r.rule for r in app.url_map.iter_rules() if r.methods & {"POST", "PUT", "DELETE", "PATCH"})
    assert rules == ["/api/answers", "/api/apply", "/api/discover", "/api/llm/check", "/api/operator/totp", "/api/operator/verify", "/api/ping", "/api/plan", "/api/retry", "/api/telegram/find-chat", "/api/telegram/test", "/api/undo"]


def test_a_journal_that_cannot_be_read_still_answers_the_poll_with_json(tmp_path):
    clock = Clock()
    sessions = Sessions(clock=clock)

    def broken(plan_id):
        raise OSError("disk gone")
    session = SetupSession(discover_fn=lambda: copy.deepcopy(systemd_report()), plan_fn=lambda *a, **k: standard_plan(),
                           repo_root=REPO, apply_fn=lambda p, r: real_apply(p, host=fresh_host(), journal_root=tmp_path / "j", replan=r,
                                                                          evidence_dir="/journal/evidence"),
                           undo_fn=None, journal_fn=broken)
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: False, clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    plan_id = post(app, cookie, csrf, "/api/plan").get_json()["plan_id"]
    post(app, cookie, csrf, "/api/apply", json={"plan_id": plan_id})
    session.wait()
    reply = call(app, "GET", "/api/events", cookie=cookie)
    assert reply.status_code == 200 and "journal cannot be read" in reply.get_json()["journal_error"]
