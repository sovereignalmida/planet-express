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

# An independent Codex review caught a real gap here: if this is run as root (directly
# or via `sudo bash deploy.sh`), RUN_USER becomes "root" and the generated service runs
# Bender as root -- at which point bare (no-sudo-prefix) commands already have full
# root access, completely bypassing casa_bender.py's _check_sudo_allowlist(), which
# only ever inspects segments containing the literal word "sudo". The whole Spec 4
# security model assumes Bender runs as an unprivileged user that needs sudo for
# anything privileged; running as root silently voids that assumption.
[[ "$RUN_USER" != "root" ]] || error "Don't run this as root (or via sudo) -- run it as the " \
    "unprivileged user Planet Express should run as. It calls sudo itself for the specific " \
    "privileged steps that need it."

command -v docker > /dev/null              || error "docker not found"
docker compose version > /dev/null 2>&1    || error "docker compose plugin not found (need 'docker compose', not just 'docker-compose')"
# `docker compose version` only checks the CLI/plugin is installed -- it never talks to
# the daemon, so it passes even when this user can't access the Docker socket (not yet
# in the `docker` group). An independent Codex review found that without this check,
# install completes and enables casa-stacks, but the smoke test AND every boot-time
# `docker compose up` then fail with permission denied.
docker info > /dev/null 2>&1 \
    || error "docker found but this user ($RUN_USER) can't reach the Docker daemon -- " \
        "add it to the docker group (sudo usermod -aG docker $RUN_USER) and start a new " \
        "shell session, then re-run this script."
command -v $PYTHON_BIN > /dev/null         || error "python3 not found"
$PYTHON_BIN -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || error "python3 is $($PYTHON_BIN --version 2>&1), need 3.11+ (repo code uses newer syntax that older Python can't even parse)"

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

# An independent Codex review found that bash only expands a literal '~' at specific
# syntax positions parsed from the script itself -- never on the *value* of a variable
# read at runtime, so a user typing "~/planetexpress/config.yaml" here would get a
# literal '~' in CASA_CONFIG, silently creating a "~" directory under the repo (the
# wizard's mkdir -p would run relative to $INSTALL_DIR) instead of expanding to $HOME.
while true; do
    read -rp "  Path for config.yaml [/etc/planetexpress/config.yaml]: " config_path
    config_path="${config_path/#\~/$HOME}"
    config_path="${config_path:-/etc/planetexpress/config.yaml}"
    [[ "$config_path" == /* ]] && break
    warn "Must be an absolute path (or blank for the default)."
done
export CASA_CONFIG="$config_path"

# Always run the wizard, even if $CASA_CONFIG already exists -- it detects that itself
# and reuses the existing topology config unchanged rather than re-prompting, but an
# independent Codex review found that skipping it here entirely (the previous
# behavior) also skipped sudoers reconciliation, the only code path that keeps
# /etc/sudoers.d/planetexpress in sync with sudo_allowlist. That mattered for a real
# case: an upgrade, or a config hand-edited after the fact, previously left the OS-level
# grant stale or missing with no indication anything was out of sync.
venv/bin/python scripts/setup_wizard.py

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
    # -s (silent): these are credentials, don't echo them to the terminal/scrollback.
    if [[ "$llm_provider" == "anthropic" ]]; then
        read -rsp "  Enter your ANTHROPIC_API_KEY (or press Enter to set it manually later): " api_key
        echo ""
        api_key_line="ANTHROPIC_API_KEY=$api_key"
    else
        read -rsp "  Enter your OPENAI_API_KEY (or press Enter to set it manually later): " api_key
        echo ""
        api_key_line="OPENAI_API_KEY=$api_key"
    fi

    read -rsp "  Telegram bot token (from @BotFather, see INSTALL.md): " tg_token
    echo ""
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
    # Renders to a local tmp file *before* touching $2 -- an independent Codex review
    # found that piping straight into `sudo tee "$2"` lets tee truncate the existing
    # (working) unit file immediately on open, before the renderer has produced any
    # output; if rendering then fails (e.g. a rejected character), pipefail aborts the
    # script but the previously-good unit file is already empty. Rendering to a local
    # file first means a failure here never touches the installed unit at all.
    local rendered_tmp
    rendered_tmp="$(mktemp)"
    venv/bin/python scripts/render_template.py "$1" "$INSTALL_DIR" "$RUN_USER" "$RUN_GROUP" "$CASA_CONFIG" \
        > "$rendered_tmp"
    sudo install -m 644 "$rendered_tmp" "$2"
    rm -f "$rendered_tmp"
}

render_unit "$INSTALL_DIR/systemd/casa-planetexpress.service.template" "$SERVICE_FILE"

# casa-stacks.service is handled separately: an independent Codex review found that
# unconditionally overwriting it on a redeploy silently destroys any custom
# Requires=/After= mount-readiness gate an operator added (see
# systemd/examples/casa-mounts.service.example) -- stacks would then start at the next
# boot before NAS/network mounts are ready, potentially writing to local fallback
# paths instead of the real mount. Ask before clobbering an existing one.
if [[ -f "$STACKS_SERVICE_FILE" ]]; then
    warn "$STACKS_SERVICE_FILE already exists. If you (or a previous install) added a custom"
    warn "mount-readiness gate (Requires=/After=) to it, overwriting will silently remove that."
    read -rp "  Overwrite it with the generic template? [y/N]: " overwrite_stacks
    if [[ "${overwrite_stacks,,}" == "y" ]]; then
        render_unit "$INSTALL_DIR/systemd/casa-stacks.service.template" "$STACKS_SERVICE_FILE"
    else
        info "Left $STACKS_SERVICE_FILE untouched."
    fi
else
    render_unit "$INSTALL_DIR/systemd/casa-stacks.service.template" "$STACKS_SERVICE_FILE"
fi

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
