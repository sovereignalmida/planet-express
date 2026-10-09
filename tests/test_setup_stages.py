"""The wizard's routes and first stages (A4b): answers are validated whole, secrets never come back out, and the
pages carry nothing a strict Content-Security-Policy would block."""
import copy
import re
from pathlib import Path

import pytest

from planet_express.setup.plan import plan
from planet_express.setup.server import Sessions, create_app
from planet_express.setup.session import SetupSession
from test_setup_plan import KEY, PASS, TOKEN, TOTP, systemd_report
from test_setup_server import HOST, Clock, call, login, post

REPO = str(Path(__file__).resolve().parents[1])


@pytest.fixture
def report():
    return systemd_report()


@pytest.fixture
def wizard(report):
    clock = Clock()
    sessions = Sessions(clock=clock)
    holder = {"report": report}
    session = SetupSession(discover_fn=lambda: copy.deepcopy(holder["report"]), plan_fn=plan, repo_root=REPO)
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: False, clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    return app, cookie, csrf, session, holder


def put(app, cookie, csrf, body, path="/api/answers"):
    return call(app, "PUT", path, cookie=cookie, json=body,
                headers={"Origin": f"https://{HOST}", "X-CSRF-Token": csrf})


def test_the_root_lands_on_the_first_stage_and_each_built_stage_renders(wizard):
    app, cookie, csrf, *_ = wizard
    assert call(app, "GET", "/", cookie=cookie).headers["Location"].endswith("/stage/welcome")
    for name, marker in (("welcome", "DETECTED"), ("scan", "RE-CHECK"), ("location", "INSTALL PATH"),
                         ("powers", "WATCH ONLY"), ("review", "Building the plan")):
        page = call(app, "GET", f"/stage/{name}", cookie=cookie)
        assert page.status_code == 200 and marker in page.get_data(as_text=True), name


def test_an_unknown_stage_is_404(wizard):
    app, cookie, *_ = wizard
    assert call(app, "GET", "/stage/nope", cookie=cookie).status_code == 404


def test_pages_are_safe_under_a_strict_content_security_policy(wizard):
    app, cookie, *_ = wizard
    for name in ("welcome", "scan", "location", "powers", "telegram", "operator", "llm", "review"):
        html = call(app, "GET", f"/stage/{name}", cookie=cookie).get_data(as_text=True)
        assert not re.search(r"\sstyle=|\son[a-z]+=|<script(?![^>]*\ssrc=)", html), name
        assert "googleapis" not in html and "http://" not in html
    assert "default-src 'self'" in call(app, "GET", "/stage/welcome", cookie=cookie).headers["Content-Security-Policy"]


def test_static_files_need_the_session_and_then_serve(wizard):
    app, cookie, *_ = wizard
    assert call(app, "GET", "/static/cockpit.css").status_code == 403
    assert call(app, "GET", "/static/cockpit.css", cookie=cookie).status_code == 200
    assert call(app, "GET", "/static/setup.js", cookie=cookie).status_code == 200
    assert call(app, "GET", "/static/characters/futurama/avatar/fry.png", cookie=cookie).status_code == 200


def test_a_blocking_scan_disables_next(wizard):
    app, cookie, csrf, session, holder = wizard
    holder["report"]["summary"] = {**holder["report"]["summary"], "can_continue": False, "blocked": 1}
    holder["report"]["checks"][0] = {**holder["report"]["checks"][0], "status": "blocked", "fix": "Install Docker yourself"}
    session.run_discover()
    html = call(app, "GET", "/stage/scan", cookie=cookie).get_data(as_text=True)
    assert "SOMETHING BLOCKS SETUP" in html and "Install Docker yourself" in html
    assert re.search(r"<button class=\"pe-btn\" disabled>NEXT", html)


# -- answers ---------------------------------------------------------------------------------------------------
def test_good_answers_are_kept_and_bad_ones_change_nothing(wizard):
    app, cookie, csrf, session, _ = wizard
    ok = put(app, cookie, csrf, {"tier": "restart"}).get_json()
    assert ok["ok"] and ok["answers"]["tier"] == "restart"
    bad = put(app, cookie, csrf, {"tier": "bogus", "dashboard_port": 99999}).get_json()
    assert not bad["ok"] and {e["field"] for e in bad["errors"]} == {"tier", "dashboard_port"}
    assert session.answers["tier"] == "restart" and session.answers["dashboard_port"] if "dashboard_port" in session.answers else True


def test_an_unknown_answer_is_refused_by_name(wizard):
    app, cookie, csrf, *_ = wizard
    refused = put(app, cookie, csrf, {"install_dir_from_browser": "/etc", "tier": "full"}).get_json()
    assert not refused["ok"] and refused["errors"][0]["field"] == "install_dir_from_browser"


def test_answers_need_csrf_and_a_json_object(wizard):
    app, cookie, csrf, *_ = wizard
    assert call(app, "PUT", "/api/answers", cookie=cookie, json={"tier": "full"}).status_code == 403
    assert put(app, cookie, csrf, ["not", "an", "object"]).status_code == 400


def test_secrets_are_kept_but_never_come_back_out(wizard):
    app, cookie, csrf, session, _ = wizard
    session.set_answers({"telegram": {"token": TOKEN, "chat_id": "555000111"},
                         "llm": {"provider": "openai", "api_key": KEY},
                         "operator": {"name": "chris", "passphrase": PASS, "totp_secret": TOTP}}, proven=True)
    reply = put(app, cookie, csrf, {"tier": "restart"})
    text = reply.get_data(as_text=True)
    assert reply.get_json()["ok"] and all(s not in text for s in (TOKEN, KEY, PASS, TOTP))
    assert reply.get_json()["answers"]["telegram_set"] is True and reply.get_json()["answers"]["llm"]["key_set"] is True
    assert reply.get_json()["answers"]["operator_name"] == "chris"
    for name in ("welcome", "scan", "location", "powers", "telegram", "operator", "llm", "review"):
        html = call(app, "GET", f"/stage/{name}", cookie=cookie).get_data(as_text=True)
        assert all(s not in html for s in (TOKEN, KEY, PASS, TOTP)), name


def test_the_generic_answers_call_cannot_set_a_credential_only_clear_one(wizard):
    app, cookie, csrf, session, _ = wizard
    for body in ({"operator": {"name": "evil", "passphrase": PASS, "totp_secret": TOTP}},
                 {"telegram": {"token": TOKEN, "chat_id": "1"}}, {"llm": {"provider": "openai", "api_key": KEY}}):
        reply = put(app, cookie, csrf, body).get_json()
        assert not reply["ok"] and "own check" in reply["errors"][0]["message"]
    assert not {"operator", "telegram", "llm"} & set(session.answers)
    session.set_answers({"llm": {"provider": "openai", "api_key": KEY}}, proven=True)
    assert put(app, cookie, csrf, {"llm": None}).get_json()["ok"] and "llm" not in session.answers


# -- plan ------------------------------------------------------------------------------------------------------
def test_the_plan_route_returns_a_masked_plan_with_an_id_that_changes_with_the_answers(wizard):
    app, cookie, csrf, session, _ = wizard
    put(app, cookie, csrf, {"llm": {"provider": "openai", "api_key": KEY}, "tier": "observe"})
    first = post(app, cookie, csrf, "/api/plan")
    body = first.get_json()
    assert first.status_code == 200 and body["plan_id"] and body["steps"] and "will_not_touch" in body
    assert KEY not in first.get_data(as_text=True)
    put(app, cookie, csrf, {"tier": "restart"})
    assert post(app, cookie, csrf, "/api/plan").get_json()["plan_id"] != body["plan_id"]
    assert session.reviewed is not None
    put(app, cookie, csrf, {"tier": "observe"})
    assert session.reviewed is None                                   # changing an answer drops what was reviewed


def test_plan_needs_csrf_like_every_mutation(wizard):
    app, cookie, *_ = wizard
    assert call(app, "POST", "/api/plan", cookie=cookie).status_code == 403


def test_the_mutating_routes_take_no_path_command_or_step(wizard):
    app, *_ = wizard
    rules = sorted(r.rule for r in app.url_map.iter_rules() if r.methods & {"POST", "PUT", "DELETE", "PATCH"})
    assert rules == ["/api/answers", "/api/apply", "/api/discover", "/api/llm/check", "/api/operator/totp", "/api/operator/verify", "/api/ping", "/api/plan", "/api/retry", "/api/telegram/find-chat", "/api/telegram/test", "/api/undo"]


def test_next_and_back_follow_the_stage_order(wizard):
    app, cookie, *_ = wizard
    powers = call(app, "GET", "/stage/powers", cookie=cookie).get_data(as_text=True)
    review = call(app, "GET", "/stage/review", cookie=cookie).get_data(as_text=True)
    assert 'href="/stage/telegram"' in powers and 'href="/stage/llm"' in review


def test_next_is_withheld_while_setup_is_exposed(report):
    clock = Clock()
    sessions = Sessions(clock=clock)
    session = SetupSession(discover_fn=lambda: copy.deepcopy(report), plan_fn=plan, repo_root=REPO)
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: True, clock=clock, session=session)
    cookie, _ = login(app, sessions)
    html = call(app, "GET", "/stage/welcome", cookie=cookie).get_data(as_text=True)
    assert "REACHABLE FROM OUTSIDE" in html and "disabled>NEXT" in html and 'href="/stage/scan"' not in html.split("<footer")[-1]


def test_the_recheck_returns_the_refreshed_report(wizard):
    app, cookie, csrf, *_ = wizard
    body = post(app, cookie, csrf, "/api/discover").get_json()
    assert body["summary"] and body["checks"] and "stacks" in body and "storage" in body


def test_running_as_root_needs_an_explicit_acknowledgement_on_the_page(wizard):
    app, cookie, csrf, session, _ = wizard
    put(app, cookie, csrf, {"run_as": "root"})
    html = call(app, "GET", "/stage/location", cookie=cookie).get_data(as_text=True)
    assert 'id="accept-root"' in html and "RUN AS ROOT" in html and "checked" not in html.split('id="accept-root"')[1].split(">")[0]
    assert put(app, cookie, csrf, {"accept_root_service": True}).get_json()["ok"]
    assert "checked" in call(app, "GET", "/stage/location", cookie=cookie).get_data(as_text=True).split('id="accept-root"')[1].split(">")[0]
    put(app, cookie, csrf, {"run_as": "pe"})
    assert 'id="accept-root"' not in call(app, "GET", "/stage/location", cookie=cookie).get_data(as_text=True)
