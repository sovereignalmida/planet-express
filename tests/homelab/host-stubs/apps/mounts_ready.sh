#!/usr/bin/env bash
# Test homelab stand-in for the live host's mounts_ready.sh (which pings the NAS and
# mounts SMB shares). Here it only proves the readiness gate runs before casa-stacks.
# Set PE_FAIL_MOUNTS=1 in /etc/systemd/system/casa-mounts.service.d/ to rehearse a
# failed gate.
set -euo pipefail
if [[ "${PE_FAIL_MOUNTS:-0}" == "1" ]]; then
    echo "mounts_ready (stub): simulated NAS unreachable" >&2
    exit 1
fi
echo "mounts_ready (stub): all mounts ready"
