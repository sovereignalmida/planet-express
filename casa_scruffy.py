"""Scruffy's dashboard: Airlock authentication, read-only snapshots, and core RPC."""

import hashlib
import hmac
import math
import os
import re
import secrets
import time
from functools import partial
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import BadData, URLSafeSerializer

import casa_scruffy_net
import config
import dashboard_data
import web_auth
from planet_express.integrations.rpc import RpcError, call


class DashboardSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure or app.config["DASHBOARD_HTTPS"]


def create_app(environ=None, *, rpc_call=None, clock=time.time) -> Flask:
    environ = os.environ if environ is None else environ
    secret = environ.get("PE_DASHBOARD_SECRET_KEY", "")
    if len(secret) < 32:
        raise SystemExit("PE_DASHBOARD_SECRET_KEY must contain at least 32 characters")
    try:
        operators = web_auth.load_operators(environ)
    except ValueError:
        raise SystemExit("Invalid PE_OPERATORS or PE_OPERATOR_* credential variables") from None
    if not operators:
        raise SystemExit("PE_OPERATORS must name at least one operator")
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict",
                      DASHBOARD_HTTPS=environ.get("CASA_DASHBOARD_HTTPS") == "1")
    app.session_interface = DashboardSessionInterface()
    rpc_call = rpc_call or partial(call, config.RPC_SOCKET)
    # Bind the trust flag to this exact token; a missing marker never extends its lifetime.
    trust_signer = URLSafeSerializer(secret, salt="pe-device-trust")
    epochs = {}

    def rpc(method, operator, **params):
        response = rpc_call("auth." + method, {"operator": operator, **params})
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RpcError("Authentication unavailable")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("ok") is False:
            raise RpcError("Authentication unavailable")
        return result

    def epoch(operator, *, fresh=False):
        now = clock()
        cached = epochs.get(operator)
        if not fresh and cached and 0 <= now - cached[1] < 60:
            return cached[0]
        value = rpc("device_epoch", operator).get("epoch")
        if type(value) is not int or value < 0:
            raise RpcError("Invalid device epoch")
        epochs[operator] = (value, now)
        return value

    def core(method, params):
        response = rpc_call(method, params)
        if not isinstance(response, dict):
            raise RpcError("Core unavailable")
        if response.get("ok") is not True:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else "internal"
            raise RpcError("Core request failed", code)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RpcError("Core unavailable")
        return result

    def is_chat_request():
        return request.path.startswith("/api/chat")

    def check_csrf():
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            if is_chat_request():
                return jsonify(error="Invalid CSRF token"), 400
            abort(400)

    def cookie_options():
        return {"httponly": True, "samesite": "Strict",
                "secure": request.is_secure or app.config["DASHBOARD_HTTPS"]}

    def clear_auth(response):
        for name in ("pe_auth", "pe_auth_trust"):
            response.delete_cookie(name, **cookie_options())
        return response

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    def airlock(state="default", status=None, note=None):
        status = status or {}
        seconds = max(0, math.ceil((status.get("locked_until") or clock()) - clock()))
        return render_template("login.html", state=state, seconds=seconds,
                               countdown=f"{seconds // 60:02d}:{seconds % 60:02d}",
                               remaining=status.get("remaining_attempts", 0), note=note,
                               domain=config.LAN_ONLY_DOMAIN, bot=config.telegram_bot_username(),
                               next_path=safe_next(request.values.get("next", "/")))

    @app.errorhandler(RpcError)
    def unavailable(error):
        if is_chat_request():
            status, message = {"bad_request": (400, "Invalid chat request"),
                               "not_found": (404, "Chat ticket not found")}.get(
                                   error.code, (503, "Chat unavailable; try again shortly"))
            return jsonify(error=message), status
        # Fail closed for this request only: keep the cookies, so a core restart doesn't
        # sign every operator out.
        return app.make_response((airlock("unavailable"), 503))

    @app.before_request
    def authenticate():
        if request.method == "POST" and not is_chat_request():
            check_csrf()
        public = ((request.path == "/login" and request.method in {"GET", "POST"})
                  or (request.path == "/login/notify" and request.method == "POST")
                  or (request.path == "/api/widget" and request.method == "GET")
                  or request.path.startswith("/static/"))
        if public:
            return None
        token = request.cookies.get("pe_auth", "")
        try:
            trust = trust_signer.loads(request.cookies.get("pe_auth_trust", ""))
        except BadData:
            trust = None
        valid_trust = (isinstance(trust, dict) and type(trust.get("t")) is bool
                       and trust.get("token") == hashlib.sha256(token.encode()).hexdigest())
        identity = web_auth.read_device_token(
            secret, token, clock(), max_age=30 * 86400 if valid_trust and trust["t"] else 12 * 3600
        ) if valid_trust else None
        if identity and identity[0] in operators and epoch(identity[0]) == identity[1]:
            g.operator = identity[0]
            if request.method == "POST" and is_chat_request():
                error = check_csrf()
                if error is not None:
                    return error
            csrf_token()
            return None
        if is_chat_request():
            return clear_auth(app.make_response((jsonify(error="Authentication required"), 401)))
        return clear_auth(redirect(url_for("login", next=request.path)))

    @app.after_request
    def private_response(response):
        if not request.path.startswith("/static/") and request.path != "/api/widget":
            response.headers["Cache-Control"] = "no-store"
        return response

    def failed(operator):
        status = rpc("record_failure", operator, client_ip=request.remote_addr)
        if status["locked"] and operator != "?":
            session["locked_operator"] = operator  # see the existing-lock branch in login()
        return airlock("locked" if status["locked"] else "rejected", status)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        csrf_token()
        if request.method == "GET":
            status = rpc("status", "?", client_ip=request.remote_addr)
            return airlock("locked" if status["locked"] else "default", status)
        status = rpc("status", "?", client_ip=request.remote_addr)
        if status["locked"]:
            return airlock("locked", status)
        matches = [op for op in operators.values() if web_auth.verify_passphrase(
            op.passphrase_hash, request.form.get("passphrase", ""))]
        if len(matches) != 1:
            return failed("?")
        op = matches[0]
        status = rpc("status", op.name, client_ip=request.remote_addr)
        if status["locked"]:
            # Signed session, set only after the passphrase matched: NOTIFY must ask core about
            # this operator's lock, which "?" + IP can't see (Codex review, T13b).
            session["locked_operator"] = op.name
            return airlock("locked", status)
        step = web_auth.verify_totp(op.totp_secret, request.form.get("code", ""), clock())
        if step is None or rpc("consume_totp_step", op.name, step=step).get("accepted") is not True:
            return failed(op.name)
        rpc("record_success", op.name, client_ip=request.remote_addr)
        token = web_auth.make_device_token(secret, op.name, epoch(op.name, fresh=True), clock())
        trusted = request.form.get("trust") == "on"
        marker = trust_signer.dumps({"t": trusted, "token": hashlib.sha256(token.encode()).hexdigest()})
        response = redirect(safe_next(request.form.get("next", "/")))
        for name, value in (("pe_auth", token), ("pe_auth_trust", marker)):
            response.set_cookie(name, value, max_age=30 * 86400 if trusted else None, **cookie_options())
        session.clear()
        csrf_token()
        return response

    @app.post("/login/notify")
    def notify():
        operator = session.get("locked_operator")
        operator = operator if operator in operators else "?"
        result = rpc("notify_locked", operator, client_ip=request.remote_addr)
        if result.get("reason") == "not_locked":
            session.pop("locked_operator", None)
            return redirect(url_for("login"))
        status = rpc("status", operator, client_ip=request.remote_addr)
        return airlock("locked", status, "Notification sent." if result.get("sent")
                       else "Notification already sent.")

    @app.post("/logout")
    def logout():
        session.clear()
        return clear_auth(redirect(url_for("login")))

    @app.post("/api/chat")
    def chat_ask():
        question = request.form.get("question", "").strip()
        submission_id = request.form.get("submission_id", "")
        if not 1 <= len(question) <= 2000 or re.fullmatch(r"[A-Za-z0-9_-]{8,64}", submission_id) is None:
            return jsonify(error="Invalid question or submission_id"), 400
        return jsonify(core("chat.ask", {"operator": g.operator, "question": question,
                                         "submission_id": submission_id}))

    @app.get("/api/chat/<ticket_id>")
    def chat_get(ticket_id):
        if re.fullmatch(r"[0-9a-f]{12}", ticket_id) is None:
            return jsonify(error="Chat ticket not found"), 404
        return jsonify(core("chat.get", {"operator": g.operator, "ticket_id": ticket_id}))

    @app.get("/api/chat/quota")
    def chat_quota():
        return jsonify(core("chat.quota", {}))

    app.add_template_filter(extract_host)
    app.add_template_filter(_extra_host_count, "extra_host_count")
    app.add_url_rule("/", view_func=index)
    app.add_url_rule("/api/widget", view_func=widget)
    return app


def safe_next(value):
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "/"
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else "/"


_HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")
_PATH_RULE_RE = re.compile(r"PathPrefix\(`([^`]+)`\)")


def extract_host(rule: str) -> str:
    """Pull the display host out of a Traefik router rule for the Network tab's
    routing-matrix cards -- purely a presentation shortener over a string that's
    already in ctx, not a new data field. Falls back to the path prefix, then the
    caller passes the router's own service name as the ultimate fallback via the
    template's `or` chain since a rule can combine rules with no Host()/PathPrefix()
    at all (e.g. a pure Method() or Headers() match)."""
    m = _HOST_RULE_RE.search(rule or "")
    if m:
        return m.group(1)
    m = _PATH_RULE_RE.search(rule or "")
    if m:
        return m.group(1)
    return ""


def _extra_host_count(rule: str) -> int:
    return max(len(_HOST_RULE_RE.findall(rule or "")) - 1, 0)


def index():
    # Live network pollers merged in separately from build_dashboard_context()'s
    # file-based state -- keeps the file-vs-live-HTTP boundary explicit here, in the
    # one route allowed to do this kind of I/O, rather than folding it into
    # dashboard_data.py's zero-I/O contract.
    ctx = dashboard_data.build_dashboard_context()
    ctx["traefik"] = casa_scruffy_net.fetch_traefik_routers()
    ctx["adguard"] = casa_scruffy_net.fetch_adguard_stats()
    ctx["telegram_bot_username"] = config.telegram_bot_username()
    ctx["professor_lines"] = dashboard_data.build_professor_lines(ctx)
    return render_template("dashboard.html", ctx=ctx)


def widget():
    return jsonify(dashboard_data.summarize_health())


def main() -> None:
    # Falls back to the default on an empty/invalid value, not just a missing one --
    # a systemd unit rendered without a real port (e.g. DASHBOARD_PORT substituted as
    # "") would otherwise set CASA_DASHBOARD_PORT="" and crash int("") at startup.
    try:
        port = int(os.environ.get("CASA_DASHBOARD_PORT", "8420") or "8420")
    except ValueError:
        port = 8420
    # debug=False is a deliberate, explicit choice, not Flask's implicit default --
    # Werkzeug's interactive debugger is a known RCE vector once reachable off
    # localhost, and this process binds 0.0.0.0.
    create_app().run(host="0.0.0.0", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
