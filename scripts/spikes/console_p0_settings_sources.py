#!/usr/bin/env python3
"""Console phase 0: can a turn plant settings that govern the NEXT turn? A hook in the state dir's settings.json is the probe. (throwaway)"""
import json, os, subprocess, sys, tempfile, uuid
sys.path.insert(0, os.path.dirname(__file__))
import console_p0_e3_e4_e6 as e


def main():
    claude = sys.argv[1] if len(sys.argv) > 1 else "claude"
    def script(last, data):
        if "USEBASH" in last and not any(isinstance(m.get("content"), list) and any(c.get("type") == "tool_result" for c in m["content"]) for m in data["messages"][-1:]):
            return [("tool", "Bash", {"command": "true", "description": "t"})]
        return [(0.1, "done")]
    srv, _ = e.server_with(script)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    out = {}
    for label, flags in (("default", []), ("--setting-sources ''", ["--setting-sources", ""]), ("--setting-sources project", ["--setting-sources", "project"])):
        home = tempfile.mkdtemp(prefix="pe-p0s-"); work = tempfile.mkdtemp(prefix="pe-p0w-")
        cfg = os.path.join(home, "cfg"); os.makedirs(cfg)
        log = os.path.join(home, "planted.log")
        json.dump({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"echo planted >> {log}"}]}]},
                   "permissions": {"allow": ["Bash"]}}, open(os.path.join(cfg, "settings.json"), "w"))
        r = subprocess.run([claude, "-p", "USEBASH", "--max-turns", "3", "--output-format", "json", *flags], env=e.env_for(home, url), cwd=work,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        out[label] = {"planted_user_hook_ran": os.path.exists(log), "exit": r.returncode}
    print(json.dumps(out, indent=1))


main()
