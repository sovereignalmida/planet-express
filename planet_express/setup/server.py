"""The setup server: an HTTPS surface for a root process, so every request is checked before it is routed.

Design and the reasoning for each check: docs/designs/setup-server.md. In short, a request is served only if

* its peer address is private (RFC 1918, loopback, link-local, ULA),
* its `Host` is a name this server was started with (DNS rebinding),
* it carries the session cookie that the single-use start-up token was exchanged for,
* and, if it changes anything, its `Origin` is this server and its CSRF header matches the session.

Every refusal is the same 403 with the same body; the reason goes to the server log only, so a probe learns
nothing about which check it failed. Nothing in this module takes a path, a command or a step from a browser.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from planet_express.setup.session import Conflict

log = logging.getLogger("planet_express.setup.server")

COOKIE = "pe_setup"
TOKEN_LIFE = 15 * 60               # an unused token, and a session's idle life
HARD_CAP = 2 * 60 * 60             # a session never outlives this, however busy
EXPOSURE_TTL = 10                  # seconds a published exposure answer is reused


@dataclass
class Session:
    id: str
    csrf: str
    started: float
    expires: float


class Sessions:
    """One start-up token, exchanged once for one session. Held in memory only; compared in constant time."""

    def __init__(self, *, clock: Callable[[], float] = time.time, token_life: int = TOKEN_LIFE, hard_cap: int = HARD_CAP):
        self._clock, self._token_life, self._hard_cap = clock, token_life, hard_cap
        self._lock = threading.Lock()
        self._token_hash: bytes | None = None
        self._token_expires = 0.0
        self._session: Session | None = None
        self._session_hash: bytes | None = None

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._token_hash, self._token_expires = self._digest(token), self._clock() + self._token_life
        return token

    def exchange(self, token: str) -> Session | None:
        """The token for a session, once. A second use, a wrong token or a late one gets nothing."""
        with self._lock:
            ok = (self._token_hash is not None and self._clock() < self._token_expires
                  and hmac.compare_digest(self._digest(token), self._token_hash))
            if not ok:
                return None
            self._token_hash = None                                   # single use, whether or not a session exists
            now = self._clock()
            self._session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now + self._token_life)
            self._session_hash = self._digest(self._session.id)
            return self._session

    def lookup(self, cookie: str | None) -> Session | None:
        """The live session for this cookie, renewing its idle life up to the hard cap."""
        if not cookie:
            return None
        with self._lock:
            session = self._session
            if session is None or not hmac.compare_digest(self._digest(cookie), self._session_hash):
                return None
            now = self._clock()
            if now >= session.expires or now >= session.started + self._hard_cap:
                self._session = self._session_hash = None
                return None
            session.expires = min(now + self._token_life, session.started + self._hard_cap)
            return session

    def seconds_left(self) -> int:
        with self._lock:
            session = self._session
            if session is None:
                return max(0, int(self._token_expires - self._clock())) if self._token_hash else 0
            return max(0, int(min(session.expires, session.started + self._hard_cap) - self._clock()))

    def finished(self) -> bool:
        """True once the token has been used up or has lapsed and no session remains: `serve` should stop."""
        with self._lock:
            now = self._clock()
            if self._session is not None:
                return now >= self._session.expires or now >= self._session.started + self._hard_cap
            return self._token_hash is None or now >= self._token_expires


_PEER_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",      # RFC 1918, loopback, link-local
    "::1/128", "fc00::/7", "fe80::/10", "fec0::/10"))      # fec0 is deprecated site-local, still handed out by some virtual networks                                                    # and their IPv6 equivalents


def is_private_peer(address: str | None) -> bool:
    """RFC 1918, loopback, link-local or unique-local, and nothing else (not documentation ranges, not shared
    carrier space). An IPv4-mapped IPv6 address is judged as the IPv4 it carries."""
    try:
        ip = ipaddress.ip_address((address or "").split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in _PEER_NETWORKS)


def _host_name(header: str) -> str:
    """The host part of a Host header: no port, brackets stripped from an IPv6 literal, lower-cased."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class _Exposure:
    """`reachable_from_outside`, recomputed at most every EXPOSURE_TTL seconds."""

    def __init__(self, probe: Callable[[], bool], clock: Callable[[], float]):
        self._probe, self._clock, self._lock = probe, clock, threading.Lock()
        self._at = float("-inf")
        self._value = True                  # until proven otherwise: a failed probe must not read as "safe"

    def __call__(self, fresh: bool = False) -> bool:
        """`fresh` skips the cache: used before anything that changes the host, where ten seconds is too stale."""
        with self._lock:
            if fresh or self._clock() - self._at >= EXPOSURE_TTL:
                try:
                    self._value = bool(self._probe())
                except Exception:
                    log.exception("exposure probe failed; treating setup as exposed")
                    self._value = True
                self._at = self._clock()
            return self._value


_REPO = Path(__file__).resolve().parents[2]

# The ten stages of the wizard. `built` is False until the slice that implements it lands.
STAGES = [
    ("welcome", "Welcome", "fry", True), ("scan", "Scan", "leela", True), ("location", "Where it lives", "fry", True),
    ("powers", "What it may do", "fry", True), ("telegram", "Telegram", "fry", True),
    ("operator", "Operator account", "fry", True), ("llm", "LLM key", "fry", True),
    ("review", "Review the plan", "farnsworth", True), ("install", "Install", "bender", True),
    ("done", "Verify and done", "hermes", True)]

TIERS = [
    {"id": "observe", "title": "WATCH ONLY", "risk": "R0", "risk_class": "ok",
     "detail": "Looks at containers, disks and logs and tells you. Changes nothing.",
     "access": "Read access to Docker and your compose files."},
    {"id": "restart", "title": "RESTART CONTAINERS", "risk": "R1", "risk_class": "ok",
     "detail": "Can restart a container that has failed, and ask before anything else.",
     "access": "Docker control."},
    {"id": "stacks", "title": "MANAGE STACKS", "risk": "R2", "risk_class": "warn",
     "detail": "Can also update stacks with a canary check and prune safely.",
     "access": "Docker control and write access to compose files."},
    {"id": "full", "title": "FULL CREW", "risk": "R3", "risk_class": "warn",
     "detail": "Everything above plus host services you name, restarted through a narrow sudo grant.",
     "access": "Docker control, compose files and a sudoers grant for the units you list."}]


# Repair and uninstall act on what is already there, so the questions about powers and credentials are not asked.
HIDDEN_STAGES = {"repair": frozenset({"powers", "telegram", "operator", "llm"}),
                 "uninstall": frozenset({"powers", "telegram", "operator", "llm"})}


def _stage_views(story: str = "fresh"):
    hidden = HIDDEN_STAGES.get(story, frozenset())
    names = [stage[0] for stage in STAGES if stage[0] not in hidden]
    views = []
    for n, t, w, b in STAGES:
        if n in hidden:
            views.append({"index": -1, "name": n, "title": t, "who": w, "built": False, "prev": None, "next": None, "hidden": True})
            continue
        i = names.index(n)
        views.append({"index": i, "name": n, "title": t, "who": w, "built": b, "hidden": False,
                      "prev": names[i - 1] if i else None, "next": names[i + 1] if i + 1 < len(names) else None})
    return views


def create_app(*, sessions: Sessions, allowed_hosts: set[str], exposure: Callable[[], bool],
               clock: Callable[[], float] = time.time, session=None) -> Flask:
    app = Flask(__name__, static_folder=str(_REPO / "static"), template_folder=str(_REPO / "templates" / "setup"))
    allowed = {h.lower() for h in allowed_hosts}
    exposed = _Exposure(exposure, clock)

    def refuse(reason: str):
        # The reason is for the operator's terminal. The response is identical for every cause.
        log.warning("refused %s %s from %s: %s", request.method, request.path, request.remote_addr, reason)
        response = make_response(jsonify({"error": "forbidden"}), 403)
        return response

    @app.before_request
    def guard():
        if not is_private_peer(request.remote_addr):
            return refuse("peer is not on a private address")
        if _host_name(request.host or "") not in allowed:
            return refuse("unexpected Host header")
        if request.path == "/" and request.method == "GET" and "t" in request.args:
            session = sessions.exchange(request.args.get("t", ""))
            if session is None:
                return refuse("start-up token unknown, used or expired")
            response = redirect("/", code=303)
            response.set_cookie(COOKIE, session.id, httponly=True, secure=True, samesite="Strict", path="/")
            return response
        session = sessions.lookup(request.cookies.get(COOKIE))
        if session is None:
            return refuse("no valid session cookie")
        g.session = session
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin", "")
            if origin.lower() != f"https://{request.host}".lower():
                return refuse("Origin is missing or is not this server")
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf):
                return refuse("CSRF header is missing or wrong")
        return None

    @app.after_request
    def harden(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.get("/")
    def index():
        if session is not None:
            return redirect(url_for("stage", name="welcome"))
        return jsonify({"setup": "Planet Express", "state": "/api/state"})

    @app.get("/api/state")
    def state():
        return jsonify({"csrf": g.session.csrf, "seconds_left": sessions.seconds_left(),
                        "reachable_from_outside": exposed()})

    @app.post("/api/ping")
    def ping():
        return jsonify({"ok": True})

    if session is not None:
        _wizard_routes(app, sessions, session, exposed)
    return app


def _wizard_routes(app: Flask, sessions: Sessions, session, exposed) -> None:

    @app.get("/stage/<name>")
    def stage(name):
        views = _stage_views(session.answers.get("story", "fresh"))
        current = next((v for v in views if v["name"] == name), None)
        if current is None:
            abort(404)
        if not current["built"] or current["hidden"]:
            return redirect(url_for("stage", name="welcome"))
        if current["name"] == "install" and session.phase == "idle":
            return redirect(url_for("stage", name="review"))
        if current["name"] == "done" and session.phase != "done":
            return redirect(url_for("stage", name="install" if session.phase != "idle" else "review"))
        if session.discovery is None:
            session.run_discover()
        d, a = session.discovery, session.public_answers()
        left = sessions.seconds_left()
        is_exposed = exposed()
        blocked_next = is_exposed or (current["name"] == "scan" and not d["summary"]["can_continue"]) \
            or (current["name"] == "operator" and not a["operator_set"])
        host_addr = _host_name(request.host or "")
        return render_template("stage.html", stages=[v for v in views if not v["hidden"]], current=current, d=d, a=a, tiers=TIERS, csrf=g.session.csrf,
                               exposed=is_exposed, seconds_left=left, clock=f"{left // 60:02d}:{left % 60:02d}",
                               host_addr=host_addr, blocked_next=blocked_next, session=session)

    @app.post("/api/discover")
    def api_discover():
        found = session.run_discover()
        return jsonify({key: found.get(key) for key in ("summary", "checks", "storage", "stacks", "host", "docker")})

    @app.put("/api/answers")
    def api_answers():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "errors": [{"field": "", "message": "expected a JSON object"}]}), 400
        return jsonify(session.set_answers(body))

    @app.post("/api/plan")
    def api_plan():
        return jsonify(session.build_plan())

    def conflict(exc):
        return jsonify({"error": str(exc)}), 409

    @app.post("/api/apply")
    def api_apply():
        body = request.get_json(silent=True)
        plan_id = body.get("plan_id") if isinstance(body, dict) else None
        if not isinstance(plan_id, str):
            return jsonify({"error": "plan_id is required"}), 400
        if exposed(fresh=True):
            log.warning("apply refused: setup is reachable from outside")
            return jsonify({"error": "setup is reachable from outside your LAN, so nothing will be installed"}), 403
        try:
            session.approve(plan_id)
        except Conflict as exc:
            return conflict(exc)
        return jsonify({"started": True})

    @app.post("/api/retry")
    def api_retry():
        body = request.get_json(silent=True)
        step = body.get("step") if isinstance(body, dict) else None
        if exposed(fresh=True):
            return jsonify({"error": "setup is reachable from outside your LAN, so nothing will be installed"}), 403
        try:
            session.retry(step if isinstance(step, str) else None)
        except Conflict as exc:
            return conflict(exc)
        return jsonify({"started": True})

    @app.post("/api/undo")
    def api_undo():
        try:
            session.undo()
        except Conflict as exc:
            return conflict(exc)
        return jsonify({"started": True})

    def json_body():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    def text(body, key):
        value = body.get(key)
        return value if isinstance(value, str) else ""

    @app.post("/api/telegram/find-chat")
    def api_telegram_find():
        return jsonify(session.telegram_find(text(json_body(), "token")))

    @app.post("/api/telegram/test")
    def api_telegram_test():
        try:
            return jsonify(session.telegram_test())
        except Conflict as exc:
            return conflict(exc)

    @app.post("/api/llm/check")
    def api_llm_check():
        body = json_body()
        return jsonify(session.llm_check(text(body, "provider"), text(body, "api_key")))

    @app.post("/api/operator/totp")
    def api_operator_totp():
        return jsonify(session.totp_begin(text(json_body(), "name")))

    @app.post("/api/operator/verify")
    def api_operator_verify():
        body = json_body()
        try:
            return jsonify(session.totp_verify(text(body, "name"), text(body, "passphrase"), text(body, "code")))
        except Conflict as exc:
            return conflict(exc)

    @app.get("/api/events")
    def api_events():
        try:
            after = max(0, int(request.args.get("after", "0")))
        except ValueError:
            after = 0
        return jsonify(session.progress(after))


def _scrub(text: str) -> str:
    """Drop query strings from anything about to be logged: the start-up token travels in one."""
    return re.sub(r"\?\S*", "?…", text)


def _quiet_handler():
    from werkzeug.serving import WSGIRequestHandler

    class Handler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            log.info("%s %s %s", self.command, self.path.split("?")[0], code)

        def log_message(self, format, *args):
            log.warning(_scrub(format % args))
    return Handler


def network_exposed() -> bool:
    """True if this host has a globally routable address on a real interface: setup is meant for the LAN only."""
    from planet_express.setup.discover import _network
    from planet_express.setup.env import SystemEnv
    return bool(_network(SystemEnv())["public_addresses"])


def serve(*, addresses: list[str], port: int, names: list[str], sessions: Sessions,
          exposure: Callable[[], bool] = network_exposed, out: Callable[[str], None] = print,
          stop: threading.Event | None = None, session=None) -> int:
    """Run the server on exactly these private addresses (never a wildcard) until the session ends, the hard cap
    passes, `stop` is set or the operator interrupts. Prints the URL and the certificate fingerprint."""
    import os
    import shutil
    import tempfile

    from werkzeug.serving import make_server

    from planet_express.setup.tls import make_certificate

    bad = [a for a in addresses if not is_private_peer(a)]
    if not addresses or bad:
        out(f"refusing to listen on {', '.join(bad) or 'no address'}: setup is served only on private addresses.")
        return 2
    stop = stop or threading.Event()
    certificate = make_certificate(names, addresses)
    directory = tempfile.mkdtemp(prefix="pe-setup-")            # 0700, owned by this process
    servers = []
    try:
        cert_file, key_file = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
        for path, data in ((cert_file, certificate.cert_pem), (key_file, certificate.key_pem)):
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
        hosts = set(names) | set(addresses) | {"localhost"}
        app = create_app(sessions=sessions, allowed_hosts=hosts, exposure=exposure, session=session)
        for address in addresses:
            try:
                server = make_server(address, port, app, threaded=True, ssl_context=(cert_file, key_file),
                                     request_handler=_quiet_handler())
            except (OSError, SystemExit) as exc:        # werkzeug calls sys.exit when the address is in use
                out(f"cannot listen on {address}:{port}" + (f": {exc.strerror}" if isinstance(exc, OSError) and exc.strerror else ""))
                return 2
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()     # started now: shutdown() needs a loop
        token = sessions.issue_token()
        out("Planet Express setup is running. Open ONE of these in a browser on your LAN:")
        for address in addresses:
            shown = f"[{address}]" if ":" in address else address
            out(f"  https://{shown}:{port}/?t={token}")
        out(f"The page uses a certificate made just now, so the browser will warn once. Its SHA-256 fingerprint is\n"
            f"  {certificate.fingerprint}")
        out("The link works once and expires in 15 minutes. Press Ctrl-C here to stop setup at any time.")
        try:
            while not stop.is_set() and not sessions.finished():
                stop.wait(1.0)
        except KeyboardInterrupt:
            out("Stopping setup.")
        return 0
    finally:
        for server in servers:
            server.shutdown()
        shutil.rmtree(directory, ignore_errors=True)
