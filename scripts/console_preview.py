#!/usr/bin/env python3
"""Look at the Console page without a host: renders the real template and serves it with the real static files, and answers the JSON
endpoints from canned conversations (a plan waiting, a turn running, a turn that failed). No Core, no login, nothing is stored.

    .venv/bin/python scripts/console_preview.py --port 8765
    open http://127.0.0.1:8765/console/aaaaaaaaaaaaaaaa      (plan)   ...bbbb (running)   ...cccc (didn't finish)
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))

A, B, C = "a" * 16, "b" * 16, "c" * 16
STATUS = {"enabled": True, "open": 3, "max_open": 10, "token_budget": 150000, "min_turn_tokens": 20000, "proposal_only": False, "proposal_only_reason": None}
PLAN = {"known": ["The VPN gateway is healthy.", "Four containers share its network and are running."],
        "unsure": ["Whether the provider's port rotation or the start order caused it."],
        "options": [
            {"name": "Recreate the dependents", "recommended": True, "summary": "Recreate the four dependents in order so they rebind to the gateway.",
             "changes": ["4 services in the media stack"], "will_not_touch": ["the gateway", "ingress", "DNS", "any data volume"],
             "risk": {"level": "low", "text": "About one minute of downtime."}, "rolls_back_to": "the checkpoint taken just before", "effort": "about 2 minutes",
             "checks": [{"id": "a", "check": "container_state", "params": {"container": "qbittorrent"}}, {"id": "b", "check": "http", "params": {"url": "http://qbittorrent:8080/"}}]},
            {"name": "Restart the gateway only", "recommended": False, "summary": "Restart the gateway and wait for the port-forward.",
             "changes": ["gluetun"], "will_not_touch": ["the dependents"], "risk": {"level": "medium", "text": "The dependents may not recover on their own."},
             "rolls_back_to": "the checkpoint", "effort": "about 1 minute", "checks": [{"id": "g", "check": "container_state", "params": {"container": "gluetun"}}]}]}
AGENT = ("The VPN gateway container came back up **before its port-forward was assigned**, so the apps that join its network can't route.\n\n"
         "I looked at:\n- the gateway's health\n- the port-forward status\n- which containers share its network\n\nThe gateway reports `healthy`; four dependents are running but unreachable.")
EVENTS = [{"seq": 1, "at": 1, "turn": 1, "type": "call", "kind": "ran", "name": "Bash", "detail": "docker inspect gluetun --format '{{.State.Health.Status}}'"},
          {"seq": 2, "at": 1, "turn": 1, "type": "result", "name": "Bash", "excerpt": "healthy", "failed": False},
          {"seq": 3, "at": 1, "turn": 1, "type": "call", "kind": "read", "name": "Read", "detail": "/var/lib/planetexpress/briefs/media.md"},
          {"seq": 4, "at": 1, "turn": 1, "type": "result", "name": "Read", "excerpt": "stack media: 14 services", "failed": False},
          {"seq": 5, "at": 1, "turn": 1, "type": "denied", "name": "Bash", "detail": "docker restart qbittorrent", "why": "A conversation cannot change the host. Propose a plan instead."},
          {"seq": 6, "at": 1, "turn": 1, "type": "call", "kind": "changed", "name": "Write", "detail": "plan.json"}]


def feed(cid):
    base = {"budget": {"used_pct": 24, "tokens_used": 36000, "token_budget": 150000, "tokens_left": 114000, "state": "ok"}, "turn_starts": [1], "status": STATUS,
            "composer": {"mode": "idle", "reason": None}, "last_message": 9, "last_event": 6}
    ops = [{"seq": 1, "role": "operator", "turn": None, "at": 1, "text": "The download apps lost internet after the reboot. Figure out why.", "by": "chris"}]
    if cid == A:
        return {**base, "conversation": {"id": A, "title": "Download apps lost internet after the reboot", "origin": "operator", "state": "open", "turns": 1,
                "status": {"text": "Needs your call", "level": "warn", "group": "Needs you"},
                "verdict": {"h": "Waiting for your choice between 2 options", "p": "Recommended: Recreate the dependents. Nothing has been changed."}},
                "running": False, "messages": ops + [{"seq": 2, "role": "agent", "turn": 1, "at": 2, "text": AGENT, "by": None}], "events": EVENTS,
                "plan": {"id": 1, "turn": 1, "status": "pending", "created_at": 1, "hash": "9f3a1c22", "expired": False, "plan": PLAN}}
    if cid == B:
        return {**base, "conversation": {"id": B, "title": "Why is the media stack slow right now?", "origin": "incident", "state": "open", "turns": 1,
                "status": {"text": "Looking into it", "level": "ok", "group": "Working"}, "verdict": {"h": "Working on it", "p": "The agent is investigating. You can stop it at any time."}},
                "running": True, "composer": {"mode": "busy", "reason": "Working. Your message waits for this turn to finish, or press Stop to interrupt it."},
                "messages": [{**ops[0], "text": "Why is the media stack slow right now?"}], "events": EVENTS[:4] + [{"seq": 7, "at": 1, "turn": 1, "type": "delta", "text": "The disk latency on the NAS mount looks high; checking whether"}], "plan": None}
    return {**base, "conversation": {"id": C, "title": "Check the backups", "origin": "pe", "state": "open", "turns": 1,
            "status": {"text": "Didn't finish", "level": "high", "group": "Needs you"}, "verdict": {"h": "The last turn didn't finish", "p": "The agent stopped unexpectedly."}},
            "running": False, "messages": [{**ops[0], "text": "Check the backups"}, {"seq": 2, "role": "system", "turn": 1, "at": 2, "text": "The agent stopped unexpectedly.", "by": None}],
            "events": [], "plan": None}


def listing():
    rows = [(A, "Download apps lost internet after the reboot", "operator", "2m", "Needs your call", "warn", "Needs you"),
            (C, "Check the backups", "pe", "1h", "Didn't finish", "high", "Needs you"),
            (B, "Why is the media stack slow right now?", "incident", "10s", "Looking into it", "ok", "Working")]
    return {"conversations": [{"id": i, "title": t, "origin": o, "state": "open", "age": a, "turns": 1, "status": {"text": s, "level": lv, "group": g}} for i, t, o, a, s, lv, g in rows],
            "status": STATUS}


def page(selected):
    import casa_scruffy
    import config
    config.MISSIONS_ENABLED = True
    config.MISSIONS.console.enabled = True
    from test_scruffy_routes import ENV                   # the test operators: this server never serves a login, it only renders the template
    app = casa_scruffy.create_app(ENV, rpc_call=lambda m, p: {"ok": True, "result": {}})
    with app.test_request_context("/console"):
        from flask import render_template
        return render_template("console.html", ctx={"health": {"status": "ok", "last_scan": "now"}, "pipeline_status": {"state": "idle", "settling": None}},
                               page="console", state="ready", status=STATUS, selected=selected)


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, kind="application/json", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/static/"):
            f = ROOT / "static" / path[len("/static/"):]
            if f.is_file():
                return self._send(f.read_bytes(), {".css": "text/css", ".js": "text/javascript", ".png": "image/png"}.get(f.suffix, "application/octet-stream"))
        if path in ("/console",) or path.startswith("/console/"):
            return self._send(page(path.rsplit("/", 1)[-1] if path != "/console" else None), "text/html; charset=utf-8")
        if path == "/api/console/list":
            return self._send(json.dumps(listing()))
        if path.startswith("/api/console/") and path.endswith("/feed"):
            return self._send(json.dumps(feed(path.split("/")[3])))
        self._send("{}", code=404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(json.dumps({"id": A, "seq": 1, "stopped": True, "scrapped": True}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print(f"http://127.0.0.1:{args.port}/console/{A}   (plan)\nhttp://127.0.0.1:{args.port}/console/{B}   (running)\nhttp://127.0.0.1:{args.port}/console/{C}   (didn't finish)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
