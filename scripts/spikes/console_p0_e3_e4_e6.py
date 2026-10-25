#!/usr/bin/env python3
"""Console phase 0, E3 (streaming input / interjection), E4 (kill mid-turn then resume), E6 (hooks survive resume).
Throwaway spike; stand-in model, no cost. Reuses the stand-in server of console_p0_e1_e2.py.

    python3 scripts/spikes/console_p0_e3_e4_e6.py --claude /usr/local/bin/claude
"""
import argparse, json, os, signal, subprocess, sys, tempfile, threading, time, uuid
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(__file__))
import console_p0_e1_e2 as base


def server_with(script):
    """`script(prompt_text, request_body) -> list of (delay_s, text_piece) or ('tool', name, input)`; default: one short reply."""
    h = base.Handler
    orig = h.do_POST

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.loads(body or b"{}")
        base.FAKE.requests.append((time.time(), self.path, self.headers.get("x-api-key", ""), data))
        if "count_tokens" in self.path:
            out = b'{"input_tokens": 10}'; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        last = ""
        for m in reversed(data.get("messages", [])):
            c = m.get("content")
            if m.get("role") == "user":
                last = c if isinstance(c, str) else json.dumps(c); break
        plan = script(last, data) or [(0.1, base.FAKE.reply_text())]
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        chunk = lambda b: (self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n"), self.wfile.flush())
        usage = {"input_tokens": 10, "output_tokens": 5}
        try:
            chunk(base.sse("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": data.get("model", "f"), "content": [], "stop_reason": None, "usage": usage}}))
            if plan and plan[0][0] == "tool":
                _, name, tin = plan[0]
                chunk(base.sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}}))
                chunk(base.sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(tin)}}))
                chunk(base.sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
                stop = "tool_use"
            else:
                chunk(base.sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
                for delay, piece in plan:
                    time.sleep(delay)
                    chunk(base.sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}))
                chunk(base.sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
                stop = "end_turn"
            chunk(base.sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}}))
            chunk(base.sse("message_stop", {"type": "message_stop"}))
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
    h.do_POST = do_POST
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, orig


def env_for(home, url, key="code"):
    return {"PATH": os.environ["PATH"], "HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(home, "cfg"), "LANG": "C.UTF-8",
            "ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": key, "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def e3(claude, url, home, work):
    """One long-lived process; second message sent while the first is still streaming."""
    base.FAKE.requests.clear()
    proc = subprocess.Popen([claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                             "--include-partial-messages", "--replay-user-messages", "--disallowedTools", "WebFetch,WebSearch"],
                            env=env_for(home, url), cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    events, t0 = [], time.time()

    def reader():
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type", "?") + (":" + ev["subtype"] if ev.get("subtype") else "")
            if ev.get("type") == "stream_event":
                kind += ":" + ev.get("event", {}).get("type", "?")
            events.append((round(time.time() - t0, 2), kind, line[:0]))
    threading.Thread(target=reader, daemon=True).start()
    proc.stdin.write(user_line("SLOW please count slowly")); proc.stdin.flush()
    time.sleep(1.2)                                   # the first reply is mid-stream (10 chunks, 0.5 s apart)
    proc.stdin.write(user_line("INTERJECT: change of plan, say STOP")); proc.stdin.flush()
    time.sleep(9)
    proc.stdin.write(user_line("third message after quiet")); proc.stdin.flush()
    time.sleep(3)
    proc.stdin.close()
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill(); code = "killed"
    posts = [b for (_, p, _, b) in base.FAKE.requests if "count_tokens" not in p and b.get("messages")]
    summary = []
    for b in posts:
        users = [m for m in b["messages"] if m.get("role") == "user"]
        texts = [(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]))[:40] for m in users]
        summary.append({"n_messages": len(b["messages"]), "user_texts": texts})
    results = [(t, k) for t, k, _ in events if k in ("result:success", "user", "assistant", "result:error") or k.startswith("result")]
    return {"exit": code, "model_requests": len(posts), "requests": summary, "result_and_assistant_events": results[:20]}


def e4(claude, url, home, work):
    """Kill the process at random points mid-turn, then --resume: is the context usable?"""
    out = []
    for delay in (0.3, 0.9, 1.6, 2.4):
        sid = str(uuid.uuid4())
        base.FAKE.requests.clear()
        p = subprocess.Popen([claude, "-p", "SLOW remember PINEAPPLE", "--session-id", sid, "--output-format", "stream-json", "--verbose",
                              "--disallowedTools", "WebFetch,WebSearch"], env=env_for(home, url), cwd=work,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(delay)
        os.killpg(p.pid, signal.SIGKILL); p.wait()
        n = len(base.FAKE.requests)
        r = subprocess.run([claude, "-p", "what word?", "--resume", sid, "--disallowedTools", "WebFetch,WebSearch"], env=env_for(home, url),
                           cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        later = [b for (_, pth, _, b) in base.FAKE.requests[n:] if "count_tokens" not in pth and b.get("messages")]
        out.append({"killed_after_s": delay, "resume_exit": r.returncode, "resume_made_request": bool(later),
                    "context_present": any("PINEAPPLE" in json.dumps(b["messages"]) for b in later),
                    "stderr": r.stderr.strip()[:160]})
    return out


def e6(claude, url, home, work):
    """A PreToolUse hook that denies Bash must still deny it on a resumed turn."""
    hook = os.path.join(home, "hook.py"); log = os.path.join(home, "hook.log")
    open(hook, "w").write("import json,sys\nd=json.load(sys.stdin)\nopen(%r,'a').write(d.get('tool_name','?')+'\\n')\n"
                          "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny' if d.get('tool_name')=='Bash' else 'allow','permissionDecisionReason':'spike'}}))\n" % log)
    settings = json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{sys.executable} {hook}"}]}]}})
    sid = str(uuid.uuid4())
    common = ["--settings", settings, "--output-format", "stream-json", "--verbose", "--max-turns", "3", "--permission-mode", "bypassPermissions"]
    res = {}
    for turn, flags in (("turn1", ["--session-id", sid]), ("turn2_resumed", ["--resume", sid]), ("turn3_resumed", ["--resume", sid])):
        base.FAKE.requests.clear()
        r = subprocess.run([claude, "-p", "USEBASH please run ls", *flags, *common], env=env_for(home, url), cwd=work,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        denied = "spike" in r.stdout or "denied" in r.stdout.lower() or "permission" in r.stdout.lower()
        res[turn] = {"exit": r.returncode, "stream_mentions_denial": denied}
    res["hook_log_tool_calls"] = open(log).read().split() if os.path.exists(log) else []
    return res


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--claude", default="claude"); ap.add_argument("--only", default="")
    args = ap.parse_args()

    def script(last, data):
        if "SLOW" in last:
            return [(0.5, f"chunk{i} ") for i in range(10)]
        if "USEBASH" in last and not any(m.get("role") == "user" and isinstance(m.get("content"), list) and any(c.get("type") == "tool_result" for c in m["content"]) for m in data["messages"][-1:]):
            return [("tool", "Bash", {"command": "ls", "description": "list"})]
        if "tool_result" in last:
            return [(0.1, "done")]
        return None
    srv, _ = server_with(script)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    home = tempfile.mkdtemp(prefix="pe-p0b-home-"); work = tempfile.mkdtemp(prefix="pe-p0b-ws-")
    out = {}
    for name, fn in (("E3_streaming_input", e3), ("E4_kill_then_resume", e4), ("E6_hooks_on_resume", e6)):
        if args.only and args.only not in name:
            continue
        try:
            out[name] = fn(args.claude, url, home, work)
        except Exception as exc:                        # a spike reports, it does not hide
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
