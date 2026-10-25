#!/usr/bin/env python3
"""Console phase 0, E1 + E2 (throwaway spike, see docs/designs/v4-console-p0-spike.md).

A stand-in Messages API (no model, no cost) records every request the real `claude` CLI makes. It lets us check, deterministically:
  E1  a second PROCESS with --resume sends the first turn's messages again (context survives), with a different API key per turn
  E2  --output-format stream-json yields events live, and what they look like

    python3 scripts/spikes/console_p0_e1_e2.py --claude /usr/local/bin/claude
"""
import argparse, json, os, subprocess, sys, tempfile, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Fake:
    def __init__(self):
        self.requests = []          # (t, path, key header, body dict)
        self.replies = 0

    def reply_text(self):
        self.replies += 1
        return f"ack {self.replies}"


FAKE = Fake()


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        key = self.headers.get("x-api-key") or self.headers.get("authorization", "")
        FAKE.requests.append((time.time(), self.path, key, data))
        if "count_tokens" in self.path:
            out = json.dumps({"input_tokens": 10}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        text = FAKE.reply_text()
        model = data.get("model", "fake")
        usage = {"input_tokens": 10, "output_tokens": 5}
        if not data.get("stream"):
            out = json.dumps({"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                              "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                              "stop_sequence": None, "usage": usage}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()

        def chunk(b):
            self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n"); self.wfile.flush()
        chunk(sse("message_start", {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant",
                 "model": model, "content": [], "stop_reason": None, "usage": usage}}))
        chunk(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        for piece in (text[: len(text) // 2], text[len(text) // 2:]):
            time.sleep(0.4)           # visible gap between partial chunks: E2 measures arrival, not just content
            chunk(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}))
        chunk(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        chunk(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}))
        chunk(sse("message_stop", {"type": "message_stop"}))
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()


def run_turn(claude, base, env_dir, cwd, key, prompt, extra):
    env = {"PATH": os.environ["PATH"], "HOME": env_dir, "CLAUDE_CONFIG_DIR": os.path.join(env_dir, "cfg"), "LANG": "C.UTF-8",
           "ANTHROPIC_BASE_URL": base, "ANTHROPIC_API_KEY": key, "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    argv = [claude, "-p", prompt, "--max-turns", "1", "--disallowedTools", "WebFetch,WebSearch", *extra]
    t0 = time.time()
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = []
    for line in proc.stdout:
        lines.append((time.time() - t0, line.rstrip("\n")))
    err = proc.stderr.read()
    code = proc.wait(timeout=120)
    return code, lines, err


def messages_text(body):
    return json.dumps(body.get("messages", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default="claude")
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    home = tempfile.mkdtemp(prefix="pe-p0-home-"); work = tempfile.mkdtemp(prefix="pe-p0-ws-")
    sid = str(uuid.uuid4())
    results = {}

    # ---- E1: resume across processes, different key each turn
    FAKE.requests.clear()
    c1, l1, e1 = run_turn(args.claude, base, home, work, "access-code-ONE", "Remember the word PINEAPPLE. Say ok.",
                          ["--session-id", sid, "--output-format", "stream-json", "--verbose", "--include-partial-messages"])
    n1 = len(FAKE.requests)
    c2, l2, e2 = run_turn(args.claude, base, home, work, "access-code-TWO", "What word did I ask you to remember?",
                          ["--resume", sid, "--output-format", "stream-json", "--verbose"])
    turn2 = [b for (_, p, k, b) in FAKE.requests[n1:] if b.get("messages") and "count_tokens" not in p]
    keys_t1 = {k for (_, _, k, _) in FAKE.requests[:n1]}; keys_t2 = {k for (_, _, k, _) in FAKE.requests[n1:]}
    has_old = any("PINEAPPLE" in messages_text(b) for b in turn2)
    has_ack = any("ack 1" in messages_text(b) for b in turn2)
    results["E1"] = {"exit_turn1": c1, "exit_turn2": c2, "turn2_request_contains_turn1_user_msg": has_old,
                     "turn2_request_contains_turn1_assistant_reply": has_ack,
                     "keys_turn1": sorted(keys_t1), "keys_turn2": sorted(keys_t2),
                     "turn2_used_only_its_own_key": not (keys_t1 & keys_t2),
                     "stderr_turn2": e2.strip()[:300]}
    # a resume from a different working directory (what happens if the workspace moves)
    other = tempfile.mkdtemp(prefix="pe-p0-other-")
    n2 = len(FAKE.requests)
    c3, l3, e3 = run_turn(args.claude, base, home, other, "access-code-THREE", "again", ["--resume", sid])
    results["E1"]["resume_from_other_cwd_kept_context"] = any(
        "PINEAPPLE" in messages_text(b) for (_, p, k, b) in FAKE.requests[n2:] if "count_tokens" not in p)
    results["E1"]["resume_from_other_cwd_exit"] = c3
    results["E1"]["resume_from_other_cwd_stderr"] = e3.strip()[:200]

    # ---- E2: live events (turn 1's stream)
    kinds, first_text_t, last_t = [], None, 0.0
    parsed_ok = 0
    for t, line in l1:
        try:
            ev = json.loads(line); parsed_ok += 1
        except ValueError:
            kinds.append(("UNPARSEABLE", round(t, 2))); continue
        kind = ev.get("type", "?") + (":" + ev["subtype"] if ev.get("subtype") else "")
        if ev.get("type") == "stream_event":
            kind += ":" + ev.get("event", {}).get("type", "?")
        kinds.append((kind, round(t, 2)))
        last_t = t
    results["E2"] = {"lines": len(l1), "parsed_as_json": parsed_ok, "event_kinds_with_arrival_seconds": kinds[:40],
                     "session_id_in_stream": any(sid in line for _, line in l1)}
    print(json.dumps(results, indent=1))
    ok = (results["E1"]["turn2_request_contains_turn1_user_msg"] and results["E1"]["turn2_request_contains_turn1_assistant_reply"]
          and results["E1"]["turn2_used_only_its_own_key"] and results["E2"]["parsed_as_json"] == results["E2"]["lines"] > 0)
    print("E1/E2 mechanics:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
