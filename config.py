"""
config.py — CasaHefe shared configuration
Loads casa-sysadmin-context.yaml and provides typed helpers.
All agents import from here — keeps paths and credentials in one place.
"""

import os
import yaml
from pathlib import Path

# ── Directory paths (override via env vars for testing) ──────────────────────
CONTEXT_FILE = Path(os.environ.get(
    "CASA_CONTEXT", "/home/casaroot/casa-sysadmin-context.yaml"
))
STATE_DIR = Path(os.environ.get(
    "CASA_STATE_DIR", "/home/casaroot/apps/sysadmin-agent/state"
))
LOG_DIR = Path(os.environ.get(
    "CASA_LOG_DIR", "/home/casaroot/apps/sysadmin-agent/logs"
))

# State file names
STATE_MONITOR   = STATE_DIR / "latest_monitor.json"
STATE_FINDINGS  = STATE_DIR / "latest_findings.json"
STATE_PLAN      = STATE_DIR / "pending_plan.json"
STATE_STATUS    = STATE_DIR / "run_status.json"

# ── Stack discovery — single source of truth ─────────────────────────────────
# Every agent that needs "which stacks exist / which are off-limits" imports this,
# rather than keeping its own copy of the exclusion list. Drifting copies of this
# list across files is exactly how a stack could end up monitored in one place but
# not another.
STACKS_ROOT = Path("/home/casaroot/stacks")
FORBIDDEN_STACKS = ["clawbot", "ai"]  # never start, monitor, auto-update, or auto-prune around

# ── NAS mount inventory — single source of truth ──────────────────────────────
# Both casa_stackctl.check_mounts() (the /mounts command) and casa_leela.check_mounts()
# (the periodic monitor) test these the same way -- actually listing the path, not
# trusting systemd unit state, since a persistent CIFS mount can stay "active (mounted)"
# even after its session goes stale (e.g. surviving an Unraid reboot).
MOUNT_UNITS = {
    "casamedia.mount": "/casamedia",
    # casamediafast1tb decommissioned 2026-07-08 -- Unraid no longer exports fastmedia1tb
    # ("special device does not exist"), nothing referenced it (already commented out in
    # media/docker-compose.yml), same treatment as casamediafast2tb on 2026-07-07.
    "erugo.mount": "/erugo",
    "immichPhotos.mount": "/immichPhotos",
    "urphotos.mount": "/urphotos",
    "mnt-casabu.mount": "/mnt/casabu",
}

# Containers intentionally stopped by the user, pending a decision — don't flag as "not
# running" in Leela's monitor while paused. Remove an entry once its decision is made
# (either torn down for good or brought back up).
# - CASA_ADGUARD: stopped 2026-07-04, undecided between decommissioning it or wiring it into
#   OPNsense's Kea DHCPv4 as the LAN's DNS server. See project_casaserver_reip_plan memory.
PAUSED_CONTAINERS = ["CASA_ADGUARD"]


def active_stack_dirs() -> list[Path]:
    """Every stack directory with a live docker-compose.yml, minus forbidden ones."""
    return [
        p.parent for p in sorted(STACKS_ROOT.glob("*/docker-compose.yml"))
        if p.parent.name not in FORBIDDEN_STACKS
    ]

# ── Context loader ────────────────────────────────────────────────────────────
_ctx: dict | None = None

def ctx() -> dict:
    global _ctx
    if _ctx is None:
        if not CONTEXT_FILE.exists():
            raise FileNotFoundError(f"Context file not found: {CONTEXT_FILE}")
        with open(CONTEXT_FILE) as f:
            _ctx = yaml.safe_load(f)
    return _ctx

# ── Credential helpers ────────────────────────────────────────────────────────
def telegram_credentials() -> tuple[str, str]:
    """Return (TG_BOT_TOKEN, TG_CHAT_ID) for Planet Express's own dedicated bot
    (@casafarnsworthbot / "Casa Almida Planet Express" group), loaded from
    /etc/planetexpress.env via systemd's EnvironmentFile — same mechanism as
    ANTHROPIC_API_KEY below. Deliberately not shared with billarr's bot or
    ~/stacks/services/.env, which airbnb-notify also depends on."""
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError(
            "TG_BOT_TOKEN or TG_CHAT_ID not set. Add both to /etc/planetexpress.env."
        )
    return token, chat_id

def anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to /etc/planetexpress.env or export it."
        )
    return key

def openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Add it to /etc/planetexpress.env or export it."
        )
    return key

# ── LLM provider switch ────────────────────────────────────────────────────────
# Set LLM_PROVIDER=anthropic in /etc/planetexpress.env to switch back once Anthropic
# credits are available again — every call site (Hermes, Farnsworth, Amy) reads this,
# there's no per-file copy to forget. Defaults to "openai" (2026-07-13: Anthropic
# credit balance ran out, see project_casaserver_sysadmin_agents memory).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower()

# tier "small" = fast/cheap deterministic classification (Hermes' findings analysis,
# Farnsworth's action planning) — runs every pipeline cycle (every 6h).
# tier "large" = real synthesis + web research (Amy's failure diagnosis) — only runs
# when a normal remediation already failed, so cost stays bounded regardless of price.
MODELS = {
    "anthropic": {"small": "claude-haiku-4-5-20251001", "large": "claude-opus-4-8"},
    "openai": {"small": "gpt-5.4-nano", "large": "gpt-5.4-mini"},
}

def model_for(tier: str) -> str:
    if LLM_PROVIDER not in MODELS:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER {LLM_PROVIDER!r} — expected 'anthropic' or 'openai'."
        )
    return MODELS[LLM_PROVIDER][tier]

# ── Ensure runtime directories exist ─────────────────────────────────────────
def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
