#!/usr/bin/env bash
# vm.sh — run the Planet Express test homelab: a KVM guest shaped like the live host
# (Ubuntu 24.04, user casaroot, the /home/casaroot paths casa_leela.py reads, borg timers,
# a casa-mounts gate). Plain qemu + user-mode networking: no libvirt, no bridge, no root.
#
#   ./vm.sh up          fetch + verify image (if needed), create disk, boot, wait for cloud-init
#   ./vm.sh provision   push this working tree into the guest and run guest/provision.sh
#   ./vm.sh push        re-push the working tree only (keeps venv/, state/, logs/)
#   ./vm.sh ssh [cmd]   ssh in as casaroot
#   ./vm.sh status      is it running, what's forwarded
#   ./vm.sh down        clean shutdown
#   ./vm.sh destroy     shut down and delete the disk (keeps the downloaded base image)
#
# Everything the VM owns lives in $PE_HOMELAB_DIR (default ~/.cache/planet-express-homelab),
# never in the repo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE="${PE_HOMELAB_DIR:-$HOME/.cache/planet-express-homelab}"

IMG_NAME="noble-server-cloudimg-amd64.img"
IMG_BASE_URL="https://cloud-images.ubuntu.com/noble/current"
BASE_IMG="$STATE/$IMG_NAME"
DISK="$STATE/disk.qcow2"
PIDFILE="$STATE/qemu.pid"
CONSOLE="$STATE/console.log"
SEED_DIR="$STATE/seed"

VCPUS="${PE_VCPUS:-2}"
MEM_MB="${PE_MEM_MB:-3072}"
DISK_SIZE="${PE_DISK_SIZE:-20G}"
SSH_PORT="${PE_SSH_PORT:-2222}"
DASH_PORT="${PE_DASH_PORT:-18420}"   # host side; guest dashboard listens on 8420
SEED_PORT="${PE_SEED_PORT:-8765}"

# ControlMaster/ControlPath off: a user-level ~/.ssh/config multiplexing setting would
# otherwise keep reusing the master connection opened during the first-boot wait, whose
# login predates cloud-init adding casaroot to the docker group ("permission denied" on
# docker.sock in every later session).
SSH_COMMON=(-o StrictHostKeyChecking=no -o "UserKnownHostsFile=$STATE/known_hosts"
            -o LogLevel=ERROR -o ConnectTimeout=5 -o ControlMaster=no -o ControlPath=none)
SSH_OPTS=(-p "$SSH_PORT" "${SSH_COMMON[@]}")

die() { echo "vm.sh: $*" >&2; exit 1; }

is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

pubkey() {
    local k
    for k in "${PE_SSH_PUBKEY:-}" "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_ecdsa.pub" "$HOME/.ssh/id_rsa.pub"; do
        [[ -n "$k" && -f "$k" ]] && { cat "$k"; return; }
    done
    die "no SSH public key found; create one (ssh-keygen -t ed25519) or set PE_SSH_PUBKEY"
}

expected_sha() { awk -v f="$IMG_NAME" '$2=="*"f || $2==f {print $1}' "$STATE/SHA256SUMS"; }

cmd_fetch() {
    mkdir -p "$STATE"
    curl -fsSL -o "$STATE/SHA256SUMS" "$IMG_BASE_URL/SHA256SUMS"
    if [[ -f "$BASE_IMG" && "$(expected_sha)" == "$(sha256sum "$BASE_IMG" | cut -d' ' -f1)" ]]; then
        echo "base image present and verified"
        return
    fi
    echo "downloading $IMG_NAME ..."
    curl -fL --retry 3 -o "$BASE_IMG.part" "$IMG_BASE_URL/$IMG_NAME"
    local exp act
    exp="$(expected_sha)"; act="$(sha256sum "$BASE_IMG.part" | cut -d' ' -f1)"
    [[ -n "$exp" && "$exp" == "$act" ]] || { rm -f "$BASE_IMG.part"; die "checksum mismatch (expected $exp, got $act)"; }
    mv "$BASE_IMG.part" "$BASE_IMG"
    echo "verified sha256 $act"
}

write_seed() {
    mkdir -p "$SEED_DIR"
    local key; key="$(pubkey)"
    printf 'instance-id: pe-homelab-%s\nlocal-hostname: pe-homelab\n' "$(date +%s)" > "$SEED_DIR/meta-data"
    : > "$SEED_DIR/vendor-data"
    # Python for the substitution: an SSH key can contain characters sed would choke on.
    python3 - "$HERE/cloud-init/user-data.yaml" "$SEED_DIR/user-data" "$key" <<'PY'
import sys
src, dst, key = sys.argv[1:4]
open(dst, "w").write(open(src).read().replace("@SSH_PUBKEY@", key.strip()))
PY
}

wait_for_guest() {
    local deadline=$((SECONDS + 900))
    echo "waiting for SSH + cloud-init (first boot installs Docker; allow ~5 minutes) ..."
    until ssh "${SSH_OPTS[@]}" casaroot@127.0.0.1 'test -f /var/lib/pe-homelab/cloud-init-done' 2>/dev/null; do
        is_running || die "qemu exited during boot; see $CONSOLE"
        (( SECONDS < deadline )) || die "timed out waiting for the guest; see $CONSOLE"
        sleep 10
    done
}

cmd_up() {
    command -v qemu-system-x86_64 >/dev/null || die "qemu-system-x86_64 not installed"
    command -v qemu-img >/dev/null || die "qemu-img not installed (usually in qemu-utils)"
    [[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm not usable by $(whoami)"
    is_running && { echo "already running (pid $(cat "$PIDFILE"))"; return; }
    cmd_fetch

    local first_boot=0
    if [[ ! -f "$DISK" ]]; then
        qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMG" "$DISK" "$DISK_SIZE"
        first_boot=1
    fi

    # Global, not `local`: the EXIT trap runs after cmd_up returns, and under `set -u` a
    # function-local name is gone by then ("seed_pid: unbound variable", seed server leaked).
    SEED_PID=""
    if (( first_boot )); then
        write_seed
        python3 -m http.server "$SEED_PORT" --bind 127.0.0.1 --directory "$SEED_DIR" >/dev/null 2>&1 &
        SEED_PID=$!
        trap '[[ -n "${SEED_PID:-}" ]] && kill "$SEED_PID" 2>/dev/null; true' EXIT
    fi

    # 10.0.2.2 is the host as seen from qemu user-mode networking, so the guest's
    # cloud-init NoCloud datasource can fetch the seed from the local http.server.
    qemu-system-x86_64 \
        -name pe-homelab -enable-kvm -cpu host -smp "$VCPUS" -m "$MEM_MB" \
        -drive "file=$DISK,if=virtio,format=qcow2" \
        -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:$DASH_PORT-:8420" \
        -device virtio-net-pci,netdev=n0 \
        -smbios "type=1,serial=ds=nocloud;s=http://10.0.2.2:$SEED_PORT/" \
        -display none -serial "file:$CONSOLE" \
        -daemonize -pidfile "$PIDFILE"

    wait_for_guest
    echo "up: ssh via './vm.sh ssh', dashboard (after deploy) at http://127.0.0.1:$DASH_PORT/"
    (( first_boot )) && echo "next: ./vm.sh provision"
    return 0
}

cmd_push() {
    is_running || die "not running; ./vm.sh up first"
    echo "pushing working tree -> /home/casaroot/planet-express (venv/, state/, logs/ preserved)"
    tar -C "$REPO" \
        --exclude=./venv --exclude=./state --exclude=./logs --exclude='__pycache__' \
        --exclude=./tests/homelab/secrets.env \
        -cf - . \
      | ssh "${SSH_OPTS[@]}" casaroot@127.0.0.1 \
          'mkdir -p ~/planet-express && tar -C ~/planet-express -xf -'
}

cmd_provision() {
    cmd_push
    # Secrets travel only here, never on a plain push: provision.sh installs them as
    # /etc/planetexpress.env and deletes the staging copy. umask 077 on the receiving side
    # so the staged file is owner-only for the few seconds it exists.
    if [[ -f "$HERE/secrets.env" ]]; then
        ssh "${SSH_OPTS[@]}" casaroot@127.0.0.1 'umask 077; cat > /tmp/pe-secrets.env' < "$HERE/secrets.env"
        echo "staged tests/homelab/secrets.env for install (test bot credentials)"
    fi
    ssh "${SSH_OPTS[@]}" -t casaroot@127.0.0.1 \
        'sudo bash ~/planet-express/tests/homelab/guest/provision.sh'
}

cmd_ssh() {
    is_running || die "not running; ./vm.sh up first"
    exec ssh "${SSH_OPTS[@]}" -t casaroot@127.0.0.1 "$@"
}

cmd_status() {
    if is_running; then
        echo "running (pid $(cat "$PIDFILE")), ssh 127.0.0.1:$SSH_PORT, dashboard http://127.0.0.1:$DASH_PORT/"
    else
        echo "not running"
    fi
    [[ -f "$DISK" ]] && echo "disk: $DISK ($(du -h "$DISK" | cut -f1) used)"
    [[ -f "$BASE_IMG" ]] && echo "base: $BASE_IMG"
    return 0
}

cmd_down() {
    is_running || { echo "not running"; return; }
    ssh "${SSH_OPTS[@]}" casaroot@127.0.0.1 'sudo systemctl poweroff' 2>/dev/null || true
    local pid; pid="$(cat "$PIDFILE")"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 2; done
    kill -0 "$pid" 2>/dev/null && { echo "guest didn't power off; stopping qemu"; kill "$pid"; }
    rm -f "$PIDFILE"
    echo "stopped"
}

cmd_destroy() {
    cmd_down
    rm -rf "$DISK" "$SEED_DIR" "$STATE/known_hosts" "$CONSOLE"
    echo "disk deleted (base image kept at $BASE_IMG)"
}

case "${1:-}" in
    fetch)     cmd_fetch ;;
    up)        cmd_up ;;
    push)      cmd_push ;;
    provision) cmd_provision ;;
    ssh)       shift; cmd_ssh "$@" ;;
    status)    cmd_status ;;
    down)      cmd_down ;;
    destroy)   cmd_destroy ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
