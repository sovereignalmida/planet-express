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

from flask import Flask, render_template

import dashboard_data

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html", ctx=dashboard_data.build_dashboard_context())


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
