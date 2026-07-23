"""
config.py — Planet Express shared configuration
Loads and validates /etc/planetexpress/config.yaml (or $CASA_CONFIG) and provides typed
helpers. All agents import from here — keeps topology config, paths, and credentials in
one place.
"""

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from config_schema import (
    ExcludedService as ExcludedService,
    PlanetExpressConfig,
    SudoAllowlist as SudoAllowlist,
    SudoGlobGrant as SudoGlobGrant,
    SudoUnitGrant as SudoUnitGrant,
)

# ── Directory paths (override via env vars for testing, or per-install via the
# systemd unit's CASA_STATE_DIR/CASA_LOG_DIR) ─────────────────────────────────
# Defaults are repo-relative, not a hardcoded absolute path to one host's home
# directory — every real install (including this one) sets these explicitly via
# its systemd unit, so this default only matters as a fallback for someone
# running a script by hand without the env override.
STATE_DIR = Path(os.environ.get(
    "CASA_STATE_DIR", str(Path(__file__).parent / "state")
))
LOG_DIR = Path(os.environ.get(
    "CASA_LOG_DIR", str(Path(__file__).parent / "logs")
))

# State file names
STATE_MONITOR   = STATE_DIR / "latest_monitor.json"
STATE_FINDINGS  = STATE_DIR / "latest_findings.json"
STATE_PLAN      = STATE_DIR / "pending_plan.json"
STATE_STATUS    = STATE_DIR / "run_status.json"
# Single source of truth -- used to live as two independently-defined copies in
# casa_farnsworth.py and casa_zoidberg.py (same path, same drift risk as the old
# FORBIDDEN_STACKS duplication).
ROLLBACK_CANDIDATES_FILE = STATE_DIR / "rollback_candidates.json"
UPDATE_HISTORY_FILE      = STATE_DIR / "update_history.json"

# ── Topology config — single source of truth for per-install values ──────────
# Every agent that needs "which stacks exist / which are off-limits / which mounts and
# containers to track" imports the module-level constants below, rather than keeping a
# local copy. Drifting copies of this data across files is exactly how a stack could end
# up monitored in one place but not another (this already happened once — EXCLUDE_SERVICES
# used to live only in casa_zoidberg.py, see CHANGELOG).
CONFIG_FILE = Path(os.environ.get("CASA_CONFIG", "/etc/planetexpress/config.yaml"))


def _load_config() -> PlanetExpressConfig:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"Config file not found: {CONFIG_FILE}\n"
            f"Copy config.example.yaml to {CONFIG_FILE} and edit it for your environment "
            f"(or set CASA_CONFIG to point somewhere else). If you're upgrading an install "
            f"that predates this file, see scripts/migrate_config.py."
        )
    with open(CONFIG_FILE) as f:
        raw = yaml.safe_load(f) or {}
    try:
        return PlanetExpressConfig(**raw)
    except ValidationError as e:
        raise SystemExit(f"Invalid config at {CONFIG_FILE}:\n{e}")


_cfg = _load_config()

STACKS_ROOT = _cfg.stacks_root
FORBIDDEN_STACKS = _cfg.forbidden_stacks
PAUSED_CONTAINERS = _cfg.paused_containers
MOUNT_UNITS = _cfg.mounts
EXCLUDE_SERVICES: set[tuple[str, str]] = {(s.stack, s.service) for s in _cfg.exclude_services}
SUDO_ALLOWLIST = _cfg.sudo_allowlist
LAN_ONLY_DOMAIN = _cfg.lan_only_domain


def active_stack_dirs() -> list[Path]:
    """Every stack directory with a live docker-compose.yml, minus forbidden ones."""
    return [
        p.parent for p in sorted(STACKS_ROOT.glob("*/docker-compose.yml"))
        if p.parent.name not in FORBIDDEN_STACKS
    ]

# ── Credential helpers ────────────────────────────────────────────────────────
def telegram_credentials() -> tuple[str, str]:
    """Return (TG_BOT_TOKEN, TG_CHAT_ID) for Planet Express's own Telegram bot, loaded
    from /etc/planetexpress.env via systemd's EnvironmentFile — same mechanism as
    ANTHROPIC_API_KEY/OPENAI_API_KEY below."""
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

def adguard_credentials() -> tuple[str, str]:
    """Return (ADGUARD_USERNAME, ADGUARD_PASSWORD) for the dashboard's live AdGuard
    stats panel. Loaded from /etc/planetexpress-dashboard.env, NOT /etc/planetexpress.env
    -- casa-dashboard.service.template deliberately keeps the LLM API key / Telegram
    bot token out of this process's environment, so these two low-sensitivity,
    LAN-scoped values get their own optional env file rather than riding along in the
    main secrets file. Unlike telegram_credentials()/anthropic_api_key() above, this one
    is optional: it returns ("", "") on a missing value instead of raising, since the
    Network tab should degrade to "not configured" rather than take down dashboard
    rendering."""
    return os.environ.get("ADGUARD_USERNAME", ""), os.environ.get("ADGUARD_PASSWORD", "")

# ── LLM provider switch ────────────────────────────────────────────────────────
# Set LLM_PROVIDER=anthropic in /etc/planetexpress.env to switch providers — every call
# site (Hermes, Farnsworth, Amy) reads this, there's no per-file copy to forget.
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
