#!/usr/bin/env bash
# deploy.sh — Planet Express deployment script
#
#   git clone https://github.com/sovereignalmida/planet-express.git
#   cd planet-express
#   bash deploy.sh
#
# Must be run from inside the cloned repo (it resolves its own install path from its
# location on disk) -- piping this script over stdin (e.g. `ssh host 'bash -s' <
# deploy.sh`) won't work, since a piped script has no real path to resolve. SSH in and
# clone it there instead.
#
# See INSTALL.md for the full walkthrough (prerequisites, what each step does, how to
# get a Telegram bot token/chat id).

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(whoami)"
RUN_GROUP="$(id -gn "$RUN_USER")"
ENV_FILE="/etc/planetexpress.env"
SERVICE_FILE="/etc/systemd/system/casa-planetexpress.service"
STACKS_SERVICE_FILE="/etc/systemd/system/casa-stacks.service"
PYTHON_BIN="python3"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${GREEN}══ $* ══${NC}"; }

# ── Preflight ──────────────────────────────────────────────────────────────────
section "Preflight checks"

command -v docker > /dev/null              || error "docker not found"
docker compose version > /dev/null 2>&1    || error "docker compose plugin not found (need 'docker compose', not just 'docker-compose')"
command -v $PYTHON_BIN > /dev/null         || error "python3 not found"

info "Preflight OK"

# ── Directory setup ────────────────────────────────────────────────────────────
section "Creating runtime directories"

mkdir -p "$INSTALL_DIR/state"
mkdir -p "$INSTALL_DIR/logs"
info "Directories created"

# ── Python virtual environment ─────────────────────────────────────────────────
section "Setting up Python virtualenv"

cd "$INSTALL_DIR"

if [[ ! -d venv ]]; then
    info "Creating virtualenv..."
    $PYTHON_BIN -m venv venv
fi

info "Installing/upgrading dependencies..."
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
info "Python environment ready"

# ── Topology configuration (config.yaml + sudoers.d) ───────────────────────────
section "Configuration wizard"

read -rp "  Path for config.yaml [/etc/planetexpress/config.yaml]: " config_path
export CASA_CONFIG="${config_path:-/etc/planetexpress/config.yaml}"

if [[ -f "$CASA_CONFIG" ]]; then
    info "Config already exists at $CASA_CONFIG — skipping wizard. Delete it first if you want to reconfigure."
else
    venv/bin/python scripts/setup_wizard.py
fi

# ── Secrets (LLM provider/key + Telegram bot) ───────────────────────────────────
section "Secrets setup"

if [[ ! -f "$ENV_FILE" ]]; then
    warn "Environment file not found at $ENV_FILE"
    echo ""

    llm_provider=""
    while [[ "$llm_provider" != "openai" && "$llm_provider" != "anthropic" ]]; do
        read -rp "  LLM provider, 'openai' or 'anthropic' [openai]: " llm_provider
        llm_provider="${llm_provider:-openai}"
        if [[ "$llm_provider" != "openai" && "$llm_provider" != "anthropic" ]]; then
            warn "Must be exactly 'openai' or 'anthropic' (config.py rejects anything else at runtime)."
        fi
    done
    if [[ "$llm_provider" == "anthropic" ]]; then
        read -rp "  Enter your ANTHROPIC_API_KEY (or press Enter to set it manually later): " api_key
        api_key_line="ANTHROPIC_API_KEY=$api_key"
    else
        read -rp "  Enter your OPENAI_API_KEY (or press Enter to set it manually later): " api_key
        api_key_line="OPENAI_API_KEY=$api_key"
    fi

    read -rp "  Telegram bot token (from @BotFather, see INSTALL.md): " tg_token
    read -rp "  Telegram chat id (see INSTALL.md for how to find it): " tg_chat_id

    {
        echo "LLM_PROVIDER=$llm_provider"
        [[ -n "$api_key" ]] && echo "$api_key_line"
        [[ -n "$tg_token" ]] && echo "TG_BOT_TOKEN=$tg_token"
        [[ -n "$tg_chat_id" ]] && echo "TG_CHAT_ID=$tg_chat_id"
    } | sudo tee "$ENV_FILE" > /dev/null
    sudo chmod 600 "$ENV_FILE"
    info "Secrets saved to $ENV_FILE"

    if [[ -z "$api_key" || -z "$tg_token" || -z "$tg_chat_id" ]]; then
        warn "One or more values were left blank. Planet Express will fail to start until"
        warn "all of LLM_PROVIDER, ${api_key_line%%=*}, TG_BOT_TOKEN, TG_CHAT_ID are set in $ENV_FILE."
    fi
else
    info "Environment file already exists at $ENV_FILE"
fi

# ── Systemd units ────────────────────────────────────────────────────────────────
section "Installing systemd units"

render_unit() {
    # $1 = template path, $2 = destination path
    # Uses scripts/render_template.py (string.Template), not sed -- sed substitution
    # corrupts values containing '&'/'\'/the delimiter itself, and an independent Codex
    # review also flagged that unquoted systemd directives would silently break on a
    # path containing a space. render_template.py rejects both cases with a clear error
    # instead of producing a unit file that fails to start.
    venv/bin/python scripts/render_template.py "$1" "$INSTALL_DIR" "$RUN_USER" "$RUN_GROUP" "$CASA_CONFIG" \
        | sudo tee "$2" > /dev/null
}

render_unit "$INSTALL_DIR/systemd/casa-planetexpress.service.template" "$SERVICE_FILE"
render_unit "$INSTALL_DIR/systemd/casa-stacks.service.template" "$STACKS_SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable casa-stacks > /dev/null
info "Systemd units installed and casa-stacks enabled for boot (casa-planetexpress, casa-stacks)"
info "casa-stacks.service brings up your compose stacks at boot; casa-planetexpress.service"
info "is the always-on agent. If your stacks need network mounts ready first, see"
info "systemd/examples/casa-mounts.service.example."

# ── Smoke test ─────────────────────────────────────────────────────────────────
section "Smoke test"

info "Running Leela (monitor) in status mode..."
if CASA_CONFIG="$CASA_CONFIG" venv/bin/python casa_leela.py --status > /tmp/leela-test.json 2>&1; then
    CONTAINER_COUNT=$(python3 -c "import json; d=json.load(open('/tmp/leela-test.json')); print(len(d.get('containers', [])))" 2>/dev/null || echo "?")
    info "Leela OK — saw $CONTAINER_COUNT container(s)"
else
    warn "Leela returned non-zero. Check output:"
    cat /tmp/leela-test.json
fi

# ── Enable and start ───────────────────────────────────────────────────────────
section "Enable and start service"

echo ""
read -rp "  Start casa-planetexpress service now? [y/N] " start_now
if [[ "${start_now,,}" == "y" ]]; then
    sudo systemctl enable --now casa-planetexpress
    sleep 3
    if sudo systemctl is-active --quiet casa-planetexpress; then
        info "Service is running!"
        info "Tail logs: journalctl -u casa-planetexpress -f"
    else
        warn "Service may not have started cleanly. Check:"
        warn "  journalctl -u casa-planetexpress --no-pager -n 30"
    fi
else
    info "Skipped. Start manually with:"
    info "  sudo systemctl enable --now casa-planetexpress"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
section "Deploy complete"

echo ""
echo "  Install dir:   $INSTALL_DIR"
echo "  Config file:   $CASA_CONFIG"
echo "  Secrets file:  $ENV_FILE"
echo "  Systemd units: $SERVICE_FILE"
echo "                 $STACKS_SERVICE_FILE"
echo "  Logs:          $INSTALL_DIR/logs/"
echo "  State:         $INSTALL_DIR/state/"
echo ""
echo "  Telegram commands after startup:"
echo "    /help     — show all commands"
echo "    /status   — quick health check"
echo "    /check    — full scan + plan"
echo "    /updates  — image staleness report"
echo ""
echo "  Good news, everyone! Planet Express is deployed."
