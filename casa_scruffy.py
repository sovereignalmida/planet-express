"""Scruffy's dashboard: Airlock authentication, read-only snapshots, and core RPC."""

import hashlib
import hmac
import math
import os
import re
import secrets
import signal
import time
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    current_app,
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
from config_io import validate_config_text
from planet_express.execution.actions import LOG_CURSOR_HASH_LIMIT, LOG_TIMESTAMP_RE
from planet_express.integrations.rpc import RpcError, call


class DashboardSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure or app.config["DASHBOARD_HTTPS"]


class ConfigReloadWatch:
    """Reload the dashboard's workers when core has been handed a new config (T35).

    Config values (`paused_containers`, `backup_jobs`, ...) are module constants imported at
    worker start, and core cannot restart this separate, unprivileged service. So each request
    compares the config file's mtime with the last one seen; when the file no longer matches the
    bytes this worker loaded AND is valid, the worker sends SIGHUP to its gunicorn master once,
    which starts fresh workers (the app is not preloaded, so they re-import config) and retires
    the old ones gracefully. An invalid file is never followed: fresh workers would fail to import
    config and take the dashboard down (Codex review round 5, T35)."""

    def __init__(self, path, loaded_sha256, *, reload=None):
        self._path = path
        self._loaded = loaded_sha256
        self._reload = reload or (lambda: os.kill(os.getppid(), signal.SIGHUP))
        self._seen_mtime = None
        self._signalled = None

    def check(self) -> None:
        try:
            mtime = os.stat(self._path).st_mtime_ns
        except OSError:
            return
        if mtime == self._seen_mtime:
            return
        self._seen_mtime = mtime
        try:
            data = self._path.read_bytes()
        except OSError:
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest in (self._loaded, self._signalled):
            return
        try:
            _model, errors = validate_config_text(data.decode("utf-8"))
        except UnicodeError:
            return
        if errors:
            return
        self._signalled = digest
        self._reload()


def register_template_helpers(app) -> None:
    """Everything templates/ needs beyond Flask's own globals.

    One function because several tests render these templates against a bare Flask app, and
    a template that reaches for a helper the test app never registered fails as an
    UndefinedError at render time -- which looks like a template bug, not a missing fixture.
    """
    def asset(filename):
        """url_for('static') plus the file's mtime, so a browser that cached cockpit.css or
        dashboard.js cannot keep running them against freshly deployed HTML. Without it an
        operator sees new markup styled by the old stylesheet until they hard-refresh, which
        is indistinguishable from a broken release."""
        url = url_for("static", filename=filename)
        try:
            return f"{url}?v={int((Path(app.static_folder or '') / filename).stat().st_mtime)}"
        except OSError:
            return url

    app.jinja_env.globals["asset"] = asset
    app.add_template_filter(extract_host)
    app.add_template_filter(_extra_host_count, "extra_host_count")


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
        if not isinstance(result, (dict, list)):
            raise RpcError("Core unavailable")
        return result

    def is_chat_request():
        return request.path.startswith("/api/chat")

    def is_json_request():
        return (is_chat_request() or request.path.startswith("/api/containers/")
                or request.path.startswith("/api/approvals")
                or request.path.startswith("/api/executions/")
                or request.path.startswith("/api/incidents")
                or request.path.startswith("/api/config"))

    def check_csrf():
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            if is_json_request():
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

    register_template_helpers(app)

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
        if request.path.startswith("/api/config"):
            return jsonify(error="Config unavailable; try again shortly"), 503
        if is_chat_request():
            status, message = {"bad_request": (400, "Invalid chat request"),
                               "not_found": (404, "Chat ticket not found")}.get(
                                   error.code, (503, "Chat unavailable; try again shortly"))
            return jsonify(error=message), status
        if request.path.startswith("/api/containers/"):
            status, message = {"bad_request": (400, "Invalid container request"),
                               "not_found": (404, "Container not found")}.get(
                                   error.code, (503, "host slow, retry"))
            return jsonify(error=message), status
        if request.path.startswith("/api/approvals"):
            status, message = {"bad_request": (400, "Invalid approval request"),
                               "not_found": (404, "Approval not found")}.get(
                                   error.code, (503, "Approvals unavailable; try again shortly"))
            return jsonify(error=message), status
        if request.path.startswith("/api/executions/"):
            status, message = {"bad_request": (400, "Invalid execution request"),
                               "not_found": (404, "Execution not found")}.get(
                                   error.code, (503, "Execution unavailable; try again shortly"))
            return jsonify(error=message), status
        if request.path.startswith("/api/incidents"):
            status, message = {"bad_request": (400, "Invalid incident request"),
                               "not_found": (404, "Incident not found")}.get(
                                   error.code, (503, "Incidents unavailable; try again shortly"))
            return jsonify(error=message), status
        # Fail closed for this request only: keep the cookies, so a core restart doesn't
        # sign every operator out.
        return app.make_response((airlock("unavailable"), 503))

    config_watch = ConfigReloadWatch(config.CONFIG_FILE, config.CONFIG_SHA256)

    @app.before_request
    def follow_config_changes():
        # Only a gunicorn worker has a master to signal; never the test client or a dev server.
        if request.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
            config_watch.check()

    @app.before_request
    def authenticate():
        if request.method == "POST" and not is_json_request():
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
            if request.method == "POST" and is_json_request():
                error = check_csrf()
                if error is not None:
                    return error
            csrf_token()
            return None
        if is_json_request():
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

    @app.get("/api/config")
    def config_get():
        return jsonify(core("config.get", {}))

    @app.post("/api/config/validate")
    def config_validate():
        return jsonify(core("config.validate", {"text": request.form.get("text", "")}))

    @app.post("/api/config/apply")
    def config_apply():
        return jsonify(core("config.apply", {
            "text": request.form.get("text", ""),
            "base_sha256": request.form.get("base_sha256", ""),
            "operator": g.operator,
        }))

    def container_target(stack, service):
        if any(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) is None
               for value in (stack, service)):
            if is_json_request():
                raise RpcError("Container not found", "not_found")
            abort(404)
        return {"stack": stack, "service": service}

    @app.get("/containers/<stack>/<service>")
    def container_detail(stack, service):
        container_target(stack, service)
        return render_template("container.html", stack=stack, service=service, operator=g.operator)

    @app.get("/api/containers/<stack>/<service>")
    def container_get(stack, service):
        return jsonify(core("query.container", container_target(stack, service)))

    # POST, not GET: a cursor can carry up to LOG_CURSOR_HASH_LIMIT (1000) dedupe hashes, and ~180 of
    # them in a query string already exceed gunicorn's default 4094-byte request line — every later
    # poll would fail before reaching Flask and the log stream would freeze until reload (Codex
    # review, T30). A form body has no such limit and CSRF is already enforced on /api/containers/.
    @app.post("/api/containers/<stack>/<service>/logs")
    def container_logs(stack, service):
        params = container_target(stack, service)
        cursor = request.form.get("cursor")
        if cursor:
            if LOG_TIMESTAMP_RE.fullmatch(cursor) is None:
                return jsonify(error="Invalid cursor"), 400
            params["cursor"] = cursor
        hashes = request.form.getlist("hash")[:LOG_CURSOR_HASH_LIMIT]
        if any(re.fullmatch(r"[0-9a-f]{16}", value) is None for value in hashes):
            return jsonify(error="Invalid cursor hashes"), 400
        params["cursor_hashes"] = hashes
        return jsonify(core("logs.tail", params))

    @app.post("/api/containers/<stack>/<service>/restart")
    def container_restart(stack, service):
        return jsonify(core("action.request", {
            **container_target(stack, service), "action": "docker.restart_service", "operator": g.operator,
        }))

    def action_id(value, kind):
        if re.fullmatch(r"[0-9a-f]{12}", value) is None:
            raise RpcError(f"{kind} not found", "not_found")
        return value

    def approval_for_web(item):
        if item.get("status") == "denied":
            actor = item.get("decided_by") or "unknown"
            item = dict(item)
            item.setdefault("denial_reason", "Refused by current policy."
                            if actor == "policy" else f"Denied by {actor}.")
        return item

    @app.get("/api/approvals")
    def approvals_get():
        proposals = core("proposal.list_pending", {})
        # proposal.list_pending intentionally exposes a compact card shape. Fetch each full
        # approval so the browser receives declared capabilities instead of inferring them.
        pending = [approval_for_web(core("approval.get", {"approval_id": item["id"]}))
                   for item in proposals]
        recent = [approval_for_web(item)
                  for item in core("approval.list_recent", {"limit": 20})]
        recent_ids = {item["id"] for item in recent}
        pending = [item for item in pending
                   if item.get("status") == "pending" and item.get("id") not in recent_ids]
        return jsonify(pending=pending, recent=recent)

    @app.get("/api/approvals/<approval_id>")
    def approval_get(approval_id):
        return jsonify(approval_for_web(core("approval.get", {
            "approval_id": action_id(approval_id, "Approval"),
        })))

    @app.post("/api/approvals/<approval_id>/decide")
    def approval_decide(approval_id):
        approval_id = action_id(approval_id, "Approval")
        approve = request.form.get("approve")
        if approve not in {"0", "1"}:
            return jsonify(error="Invalid approval decision"), 400
        return jsonify(core("approval.decide", {
            "approval_id": approval_id, "approve": approve == "1", "decided_by": g.operator,
        }))

    @app.get("/api/executions/<execution_id>")
    def execution_get(execution_id):
        return jsonify(core("execution.get_status", {
            "execution_id": action_id(execution_id, "Execution"),
        }))

    @app.get("/api/incidents")
    def incidents_get():
        status = request.args.get("status", "open")
        raw_limit = request.args.get("limit", "20")
        if status not in {"open", "resolved", "all"} or re.fullmatch(r"[0-9]{1,3}", raw_limit) is None:
            return jsonify(error="Invalid incident request"), 400
        limit = int(raw_limit)
        if not 1 <= limit <= 100:
            return jsonify(error="Invalid incident request"), 400
        return jsonify(core("incident.list", {"status": status, "limit": limit}))

    @app.get("/api/incidents/<incident_id>")
    def incident_get(incident_id):
        if re.fullmatch(r"[0-9a-f]{12}", incident_id) is None:
            raise RpcError("Invalid incident ID", "bad_request")
        return jsonify(core("incident.get", {
            "incident_id": incident_id,
        }))

    @app.post("/api/incidents/<incident_id>/propose")
    def incident_propose(incident_id):
        if re.fullmatch(r"[0-9a-f]{12}", incident_id) is None:
            raise RpcError("Invalid incident ID", "bad_request")
        return jsonify(core("incident.propose", {
            "incident_id": incident_id, "operator": g.operator,
        }))

    @app.post("/api/executions/<execution_id>/abort")
    def execution_abort(execution_id):
        return jsonify(core("execution.abort", _execution_control(execution_id)))

    @app.post("/api/executions/<execution_id>/rollback")
    def execution_rollback(execution_id):
        return jsonify(core("execution.rollback", _execution_control(execution_id)))

    def _execution_control(execution_id):
        if re.fullmatch(r"[0-9a-f]{12}", execution_id) is None:
            raise RpcError("Invalid execution ID", "bad_request")
        return {"execution_id": execution_id, "operator": g.operator}

    @app.get("/executions/<execution_id>")
    def execution_page(execution_id):
        return render_template("execution.html", execution_id=execution_id)

    app.add_url_rule("/", "index", partial(index, core))
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


def index(core=None):
    # Live network pollers merged in separately from build_dashboard_context()'s
    # file-based state -- keeps the file-vs-live-HTTP boundary explicit here, in the
    # one route allowed to do this kind of I/O, rather than folding it into
    # dashboard_data.py's zero-I/O contract.
    ctx = dashboard_data.build_dashboard_context()
    if core is not None:
        try:
            # The open canary rollback windows live in the core's database, which this process is
            # denied (web_access.py); core hands them over instead (Codex, T42). Core being down
            # empties one panel, it never 500s the page.
            ctx["rollback_candidates"] = core("canary.candidates", {})
        except Exception:  # noqa: BLE001 -- see above
            current_app.logger.warning("Could not read the canary rollback windows from core")
        try:
            # The Hull tile's PLANS count. RunStatus.pending_plan_id was the shell planner's
            # signal and nothing writes it any more, so reading it would have reported "0 plans"
            # with approvals genuinely waiting. Same source as /api/approvals.
            ctx["pending_approvals"] = len(core("proposal.list_pending", {}) or [])
        except Exception:  # noqa: BLE001 -- see above
            current_app.logger.warning("Could not read pending approvals from core")
    ctx["traefik"] = casa_scruffy_net.fetch_traefik_routers()
    # Zone grouping, LAN-twin merging and provider tagging are string work over the router
    # rules. Doing it in Jinja would mean regexing Host() out of a rule in a template.
    ctx["router_zones"] = casa_scruffy_net.group_routers(ctx["traefik"]["routers"])
    ctx["adguard"] = casa_scruffy_net.fetch_adguard_stats()
    ctx["adguard_stats"] = dashboard_data.summarize_adguard(ctx["adguard"])
    ctx["overview_tiles"] = dashboard_data.summarize_overview_tiles(
        ctx, ctx["traefik"], ctx["adguard"]
    )
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
