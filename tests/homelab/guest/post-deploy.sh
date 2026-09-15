#!/usr/bin/env bash
# post-deploy.sh — runs INSIDE the guest as root, once, after the first `bash deploy.sh`.
#
# The live host's casa-stacks.service carries a hand-added mount-readiness gate
# (Requires=/After=casa-mounts.service, see systemd/examples/casa-mounts.service.example).
# That gate is exactly why deploy.sh asks before overwriting the unit and defaults to N.
# Add the same gate here, inline in the unit file like the live host, so redeploys in the
# homelab hit the same prompt and the same "unit keeps its old content" behavior.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

UNIT=/etc/systemd/system/casa-stacks.service
[[ -f "$UNIT" ]] || { echo "$UNIT not found; run deploy.sh first"; exit 1; }

# Match only real directive lines: the rendered template mentions casa-mounts.service in
# its comments, so a bare grep reported "already gated" on an ungated unit.
if grep -qE '^(Requires|After)=.*casa-mounts\.service' "$UNIT"; then
    echo "casa-stacks already gated on casa-mounts"
else
    sed -i '/^\[Unit\]/a Requires=casa-mounts.service\nAfter=casa-mounts.service' "$UNIT"
    systemctl daemon-reload
    echo "added Requires=/After=casa-mounts.service to $UNIT"
fi

systemctl cat casa-stacks.service | grep -E '^(Requires|After)=' || true
echo
systemctl --no-pager --plain list-units 'casa-*' 'daily-borg-backup*' 'weekly-borg-backup*' || true
