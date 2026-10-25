#!/usr/bin/env python3
"""Console phase 0, E5 (plan schema reliability) and E7 (cost of a long conversation) against a REAL model, through the real broker.
Throwaway spike. The key is read from OPENROUTER_API_KEY, held only by the broker, never shown. A hard token cap ends the run.

    read -rs OPENROUTER_API_KEY; export OPENROUTER_API_KEY
    python3 scripts/spikes/console_p0_e5_e7.py --claude /usr/local/bin/claude --cap 600000
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))
from planet_express.missions.model_broker import BrokerLimits, ModelBroker          # noqa: E402
from planet_express.missions.verify.checks import PARAM_MODELS                       # noqa: E402
from planet_express.missions.verify.plan import PlanInvalid, parse_plan              # noqa: E402

FIXTURES = sorted((ROOT / "tests/fixtures/v4/incidents").glob("*.json"))
PLAN_KEYS = {"options", "known", "unsure"}
OPT_KEYS = {"name", "recommended", "summary", "changes", "will_not_touch", "risk", "rolls_back_to", "effort", "checks"}

SYSTEM = ("You are Planet Express's investigation agent for a Docker Compose homelab. You never change the host. You read evidence, "
          "reason, and propose plans. Be specific and honest about what is known and what is not.")


def catalogue_text():
    rows = []
    for kind, model in PARAM_MODELS.items():
        props = model.model_json_schema().get("properties", {})
        rows.append(f"  {kind}: " + ", ".join(f"{k}:{v.get('type', 'any')}" for k, v in props.items()))
    return "\n".join(rows)


def plan_prompt(fx, via_file):
    ev = "\n".join(f"[{e['source']}]\n{e['text']}" for e in fx["evidence"])
    shape = ('{"known":[str],"unsure":[str],"options":[{"name":str,"recommended":bool,"summary":str,"changes":[str],'
             '"will_not_touch":[str],"risk":{"level":"low|medium|high","text":str},"rolls_back_to":str,"effort":str,'
             '"checks":[{"id":str,"check":kind,"params":{...}}]}]}')
    out = ("Write your answer as plan.json in the current directory (use the Write tool), nothing else." if via_file
           else "Reply with only the JSON object.")
    return (f"Incident: {fx['name']}\n\nEvidence:\n{ev}\n\nInvestigate from this evidence and propose 1 to 3 options for the operator. "
            f"Exactly one option has recommended=true. Each option's `checks` are how PE will verify it worked and may ONLY use these check kinds "
            f"and parameters:\n{catalogue_text()}\n\nShape: {shape}\n{out}")


def validate(doc):
    """Our proposed plan contract. Returns a list of problems (empty = PE would accept it)."""
    if not isinstance(doc, dict) or set(doc) != PLAN_KEYS:
        return ["top-level keys"]
    problems = []
    opts = doc["options"]
    if not isinstance(opts, list) or not 1 <= len(opts) <= 3:
        return ["option count"]
    if sum(1 for o in opts if isinstance(o, dict) and o.get("recommended") is True) != 1:
        problems.append("exactly one recommended")
    for i, o in enumerate(opts):
        if not isinstance(o, dict) or set(o) != OPT_KEYS:
            problems.append(f"option {i} keys"); continue
        if not isinstance(o["risk"], dict) or o["risk"].get("level") not in ("low", "medium", "high"):
            problems.append(f"option {i} risk")
        try:
            parse_plan({"catalogue_v": 1, "checks": o["checks"]})
        except PlanInvalid as exc:
            problems.append(f"option {i} checks: {exc}")
    return problems


class Store:
    def __init__(self): self.events = []
    def get_session(self, s): return {"mission_id": "M-p0", "revoked_at": None, "expires_at": 10**10}
    def _now(self): return 1
    def get_mission(self, m):
        class M: status = "investigating"
        return M()
    def append_event(self, m, k, a, b): self.events.append(b)


class Sess: session_id = "S-p0"


def claude_env(broker, ws, cfg):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": ws, "CLAUDE_CONFIG_DIR": cfg, "LANG": "C.UTF-8", **broker.env(Sess()),
            "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def tokens(store):
    return sum(e.get("charged", 0) for e in store.events if e.get("event") == "model.request")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default="claude"); ap.add_argument("--cap", type=int, default=600_000)
    ap.add_argument("--model", default="~anthropic/claude-sonnet-latest"); ap.add_argument("--haiku", default="~anthropic/claude-haiku-latest")
    ap.add_argument("--file-runs", type=int, default=20); ap.add_argument("--schema-runs", type=int, default=9)
    ap.add_argument("--turns", type=int, default=10); ap.add_argument("--standin", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr); return 2
    store = Store()
    limits = BrokerLimits(max_requests=400, max_tokens=a.cap)
    broker_kw = dict(upstream_host="openrouter.ai", key_env="OPENROUTER_API_KEY", models=sorted({a.model, a.haiku}), limits=limits, store=store,
                     path_prefix="/api", auth_scheme="bearer",
                     session_env={"ANTHROPIC_DEFAULT_SONNET_MODEL": a.model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": a.haiku, "ANTHROPIC_SMALL_FAST_MODEL": a.haiku})
    broker = ModelBroker(**broker_kw)
    stop = broker.start(Sess())
    out = {"cap_tokens": a.cap}
    common = ["--model", a.model, "--system-prompt", SYSTEM, "--tools", "Write,Read", "--disallowedTools", "WebFetch,WebSearch,Bash", "--max-turns", "4"]
    try:
        # ---------------- E5 ----------------
        if a.only in ("", "e5"):
            res = {"file": {"runs": 0, "valid": 0, "problems": []}, "json_schema": {"runs": 0, "valid": 0, "problems": []}}
            for method, n in (("file", a.file_runs), ("json_schema", a.schema_runs)):
                for i in range(n):
                    if tokens(store) > a.cap * 0.55: break
                    fx = json.loads(FIXTURES[i % len(FIXTURES)].read_text())
                    ws, cfg = tempfile.mkdtemp(prefix="pe-p0-e5-"), tempfile.mkdtemp(prefix="pe-p0-cfg-")
                    argv = [a.claude, "-p", plan_prompt(fx, method == "file"), *common]
                    if method == "json_schema":
                        argv += ["--output-format", "json", "--json-schema", json.dumps({"type": "object"})]
                    try:
                        r = subprocess.run(argv, env=claude_env(broker, ws, cfg), cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240)
                    except subprocess.TimeoutExpired:
                        res[method]["runs"] += 1; res[method]["problems"].append("timeout"); continue
                    doc, probs = None, []
                    try:
                        if method == "file":
                            doc = json.loads(Path(ws, "plan.json").read_text())
                        else:
                            j = json.loads(r.stdout)
                            doc = j.get("structured_output") or json.loads(j.get("result", "{}"))
                    except Exception as exc:
                        probs = [f"unreadable: {type(exc).__name__}"]
                    probs = probs or validate(doc)
                    res[method]["runs"] += 1
                    if not probs: res[method]["valid"] += 1
                    else: res[method]["problems"].append(probs[:3])
                    shutil.rmtree(ws, ignore_errors=True); shutil.rmtree(cfg, ignore_errors=True)
            out["E5"] = res
        # ---------------- E7 ----------------
        if a.only in ("", "e7"):
            ws, cfg = tempfile.mkdtemp(prefix="pe-p0-e7-"), tempfile.mkdtemp(prefix="pe-p0-cfg-")
            fx = json.loads(FIXTURES[1 % len(FIXTURES)].read_text())
            Path(ws, "evidence.txt").write_text("\n".join(e["text"] for e in fx["evidence"]))
            sid = str(uuid.uuid4()); per_turn = []
            prompts = ["Read evidence.txt and tell me what you think is wrong.", "What would you check next, and why?", "Which service is most at risk?",
                       "Is a plain restart enough? Explain.", "What would a safe rollback look like?", "Summarise the situation in two sentences.",
                       "What do you not know yet?", "Draft the one-sentence verdict for a dashboard.", "What could make your diagnosis wrong?", "Give me a final recommendation."]
            for t in range(a.turns):
                if tokens(store) > a.cap * 0.95: out["E7_stopped"] = f"cap reached at turn {t}"; break
                before = tokens(store); t0 = time.time()
                flags = ["--session-id", sid] if t == 0 else ["--resume", sid]
                r = subprocess.run([a.claude, "-p", prompts[t % len(prompts)], *flags, *common], env=claude_env(broker, ws, cfg), cwd=ws,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240)
                reqs = [e for e in store.events if e.get("event") == "model.request"]
                per_turn.append({"turn": t + 1, "exit": r.returncode, "charged_tokens": tokens(store) - before, "seconds": round(time.time() - t0, 1)})
            out["E7"] = per_turn
    finally:
        usage = broker.usage("S-p0"); stop()
    ev = [e for e in store.events if e.get("event") == "model.request"]
    out["totals"] = {"requests": len(ev), "charged_tokens": tokens(store), "input_tokens": sum(e.get("input_tokens", 0) for e in ev),
                     "output_tokens": sum(e.get("output_tokens", 0) for e in ev), "broker_usage": usage}
    out["refusals"] = [{k: e.get(k) for k in ("model", "reason", "status")} for e in store.events if e.get("event") == "model.refused"][:5]
    text = json.dumps(out, indent=1)
    print(text.replace(key, "[REDACTED]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
