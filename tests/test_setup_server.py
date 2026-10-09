"""The setup server's guard: every refusal, the token and cookie lifecycle, and a real TLS handshake (A4a)."""
import http.client
import re
import ssl
import threading
import time

import pytest

from planet_express.setup.server import COOKIE, Sessions, create_app, is_private_peer, serve
from planet_express.setup.tls import make_certificate

HOST = "192.168.1.50:8443"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sessions(clock):
    return Sessions(clock=clock)


@pytest.fixture
def exposure():
    return {"value": False}


@pytest.fixture
def app(sessions, clock, exposure):
    return create_app(sessions=sessions, allowed_hosts={"192.168.1.50", "pe.lan"}, exposure=lambda: exposure["value"],
                      clock=clock)


def call(app, method, path, *, host=HOST, peer="192.168.1.20", cookie=None, headers=None, **kw):
    client = app.test_client()
    if cookie:
        client.set_cookie(COOKIE, cookie, domain=host.split(":")[0].lower())
    base = {"REMOTE_ADDR": peer, "HTTP_HOST": host}
    return client.open(path, method=method, headers=headers or {}, environ_overrides=base, **kw)


def login(app, sessions, **kw):
    token = sessions.issue_token()
    response = call(app, "GET", f"/?t={token}", **kw)
    assert response.status_code == 303
    cookie = response.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    state = call(app, "GET", "/api/state", cookie=cookie)
    return cookie, state.get_json()["csrf"]


def post(app, cookie, csrf, path="/api/ping", origin=f"https://{HOST}", **kw):
    headers = {"X-CSRF-Token": csrf}
    if origin:
        headers["Origin"] = origin
    return call(app, "POST", path, cookie=cookie, headers=headers, **kw)


# -- the token and the cookie ------------------------------------------------------------------------------------
def test_the_token_is_exchanged_once_for_a_strict_httponly_secure_cookie_and_leaves_the_url(app, sessions):
    token = sessions.issue_token()
    response = call(app, "GET", f"/?t={token}")
    assert response.status_code == 303 and response.headers["Location"].endswith("/") and token not in response.headers["Location"]
    cookie = response.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Strict")) and token not in cookie


def test_a_used_token_cannot_be_exchanged_again(app, sessions):
    token = sessions.issue_token()
    assert call(app, "GET", f"/?t={token}").status_code == 303
    assert call(app, "GET", f"/?t={token}").status_code == 403


def test_a_wrong_or_missing_token_gets_nothing(app, sessions):
    sessions.issue_token()
    assert call(app, "GET", "/?t=nope").status_code == 403
    assert call(app, "GET", "/").status_code == 403


def test_an_unused_token_expires(app, sessions, clock):
    token = sessions.issue_token()
    clock.now += 15 * 60 + 1
    assert call(app, "GET", f"/?t={token}").status_code == 403


def test_a_session_idles_out_but_activity_renews_it_up_to_a_hard_cap(app, sessions, clock):
    cookie, _ = login(app, sessions)
    clock.now += 14 * 60
    assert call(app, "GET", "/api/state", cookie=cookie).status_code == 200      # renewed
    clock.now += 14 * 60
    assert call(app, "GET", "/api/state", cookie=cookie).status_code == 200
    for _ in range(20):                                                          # busy for hours
        clock.now += 14 * 60
        if call(app, "GET", "/api/state", cookie=cookie).status_code != 200:
            break
    assert call(app, "GET", "/api/state", cookie=cookie).status_code == 403
    assert clock.now - 1_000_000.0 <= 2 * 60 * 60 + 14 * 60                      # never past the cap by more than a step


def test_a_session_with_no_activity_expires(app, sessions, clock):
    cookie, _ = login(app, sessions)
    clock.now += 15 * 60 + 1
    assert call(app, "GET", "/api/state", cookie=cookie).status_code == 403


def test_the_countdown_reports_time_left(app, sessions, clock):
    cookie, _ = login(app, sessions)
    clock.now += 60
    assert 13 * 60 <= call(app, "GET", "/api/state", cookie=cookie).get_json()["seconds_left"] <= 15 * 60


# -- every refusal looks the same ---------------------------------------------------------------------------------
def test_each_refusal_is_the_same_403_so_a_probe_learns_nothing(app, sessions):
    cookie, csrf = login(app, sessions)
    cases = {
        "no cookie": call(app, "GET", "/api/state"),
        "wrong cookie": call(app, "GET", "/api/state", cookie="not-the-cookie"),
        "public peer": call(app, "GET", "/api/state", cookie=cookie, peer="8.8.8.8"),
        "wrong host": call(app, "GET", "/api/state", cookie=cookie, host="evil.example:8443"),
        "post without origin": post(app, cookie, csrf, origin=None),
        "post foreign origin": post(app, cookie, csrf, origin="https://evil.example"),
        "post http origin": post(app, cookie, csrf, origin=f"http://{HOST}"),
        "post no csrf": call(app, "POST", "/api/ping", cookie=cookie, headers={"Origin": f"https://{HOST}"}),
        "post wrong csrf": post(app, cookie, "wrong"),
    }
    bodies = {name: (r.status_code, r.get_data()) for name, r in cases.items()}
    assert all(code == 403 for code, _ in bodies.values()), bodies
    assert len({body for _, body in bodies.values()}) == 1, bodies


def test_a_good_post_works(app, sessions):
    cookie, csrf = login(app, sessions)
    assert post(app, cookie, csrf).status_code == 200


def test_put_and_delete_are_guarded_like_post(app, sessions):
    cookie, csrf = login(app, sessions)
    for method in ("PUT", "DELETE", "PATCH"):
        assert call(app, method, "/api/ping", cookie=cookie).status_code == 403


@pytest.mark.parametrize("peer, allowed", [
    ("192.168.1.20", True), ("10.0.0.5", True), ("172.20.1.1", True), ("127.0.0.1", True), ("169.254.1.1", True),
    ("fd00::1", True), ("fe80::1", True), ("::1", True), ("::ffff:192.168.1.20", True),
    ("8.8.8.8", False), ("1.1.1.1", False), ("100.64.0.1", False), ("192.0.2.1", False), ("2001:db8::1", False), ("::ffff:8.8.8.8", False), ("", False), ("junk", False)])
def test_peer_privacy(peer, allowed):
    assert is_private_peer(peer) is allowed


def test_host_header_forms(app, sessions):
    cookie, _ = login(app, sessions)
    assert call(app, "GET", "/api/state", cookie=cookie, host="pe.lan:8443").status_code == 200
    assert call(app, "GET", "/api/state", cookie=cookie, host="PE.LAN").status_code == 200
    assert call(app, "GET", "/api/state", cookie=cookie, host="pe.lan.evil.example").status_code == 403
    assert call(app, "GET", "/api/state", cookie=cookie, host="192.168.1.50.evil.example").status_code == 403


def test_responses_carry_the_hardening_headers(app, sessions):
    cookie, _ = login(app, sessions)
    headers = call(app, "GET", "/api/state", cookie=cookie).headers
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    assert call(app, "GET", "/api/state").headers["Cache-Control"] == "no-store"          # on refusals too


def test_the_exposure_flag_is_published_and_a_failing_probe_reads_as_exposed(sessions, clock):
    boom = {"raise": False}

    def probe():
        if boom["raise"]:
            raise RuntimeError("ip not found")
        return False
    app = create_app(sessions=sessions, allowed_hosts={"192.168.1.50"}, exposure=probe, clock=clock)
    cookie, _ = login(app, sessions)
    assert call(app, "GET", "/api/state", cookie=cookie).get_json()["reachable_from_outside"] is False
    clock.now += 11
    boom["raise"] = True
    assert call(app, "GET", "/api/state", cookie=cookie).get_json()["reachable_from_outside"] is True


def test_no_route_takes_a_path_command_or_step_from_the_browser(app):
    """The authority contract: in this slice the only mutating route is a no-op ping."""
    mutating = sorted(r.rule for r in app.url_map.iter_rules() if r.methods & {"POST", "PUT", "DELETE", "PATCH"})
    assert mutating == ["/api/ping"]


# -- a real TLS handshake ----------------------------------------------------------------------------------------
def test_serve_speaks_tls_with_the_fingerprint_it_prints_and_refuses_public_binds(clock):
    lines = []
    sessions = Sessions()
    stop = threading.Event()
    port = _free_port()
    thread = threading.Thread(target=serve, kwargs=dict(addresses=["127.0.0.1"], port=port, names=["testhost"],
                                                        sessions=sessions, exposure=lambda: False, out=lines.append, stop=stop))
    thread.start()
    try:
        for _ in range(100):
            if any("fingerprint" in line for line in lines):
                break
            time.sleep(0.05)
        text = "\n".join(lines)
        flat = [part.strip() for line in lines for part in line.splitlines()]
        url = next(line for line in flat if line.startswith("https://"))
        token = url.split("?t=")[1]
        printed = next(line for line in flat if re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", line))
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
        import hashlib
        connection.connect()
        der = connection.sock.getpeercert(binary_form=True)
        digest = hashlib.sha256(der).hexdigest().upper()
        assert ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)) == printed
        connection.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        assert response.status == 303 and "Secure" in response.getheader("Set-Cookie")
        assert token in text
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    refused = []
    assert serve(addresses=["8.8.8.8"], port=_free_port(), names=["x"], sessions=Sessions(), out=refused.append) == 2
    assert "private addresses" in refused[0]
    assert serve(addresses=[], port=_free_port(), names=["x"], sessions=Sessions(), out=refused.append) == 2


def test_the_certificate_covers_the_names_and_addresses_given():
    from cryptography import x509
    certificate = make_certificate(["pe.lan"], ["192.168.1.50"])
    cert = x509.load_pem_x509_certificate(certificate.cert_pem)
    alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "pe.lan" in alt.get_values_for_type(x509.DNSName) and any(str(a) == "192.168.1.50" for a in alt.get_values_for_type(x509.IPAddress))
    assert b"PRIVATE KEY" in certificate.key_pem and certificate.fingerprint.count(":") == 31


def _free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_start_up_token_never_reaches_the_log(caplog):
    import logging
    lines, stop, sessions, port = [], threading.Event(), Sessions(), _free_port()
    thread = threading.Thread(target=serve, kwargs=dict(addresses=["127.0.0.1"], port=port, names=["t"], sessions=sessions,
                                                        exposure=lambda: False, out=lines.append, stop=stop))
    with caplog.at_level(logging.DEBUG):
        thread.start()
        try:
            for _ in range(100):
                if any("https://" in line for line in lines):
                    break
                time.sleep(0.05)
            token = next(part for line in lines for part in line.split() if part.startswith("https://")).split("?t=")[1]
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
            connection.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            assert connection.getresponse().status == 303
        finally:
            stop.set()
            thread.join(timeout=10)
    assert token not in caplog.text


def test_a_failed_second_bind_returns_instead_of_hanging():
    import socket
    busy = socket.socket()
    busy.bind(("127.0.0.2", 0))
    busy.listen()
    port = busy.getsockname()[1]
    out = []
    try:
        done = []
        thread = threading.Thread(target=lambda: done.append(serve(addresses=["127.0.0.1", "127.0.0.2"], port=port, names=["t"],
                                                                   sessions=Sessions(), exposure=lambda: False, out=out.append)))
        thread.start()
        thread.join(timeout=15)
        assert not thread.is_alive() and done == [2] and "cannot listen" in out[0]
    finally:
        busy.close()
