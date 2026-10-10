"""The repair and uninstall stories in the wizard: which stages show, what the plan says, and the page copy."""
import copy

import pytest
from test_setup_discover import ubuntu
from test_setup_plan import LIVE_FILES
from test_setup_server import Clock, call, login, post
from test_setup_stages import REPO, put

from planet_express.setup.discover import discover
from planet_express.setup.plan import plan
from planet_express.setup.server import Sessions, create_app
from planet_express.setup.session import SetupSession


def rig(files, dry_run=None):
    report = discover(ubuntu(files=files), repo_root="/home/pe/apps/pe")
    clock = Clock()
    sessions = Sessions(clock=clock)
    session = SetupSession(discover_fn=lambda: copy.deepcopy(report), plan_fn=plan, repo_root=REPO, dry_run_fn=dry_run)
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: False, clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    return app, cookie, csrf, session


def page(app, cookie, name):
    return call(app, "GET", f"/stage/{name}", cookie=cookie)


def test_repair_and_uninstall_are_offered_only_when_something_is_installed():
    app, cookie, *_ = rig(LIVE_FILES)
    html = page(app, cookie, "welcome").get_data(as_text=True)
    assert "REPAIR IT" in html and "UNINSTALL IT" in html
    app2, cookie2, *_ = rig({})
    html2 = page(app2, cookie2, "welcome").get_data(as_text=True)
    assert "REPAIR IT" not in html2 and "UNINSTALL IT" not in html2


@pytest.mark.parametrize("story", ["repair", "uninstall"])
def test_these_stories_skip_the_questions_about_powers_and_credentials(story):
    app, cookie, csrf, _session = rig(LIVE_FILES)
    assert put(app, cookie, csrf, {"story": story}).get_json()["ok"]
    for hidden in ("powers", "telegram", "operator", "llm"):
        assert page(app, cookie, hidden).headers["Location"].endswith("/stage/welcome")
    location = page(app, cookie, "location").get_data(as_text=True)
    assert 'href="/stage/review"' in location and 'href="/stage/powers"' not in location
    assert "Telegram" not in page(app, cookie, "welcome").get_data(as_text=True).split("<main")[0]      # not in the rail


def test_the_uninstall_plan_comes_back_through_the_browser_api_and_removes_only_integration():
    app, cookie, csrf, _session = rig(LIVE_FILES)
    put(app, cookie, csrf, {"story": "uninstall"})
    body = post(app, cookie, csrf, "/api/plan").get_json()
    assert body["story"] == "uninstall" and body["applicable"]
    kinds = {s["kind"] for s in body["steps"]}
    assert kinds == {"state.snapshot", "service.disable", "file.remove"}
    assert not any("config" in s["target"] or ".env" in s["target"] for s in body["steps"])
    assert page(app, cookie, "review").status_code == 200


def test_the_review_button_and_done_page_speak_the_story():
    app, cookie, csrf, session = rig(LIVE_FILES, dry_run=lambda p: [])
    put(app, cookie, csrf, {"story": "uninstall"})
    session.cannot_apply = None
    assert "APPROVE AND UNINSTALL" in page(app, cookie, "review").get_data(as_text=True)
    put(app, cookie, csrf, {"story": "repair"})
    assert "APPROVE AND REPAIR" in page(app, cookie, "review").get_data(as_text=True)


def test_repair_shows_what_would_change_by_asking_the_host_without_changing_it():
    seen = []

    def dry_run(reviewed):
        seen.append(reviewed)
        return [{"step": s.id, "check": "satisfied" if i % 2 else "would run", "detail": ""} for i, s in enumerate(reviewed.steps)]
    app, cookie, csrf, _session = rig(LIVE_FILES, dry_run=dry_run)
    put(app, cookie, csrf, {"story": "repair"})
    body = post(app, cookie, csrf, "/api/plan").get_json()
    assert seen and set(body["checks"]) == {s["id"] for s in body["steps"]}
    assert {c["check"] for c in body["checks"].values()} == {"satisfied", "would run"}
    # a normal install never carries a diff
    put(app, cookie, csrf, {"story": "adopt"})
    assert "checks" not in post(app, cookie, csrf, "/api/plan").get_json()


def test_a_dry_run_that_fails_still_gives_the_plan():
    def boom(reviewed):
        raise OSError("no")
    app, cookie, csrf, _session = rig(LIVE_FILES, dry_run=boom)
    put(app, cookie, csrf, {"story": "repair"})
    body = post(app, cookie, csrf, "/api/plan").get_json()
    assert body["steps"] and "checks" not in body
