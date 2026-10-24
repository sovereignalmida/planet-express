#!/usr/bin/env python3
"""Smoke test for the model broker: the real `claude` CLI, through a real broker, to the real upstream.

  OPENROUTER_API_KEY=... python3 scripts/model_broker_smoke.py \\
      --claude /usr/bin/claude --model '~anthropic/claude-sonnet-latest' --haiku '~anthropic/claude-haiku-latest'

It asks the model for one word, then prints what happened: the CLI's answer, how many requests the broker relayed, the tokens it
counted, every status, and whether the real key appeared anywhere in the environment the CLI was given. It uses one short request
(a few cents at most). The key is read from the environment variable named by --key-env and is never printed.

Defaults are OpenRouter's (host openrouter.ai, path prefix /api, bearer auth); pass --host api.anthropic.com --prefix '' --auth x-api-key
for the vendor itself.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.missions.model_broker import BrokerLimits, ModelBroker  # noqa: E402


class _Store:
    """Just enough of a mission store for one session: it exists, it is running, it never expires."""

    def __init__(self):
        self.events = []

    def get_session(self, session_id):
        return {"mission_id": "M-smoke", "revoked_at": None, "expires_at": 10**10}

    def _now(self):
        return 1

    def get_mission(self, mission_id):
        class Mission:
            status = "executing"
        return Mission()

    def append_event(self, mission_id, kind, actor, body):
        self.events.append(body)


class _Session:
    session_id = "S-smoke"


def _files_holding(secret: str, *roots: str) -> list[str]:
    """Where, under these directories, the secret was written (names only, never content)."""
    hits = []
    needle = secret.encode()
    for root in roots:
        for base, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(base, name)
                try:
                    with open(path, "rb") as handle:
                        if needle in handle.read(2_000_000):
                            hits.append(path)
                except OSError:
                    continue
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude", default="claude", help="the claude CLI to run")
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY", help="the environment variable holding the real key")
    parser.add_argument("--host", default="openrouter.ai")
    parser.add_argument("--prefix", default="/api")
    parser.add_argument("--auth", choices=("bearer", "x-api-key"), default="bearer")
    parser.add_argument("--model", default="~anthropic/claude-sonnet-latest", help="the model the CLI is asked to use")
    parser.add_argument("--haiku", default="~anthropic/claude-haiku-latest", help="the model for the CLI's small internal requests")
    args = parser.parse_args()
    key = os.environ.get(args.key_env, "")
    if not key:
        print(f"{args.key_env} is not set in this environment: nothing to test with.", file=sys.stderr)
        return 2
    models = sorted({args.model, args.haiku})
    store = _Store()
    broker = ModelBroker(upstream_host=args.host, key_env=args.key_env, models=models, limits=BrokerLimits(max_requests=20),
                         store=store, path_prefix=args.prefix, auth_scheme=args.auth,
                         session_env={"ANTHROPIC_DEFAULT_SONNET_MODEL": args.model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": args.haiku,
                                      "ANTHROPIC_SMALL_FAST_MODEL": args.haiku})
    stop = broker.start(_Session())
    workspace, config = tempfile.mkdtemp(prefix="pe-smoke-ws-"), tempfile.mkdtemp(prefix="pe-smoke-cfg-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": workspace, "CLAUDE_CONFIG_DIR": config, "LANG": "C.UTF-8",
           **broker.env(_Session())}
    leaked = [name for name, value in env.items() if key in value]
    try:
        run = subprocess.run([args.claude, "-p", "Reply with the single word: ok", "--model", args.model, "--max-turns", "1",
                              "--disallowedTools", "WebFetch,WebSearch"],
                             env=env, capture_output=True, text=True, timeout=180, cwd=workspace, stdin=subprocess.DEVNULL)
    finally:
        usage = broker.usage("S-smoke")
        stop()
    def shown(text: str, limit: int) -> str:
        return text.strip().replace(key, "[REDACTED]")[:limit]
    leaked += [f"file:{path}" for path in _files_holding(key, workspace, config)]
    leaked += [stream for stream, text in (("stdout", run.stdout), ("stderr", run.stderr)) if key in text]
    print(f"claude exit code : {run.returncode}")
    print(f"claude said      : {shown(run.stdout, 200)!r}")
    if run.returncode:
        print(f"claude stderr    : {shown(run.stderr, 400)!r}")
    print(f"broker usage     : {usage}")
    for event in store.events:
        if event.get("event") in ("model.request", "model.refused"):
            shown = {k: event.get(k) for k in ("event", "model", "status", "input_tokens", "output_tokens", "reason") if k in event}
            print("  event          :", shown)
    print(f"key seen by the CLI: {'YES, LEAK in ' + ', '.join(leaked) if leaked else 'no (env, its files and its output checked)'}")
    ok = run.returncode == 0 and usage and usage["requests"] >= 1 and not leaked
    print("RESULT           :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
