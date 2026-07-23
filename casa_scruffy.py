"""
casa_scruffy.py — Planet Express's read-only web dashboard ("Scruffy the janitor:
observes and does nothing"). Separate process/systemd unit from casa_farnsworth.py
(the security/execution-adjacent daemon) -- a bug here can never touch anything that
executes a command. No auth, LAN-trust model, matching how Homepage itself is exposed.

The only file in this repo that imports Flask -- all data loading/summarization lives
in dashboard_data.py, which has zero Flask dependency and is what a future
Homepage-widget JSON route (Spec 7) would import directly.
"""

import os

from flask import Flask, jsonify, render_template

import casa_scruffy_net
import config
import dashboard_data

app = Flask(__name__)


@app.route("/")
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


@app.route("/api/widget")
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
    # localhost, and this process binds 0.0.0.0 with no auth.
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
