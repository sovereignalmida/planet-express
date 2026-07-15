#!/usr/bin/env bash
# deploy.sh — Planet Express deployment script
# Run this ON CasaMediaServer as casaroot:
#
#   cd /home/casaroot/apps/planetexpress
#   bash deploy.sh
#
# Or push from your Mac and run remotely:
#   ssh casaroot@192.168.1.94 'bash -s' < deploy.sh

set -euo pipefail

INSTALL_DIR="/home/casaroot/apps/planetexpress"
CONTEXT_FILE="/home/casaroot/casa-sysadmin-context.yaml"
ENV_FILE="/etc/planetexpress.env"
SERVICE_FILE="/etc/systemd/system/casa-planetexpress.service"
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

[[ "$(whoami)" == "casaroot" ]] || error "Run as casaroot"
[[ -d "$INSTALL_DIR" ]]         || error "Install directory not found: $INSTALL_DIR. Copy files here first."
[[ -f "$CONTEXT_FILE" ]]        || warn "Context file not found at $CONTEXT_FILE — create it before starting the service"

command -v docker > /dev/null   || error "docker not found"
command -v $PYTHON_BIN > /dev/null || error "python3 not found"

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

# ── API key setup ──────────────────────────────────────────────────────────────
section "API key setup"

if [[ ! -f "$ENV_FILE" ]]; then
    warn "Environment file not found at $ENV_FILE"
    echo ""
    read -rp "  Enter your ANTHROPIC_API_KEY (or press Enter to skip and set it manually): " api_key
    if [[ -n "$api_key" ]]; then
        echo "ANTHROPIC_API_KEY=$api_key" | sudo tee "$ENV_FILE" > /dev/null
        sudo chmod 600 "$ENV_FILE"
        info "API key saved to $ENV_FILE"
    else
        warn "Skipped. Create $ENV_FILE manually before starting the service:"
        warn "  echo 'ANTHROPIC_API_KEY=sk-ant-...' | sudo tee $ENV_FILE"
        warn "  sudo chmod 600 $ENV_FILE"
    fi
else
    info "Environment file already exists at $ENV_FILE"
fi

# ── Systemd service ────────────────────────────────────────────────────────────
section "Installing systemd service"

sudo cp "$INSTALL_DIR/systemd/casa-planetexpress.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
info "Service file installed"

# ── Smoke test ─────────────────────────────────────────────────────────────────
section "Smoke test"

info "Running Leela (monitor) in status mode..."
if venv/bin/python casa_leela.py --status > /tmp/leela-test.json 2>&1; then
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
echo "  Context file:  $CONTEXT_FILE"
echo "  API key file:  $ENV_FILE"
echo "  Systemd unit:  $SERVICE_FILE"
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
