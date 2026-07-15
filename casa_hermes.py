"""
casa_hermes.py — Hermes: Issue Analyzer
"Sweet three-toed sloth of Ice Planet 2! That's a lot of findings to file."

Receives Leela's raw JSON snapshot, calls Claude to produce a prioritized,
structured findings list. Returns pure JSON — no prose, no padding.

Usage:
    python casa_hermes.py                        # reads STATE_MONITOR, writes STATE_FINDINGS
    python casa_hermes.py < monitor.json         # pipe mode
    cat monitor.json | python casa_hermes.py -   # explicit stdin
"""

import json
import logging
import sys
from datetime import datetime, timezone

import config
import casa_llm as llm

log = logging.getLogger("planetexpress.hermes")

# ── Hermes system prompt ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Hermes Conrad, chief bureaucrat and analyzer for the Planet Express home lab (CasaMediaServer).
You receive raw system snapshot JSON from Leela (the monitor) and output a structured findings list.
Be terse. Return ONLY valid JSON. No prose, no markdown fences, no explanations.

SEVERITY RULES (apply all that match):
- incomplete_stacks entries: a whole stack lost some or all of its containers — NOT the same
  as one container being down, it means containers were removed entirely (e.g. an interrupted
  teardown, or images pruned out from under already-gone containers). Leela has already
  computed the right severity in each entry's "alert" field (it has history this analysis
  step doesn't) — USE THAT VALUE AS-IS for the finding's severity, don't re-derive it from
  present_count/expected_count yourself. This must never be silently missed — a stack with
  zero containers produces zero "container not running" findings on its own, since there's
  nothing there to report as down.
- Container has crash_looping: true: ALWAYS HIGH regardless of image — a container repeatedly
  restarting is exactly as urgent whether it's Postgres or a plain app container. Do not
  downgrade these to MEDIUM.
- Container status "not running" with a clean exit (e.g. "Exited (0) ...") and crash_looping is
  NOT set: this is very likely an intentional one-shot/cron job (e.g. a notifier container that
  runs once and exits 0), not a failure. Do not create a finding for these unless the same
  container recurs as "not running" across consecutive reports with no scheduled reason.
- Container not running with a non-zero exit code (e.g. "Exited (1) ...") or unhealthy: HIGH if
  image contains postgres/redis/elasticsearch/mariadb/mysql/valkey/mongo, MEDIUM otherwise
- Disk used_pct >= 90: CRITICAL
- Disk used_pct 80-89: HIGH
- Missing SMB mount (missing list non-empty): HIGH — media services will be broken
- Backup job result != "success" or last_run is empty/n/a: HIGH
- systemd service inactive: MEDIUM (only casa-startup is actively checked as of 2026-07-04;
  nebula and dnclient were both decommissioned — remote access is now Tailscale on OPNsense,
  outside this host)
- Journal errors same message repeated > 3 times in the window: MEDIUM
- Stale :latest image (stale_days >= 30): LOW — batch into update_candidates
- Cert/acme errors (e.g. permission denied reading acme.json): this host has no active ACME
  resolver configured in Traefik — acme.json is known-dead legacy data, nothing reads or writes
  it. Do not raise this above LOW severity; note it as informational/cosmetic, not something
  needing action, unless the description text itself mentions certificates that are actually
  expiring or expired (a real expiry is always worth a real finding regardless of this rule).

Group related findings (e.g. postgres container + its dependent app = one finding).
Assign sequential IDs: f1, f2, f3...

OUTPUT SCHEMA — return exactly this structure, nothing else:
{
  "analyzed_at": "<ISO timestamp>",
  "findings": [
    {
      "id": "f1",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "category": "stack|container|disk|mount|backup|cert|service|image_update",
      "resource": "container or resource name",
      "description": "one concise sentence",
      "suggested_action": "one concise action sentence"
    }
  ],
  "has_critical": false,
  "has_high": false,
  "update_candidates": [
    {"container": "name", "image": "repo:tag", "stale_days": 45}
  ]
}

If no issues found, return findings as an empty array."""

MAX_TOKENS = 8192  # 4096 truncates mid-JSON with 7+ problem containers


def _slim_snapshot(snapshot: dict) -> dict:
    """Reduce snapshot to only what Hermes needs — keeps token count low.
    Healthy running containers are noise; only problems and summary matter."""
    containers = snapshot.get("containers", [])
    problem_containers = [c for c in containers if c.get("issue")]
    healthy_count = len(containers) - len(problem_containers)

    stack_completeness = snapshot.get("stack_completeness", [])
    incomplete_stacks = [s for s in stack_completeness if s.get("alert")]

    return {
        "timestamp": snapshot.get("timestamp"),
        "containers": {
            "total": len(containers),
            "healthy_running": healthy_count,
            "problems": problem_containers,  # only the ones with issues
        },
        # Only the stacks with something missing — a stack with all expected containers
        # present is not interesting to Hermes, same principle as healthy containers above.
        "incomplete_stacks": incomplete_stacks,
        "disk": snapshot.get("disk", []),
        "mounts": snapshot.get("mounts", {}),
        "system": {
            "memory_summary": snapshot.get("system", {}).get("memory_summary"),
            "uptime": snapshot.get("system", {}).get("uptime"),
            "recent_error_count": snapshot.get("system", {}).get("recent_error_count", 0),
            # skip full error list — too noisy, Hermes doesn't need raw journal lines
        },
        "backups": snapshot.get("backups", {}),
        "services": snapshot.get("services", {}),
        "image_candidates": snapshot.get("image_candidates", []),
        "certs": snapshot.get("certs", []),
    }


# ── Core analysis function ────────────────────────────────────────────────────
def analyze(snapshot: dict) -> dict:
    """Feed Leela's snapshot to the configured LLM, get structured findings back."""
    slimmed = _slim_snapshot(snapshot)
    snapshot_json = json.dumps(slimmed, separators=(",", ":"))

    log.info(
        f"Hermes filing report on snapshot from {snapshot.get('timestamp', '?')} "
        f"({slimmed['containers']['problems'].__len__()} problem container(s), "
        f"{slimmed['containers']['healthy_running']} healthy)"
    )

    raw = llm.complete(
        SYSTEM_PROMPT,
        f"Analyze this system snapshot and file your report:\n{snapshot_json}",
        MAX_TOKENS,
        tier="small",
    ).strip()

    # Strip markdown fences if the model disobeyed
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Hermes got invalid JSON from the LLM: {e}\nRaw:\n{raw[:500]}")
        # Return a safe fallback so the pipeline doesn't crash
        findings = {
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "findings": [{
                "id": "f1",
                "severity": "HIGH",
                "category": "service",
                "resource": "casa_hermes",
                "description": "Hermes failed to parse the LLM's analysis response",
                "suggested_action": "Check Hermes logs for raw LLM output",
            }],
            "has_critical": False,
            "has_high": True,
            "update_candidates": [],
            "_parse_error": str(e),
            "_raw_response": raw[:1000],
        }

    # Enrich with computed flags if the model skipped them
    if "findings" in findings:
        severities = {f["severity"] for f in findings["findings"]}
        findings["has_critical"] = findings.get("has_critical", "CRITICAL" in severities)
        findings["has_high"]     = findings.get("has_high",     "HIGH" in severities)

    findings.setdefault("analyzed_at", datetime.now(timezone.utc).isoformat())
    findings.setdefault("update_candidates", [])

    n = len(findings.get("findings", []))
    log.info(
        f"Hermes filed {n} finding(s). "
        f"Critical: {findings['has_critical']}, High: {findings['has_high']}"
    )
    return findings


# ── I/O helpers ───────────────────────────────────────────────────────────────
def load_snapshot(source: str = "state") -> dict:
    """Load snapshot from state file (default) or stdin ('-')."""
    if source == "-" or not sys.stdin.isatty():
        raw = sys.stdin.read()
        return json.loads(raw)
    config.ensure_dirs()
    if not config.STATE_MONITOR.exists():
        raise FileNotFoundError(
            f"No monitor snapshot found at {config.STATE_MONITOR}. "
            "Run casa_leela.py first."
        )
    return json.loads(config.STATE_MONITOR.read_text())


def save_findings(findings: dict) -> None:
    config.ensure_dirs()
    config.STATE_FINDINGS.write_text(json.dumps(findings, indent=2))
    log.info(f"Findings saved to {config.STATE_FINDINGS}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    source = sys.argv[1] if len(sys.argv) > 1 else "state"
    snapshot = load_snapshot(source)
    findings = analyze(snapshot)

    # Always save to state file when run standalone
    save_findings(findings)
    print(json.dumps(findings, indent=2))
