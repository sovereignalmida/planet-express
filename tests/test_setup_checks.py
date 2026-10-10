"""Telegram and LLM checks, the TOTP enrolment, and the stages that use them (A4d)."""
import copy
import io
import json
import time
import urllib.error

import pytest
from test_setup_plan import KEY, PASS, TOKEN, systemd_report
from test_setup_server import Clock, call, login, post
from test_setup_stages import REPO, put

import web_auth
from planet_express.setup import checks
from planet_express.setup.checks import Found
from planet_express.setup.plan import plan
from planet_express.setup.server import Sessions, create_app
from planet_express.setup.session import SetupSession

# Assembled at runtime so secret scanners do not mistake this obviously fake test value for a real bot token.
BOT = "-".join(["123456789:fake", "token", "value", "for", "tests", "0001"])  # noqa: FLY002 -- joined at runtime on purpose


class Reply:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener(routes, seen=None):
    def open_(request, timeout=10):
        if seen is not None:
            seen.append(request)
        for fragment, answer in routes.items():
            if fragment in request.full_url:
                if isinstance(answer, Exception):
                    raise answer
                if answer[0] >= 400:
                    raise urllib.error.HTTPError(request.full_url, answer[0], "x", {}, io.BytesIO(json.dumps(answer[1]).encode()))
                return Reply(*answer)
        raise AssertionError(f"unexpected request {request.full_url}")
    return open_


# -- Telegram -------------------------------------------------------------------------------------------------
def test_a_token_that_is_not_shaped_like_one_never_leaves_the_machine():
    assert not checks.telegram_find_chat("hello", opener=opener({})).ok


def test_the_chat_that_last_wrote_to_the_bot_is_found():
    routes = {"getMe": (200, {"ok": True, "result": {"username": "pe_bot"}}),
              "getUpdates": (200, {"ok": True, "result": [{"message": {"chat": {"id": 111}}}, {"message": {"chat": {"id": 555004411}}}]})}
    found = checks.telegram_find_chat(BOT, opener=opener(routes))
    assert found.ok and found.chat_id == "555004411" and found.bot == "pe_bot"


@pytest.mark.parametrize("routes, expect", [
    ({"getMe": (401, {"ok": False})}, "rejected this token"),
    ({"getMe": (200, {"ok": True, "result": {"username": "b"}}), "getUpdates": (200, {"ok": True, "result": []})}, "No messages yet"),
    ({"getMe": (200, {"ok": True, "result": {"username": "b"}}), "getUpdates": (409, {})}, "webhook or another program"),
])
def test_each_failure_says_what_is_wrong(routes, expect):
    found = checks.telegram_find_chat(BOT, opener=opener(routes))
    assert not found.ok and expect in found.message


def test_a_network_error_never_contains_the_token():
    found = checks.telegram_find_chat(BOT, opener=opener({"getMe": urllib.error.URLError(f"failed for {BOT}")}))
    assert not found.ok and BOT not in found.message


def test_the_test_message_goes_to_that_chat_and_a_refusal_is_explained():
    seen = []
    ok, _ = checks.telegram_send_test(BOT, "555", opener=opener({"sendMessage": (200, {"ok": True})}, seen))
    assert ok and json.loads(seen[0].data)["chat_id"] == "555"
    ok, message = checks.telegram_send_test(BOT, "555", opener=opener({"sendMessage": (403, {})}))
    assert not ok and "Send the bot a message first" in message


# -- LLM ------------------------------------------------------------------------------------------------------
def test_the_llm_key_is_proven_by_listing_models_with_the_right_header():
    seen = []
    assert checks.check_llm_key("openai", KEY, opener=opener({"api.openai.com": (200, {})}, seen))[0]
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"
    assert checks.check_llm_key("anthropic", KEY, opener=opener({"api.anthropic.com": (200, {})}, seen))[0]
    assert seen[1].headers["X-api-key"] == KEY
    ok, message = checks.check_llm_key("openai", KEY, opener=opener({"openai": (401, {})}))
    assert not ok and "rejected" in message and KEY not in message
    ok, message = checks.check_llm_key("openai", KEY, opener=opener({"openai": urllib.error.URLError(f"boom {KEY}")}))
    assert not ok and KEY not in message
    assert not checks.check_llm_key("somewhere-else", KEY)[0]


# -- through the server ---------------------------------------------------------------------------------------
class FakeChecks:
    def __init__(self):
        self.found = Found(True, "Found the chat.", chat_id="555004411", bot="pe_bot")
        self.send_ok, self.llm_ok = True, True
        self.sent = []

    def telegram_find_chat(self, token):
        return self.found

    def telegram_send_test(self, token, chat_id):
        self.sent.append((token, chat_id))
        return (True, "Test message sent. Check your phone.") if self.send_ok else (False, "Telegram would not deliver")

    def check_llm_key(self, provider, key):
        return (True, "The key works.") if self.llm_ok else (False, "The provider rejected this key.")


@pytest.fixture
def rig():
    clock = Clock()
    clock.now = time.time()
    sessions, fake = Sessions(clock=clock), FakeChecks()
    session = SetupSession(discover_fn=lambda: copy.deepcopy(systemd_report()), plan_fn=plan, repo_root=REPO, checks=fake, clock=clock)
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=lambda: False, clock=clock, session=session)
    cookie, csrf = login(app, sessions)
    return app, cookie, csrf, session, fake, clock


def test_find_the_chat_then_send_the_test_and_only_then_is_telegram_saved(rig):
    app, cookie, csrf, session, fake, _ = rig
    found = post(app, cookie, csrf, "/api/telegram/find-chat", json={"token": TOKEN}).get_json()
    assert found["found"] and found["chat"] == "···4411" and "555004411" not in json.dumps(found) and TOKEN not in json.dumps(found)
    assert "telegram" not in session.answers                              # found is not verified
    verified = post(app, cookie, csrf, "/api/telegram/test").get_json()
    assert verified["verified"] and session.public_answers()["telegram_set"] is True
    assert fake.sent == [(TOKEN, "555004411")]


def test_a_failed_test_message_saves_nothing_and_the_test_needs_a_found_chat(rig):
    app, cookie, csrf, session, fake, _ = rig
    assert post(app, cookie, csrf, "/api/telegram/test").status_code == 409
    post(app, cookie, csrf, "/api/telegram/find-chat", json={"token": TOKEN})
    fake.send_ok = False
    assert post(app, cookie, csrf, "/api/telegram/test").get_json()["verified"] is False
    assert "telegram" not in session.answers


def test_skipping_telegram_is_an_explicit_null(rig):
    app, cookie, csrf, session, *_ = rig
    post(app, cookie, csrf, "/api/telegram/find-chat", json={"token": TOKEN})
    post(app, cookie, csrf, "/api/telegram/test")
    assert put(app, cookie, csrf, {"telegram": None}).get_json()["ok"] and "telegram" not in session.answers


def test_a_checked_llm_key_is_saved_masked_and_a_rejected_one_is_not(rig):
    app, cookie, csrf, session, fake, _ = rig
    fake.llm_ok = False
    assert post(app, cookie, csrf, "/api/llm/check", json={"provider": "openai", "api_key": KEY}).get_json()["ok"] is False
    assert "llm" not in session.answers
    fake.llm_ok = True
    reply = post(app, cookie, csrf, "/api/llm/check", json={"provider": "openai", "api_key": KEY})
    assert reply.get_json()["ok"] and KEY not in reply.get_data(as_text=True)
    assert session.public_answers()["llm"] == {"provider": "openai", "key_set": True}
    assert not post(app, cookie, csrf, "/api/llm/check", json={"provider": "evil", "api_key": KEY}).get_json()["ok"]


def code_for(session, clock):
    return web_auth.totp_at(session._pending_totp["secret"], int(clock() // 30))


def test_the_operator_must_prove_the_authenticator_works_before_the_account_is_kept(rig):
    app, cookie, csrf, session, _fake, clock = rig
    begin = post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"}).get_json()
    assert begin["ok"] and begin["qr"].startswith("data:image/png;base64,") and len(begin["manual"].replace(" ", "")) == 32
    assert "operator" not in session.answers
    wrong = post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": "000000"})
    assert wrong.get_json()["verified"] is False and "operator" not in session.answers
    good = post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": code_for(session, clock)})
    assert good.get_json()["verified"] and session.public_answers()["operator_set"] is True
    assert PASS not in good.get_data(as_text=True)


def test_a_weak_passphrase_is_refused_without_echoing_it(rig):
    app, cookie, csrf, session, _fake, clock = rig
    post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"})
    reply = post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": "short", "code": code_for(session, clock)})
    assert reply.get_json()["verified"] is False and "short" not in reply.get_data(as_text=True)
    assert "operator" not in session.answers


def test_too_many_wrong_codes_force_a_new_enrolment(rig):
    app, cookie, csrf, _session, *_ = rig
    post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"})
    for _ in range(5):
        post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": "000000"})
    blocked = post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": "000000"})
    assert blocked.status_code == 409 and "start the authenticator setup again" in blocked.get_json()["error"]
    assert post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"}).get_json()["ok"]      # a fresh secret resets it


def test_verifying_without_enrolling_first_or_under_another_name_is_refused(rig):
    app, cookie, csrf, _session, *_ = rig
    assert post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": "123456"}).status_code == 409
    post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"})
    assert post(app, cookie, csrf, "/api/operator/verify", json={"name": "mallory", "passphrase": PASS, "code": "123456"}).status_code == 409


def test_a_bad_operator_name_gets_no_secret(rig):
    app, cookie, csrf, *_ = rig
    assert post(app, cookie, csrf, "/api/operator/totp", json={"name": "Chris Smith!"}).get_json()["ok"] is False


def test_the_stages_render_and_next_waits_for_a_verified_operator(rig):
    app, cookie, csrf, session, _fake, clock = rig
    for name, marker in (("telegram", "FIND MY CHAT"), ("operator", "SET UP AUTHENTICATOR"), ("llm", "CHECK AND SAVE")):
        assert marker in call(app, "GET", f"/stage/{name}", cookie=cookie).get_data(as_text=True)
    assert "disabled>NEXT" in call(app, "GET", "/stage/operator", cookie=cookie).get_data(as_text=True)
    post(app, cookie, csrf, "/api/operator/totp", json={"name": "chris"})
    post(app, cookie, csrf, "/api/operator/verify", json={"name": "chris", "passphrase": PASS, "code": code_for(session, clock)})
    assert 'href="/stage/llm"' in call(app, "GET", "/stage/operator", cookie=cookie).get_data(as_text=True)


def test_the_csp_allows_the_qr_image_and_nothing_else_remote(rig):
    app, cookie, *_ = rig
    csp = call(app, "GET", "/stage/operator", cookie=cookie).headers["Content-Security-Policy"]
    assert "img-src 'self' data:" in csp and "default-src 'self'" in csp


def test_a_success_that_is_not_json_is_a_failed_check_not_a_crash():
    class Html(Reply):
        def read(self):
            return b"<html>Sign in to the hotel wifi</html>"
    bad = lambda request, timeout=10: Html(200, {})
    assert not checks.check_llm_key("openai", KEY, opener=bad)[0]
    assert not checks.telegram_find_chat(BOT, opener=bad).ok
    assert not checks.telegram_send_test(BOT, "1", opener=bad)[0]
    listy = lambda request, timeout=10: Reply(200, ["not", "an", "object"])
    assert not checks.check_llm_key("openai", KEY, opener=listy)[0]
