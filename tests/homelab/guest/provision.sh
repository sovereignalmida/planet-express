#!/usr/bin/env bash
# provision.sh — runs INSIDE the test guest as root (vm.sh provision does this for you).
#
# Shapes the guest like the live host so a 1.x install behaves the way it does there:
#   - fixture compose stacks under /home/casaroot/stacks
#   - host-shaped stubs at the exact paths casa_leela.py reads (gluetun .env, Traefik
#     dynamic config + certs, one of them expiring soon)
#   - daily/weekly borg oneshot services + timers with the live unit names
#   - a casa-mounts readiness gate (wired into casa-stacks by post-deploy.sh)
#   - /etc/planetexpress/config.yaml and /etc/planetexpress.env
#   - /home/casaroot at mode 750, like a normal home dir, so 1c's ACL grants are really tested
#
# Idempotent: safe to re-run after ./vm.sh push.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

OPS=casaroot
HOME_DIR=/home/$OPS
REPO=$HOME_DIR/planet-express
HOMELAB=$REPO/tests/homelab
STUBS=$HOMELAB/host-stubs

step() { printf '\n== %s\n' "$*"; }

step "home directory mode (750, like the live host)"
chmod 750 "$HOME_DIR"

step "fixture stacks -> $HOME_DIR/stacks"
install -d -o $OPS -g $OPS "$HOME_DIR/stacks"
for stack in "$HOMELAB"/stacks/*/; do
    name="$(basename "$stack")"
    install -d -o $OPS -g $OPS "$HOME_DIR/stacks/$name"
    install -m 644 -o $OPS -g $OPS "$stack/docker-compose.yml" "$HOME_DIR/stacks/$name/docker-compose.yml"
done

step "gluetun env stub (casa_leela.py GLUETUN_ENV_FILE)"
install -d -o $OPS -g $OPS "$HOME_DIR/stacks/network"
install -m 600 -o $OPS -g $OPS "$STUBS/stacks/network/.env" "$HOME_DIR/stacks/network/.env"

step "Traefik file-provider stubs (casa_leela.py _TRAEFIK_DYNAMIC_DIR / _TRAEFIK_CERTS_HOST_DIR)"
PROXY=$HOME_DIR/apps/network/proxy
install -d -o $OPS -g $OPS "$PROXY/dynamic" "$PROXY/certs"
install -m 644 -o $OPS -g $OPS "$STUBS/apps/network/proxy/dynamic/certs.yml" "$PROXY/dynamic/certs.yml"
make_cert() {  # name days
    local name=$1 days=$2
    [[ -f "$PROXY/certs/$name.crt" ]] && return
    openssl req -x509 -newkey rsa:2048 -nodes -days "$days" \
        -subj "/O=Planet Express Homelab/CN=$name.homelab.test" \
        -addext "subjectAltName=DNS:$name.homelab.test,DNS:*.$name.homelab.test" \
        -keyout "$PROXY/certs/$name.key" -out "$PROXY/certs/$name.crt" 2>/dev/null
    chown $OPS:$OPS "$PROXY/certs/$name".{crt,key}
    chmod 600 "$PROXY/certs/$name.key"
}
make_cert wildcard 60     # renew_soon tier would start at 30d; this one is "valid"
make_cert expiring 5      # exercises the "expiring" (<= 7d) tier

step "mount-readiness stub + borg stub units"
install -d -o $OPS -g $OPS "$HOME_DIR/apps"
install -m 755 -o $OPS -g $OPS "$STUBS/apps/mounts_ready.sh" "$HOME_DIR/apps/mounts_ready.sh"
for unit in casa-mounts.service daily-borg-backup.service daily-borg-backup.timer \
            weekly-borg-backup.service weekly-borg-backup.timer; do
    install -m 644 "$STUBS/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now casa-mounts.service
systemctl enable --now daily-borg-backup.timer weekly-borg-backup.timer
# One real run each, so Leela sees a completed job with a last-run timestamp.
systemctl start daily-borg-backup.service weekly-borg-backup.service

step "Planet Express config (/etc/planetexpress)"
install -d -m 755 /etc/planetexpress
install -m 644 "$HOMELAB/config.homelab.yaml" /etc/planetexpress/config.yaml
if [[ -f /tmp/pe-secrets.env ]]; then
    install -m 600 -o root -g root /tmp/pe-secrets.env /etc/planetexpress.env
    rm -f /tmp/pe-secrets.env
    echo "installed /etc/planetexpress.env from tests/homelab/secrets.env (test bot)"
elif [[ ! -f /etc/planetexpress.env ]]; then
    # Deliberately NOT installing the empty example: deploy.sh only prompts for the LLM key
    # and bot credentials when /etc/planetexpress.env is absent, so an empty placeholder
    # would silently skip those prompts and leave the bot unable to start.
    echo "no tests/homelab/secrets.env pushed; deploy.sh will prompt for the test bot and LLM key"
fi

step "bring fixture stacks up"
for stack in "$HOME_DIR"/stacks/*/docker-compose.yml; do
    sudo -u $OPS docker compose -f "$stack" up -d
done

cat <<EOF

== provisioned. Next, the 1.x baseline install, exactly as on the live host:

   ./vm.sh ssh
   cd ~/planet-express && bash deploy.sh
     - stacks root:  /home/casaroot/stacks
     - dashboard port: 8420 (reach it from your machine at http://127.0.0.1:18420/)
   sudo bash ~/planet-express/tests/homelab/guest/post-deploy.sh

EOF
