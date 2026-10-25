#!/usr/bin/env python3
"""Console phase 0, E3b: does stream-json input have an interrupt that stops a streaming reply mid-turn? (throwaway)"""
import argparse, json, os, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
import console_p0_e1_e2 as base
import console_p0_e3_e4_e6 as e


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--claude", default="claude"); args = ap.parse_args()
    srv, _ = e.server_with(lambda last, data: [(0.5, f"chunk{i} ") for i in range(14)] if "SLOW" in last else None)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    home, work = tempfile.mkdtemp(prefix="pe-p0c-home-"), tempfile.mkdtemp(prefix="pe-p0c-ws-")
    p = subprocess.Popen([args.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                          "--include-partial-messages", "--disallowedTools", "WebFetch,WebSearch"], env=e.env_for(home, url),
                         cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen, t0 = [], time.time()

    def reader():
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            seen.append((round(time.time() - t0, 2), ev.get("type"), ev.get("subtype") or (ev.get("event") or {}).get("type") or ev.get("response", {}).get("subtype", ""), json.dumps(ev)[:200] if ev.get("type") in ("control_response", "result") else ""))
    threading.Thread(target=reader, daemon=True).start()
    p.stdin.write(e.user_line("SLOW count")); p.stdin.flush()
    time.sleep(2.0)
    p.stdin.write(json.dumps({"type": "control_request", "request_id": "req_int_1", "request": {"subtype": "interrupt"}}) + "\n"); p.stdin.flush()
    time.sleep(3)
    sent_after = time.time() - t0
    p.stdin.write(e.user_line("after the interrupt: say hello")); p.stdin.flush()
    time.sleep(8)
    p.stdin.close()
    try:
        code = p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill(); code = "killed"
    posts = [b for (_, pth, _, b) in base.FAKE.requests if "count_tokens" not in pth and b.get("messages")]
    print(json.dumps({"exit": code, "interrupt_sent_at_s": 2.0, "events": [s for s in seen if s[1] in ("control_response", "result", "assistant", "user", "system")][:20],
                      "model_requests": len(posts), "last_request_user_texts": [(m["content"] if isinstance(m["content"], str) else "blocks")[:40] for m in posts[-1]["messages"] if m["role"] == "user"] if posts else []}, indent=1))


main()
