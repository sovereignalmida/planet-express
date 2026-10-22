#!/bin/sh
# Run the v4 repair rehearsal (scripts/rehearse_repair.py) somewhere it cannot hurt anything. Same discipline as
# scripts/run_privileged_tests.sh: the rehearsal drives real Docker (it recreates containers, writes a compose project and
# runs a checkpoint restore), so it runs only on a disposable host, as root, with PE_REHEARSAL_OK=1 set HERE and never by you.
#
#   scripts/run_rehearsal.sh --vm HOST [-- ARGS]      # rsync this tree to a disposable VM and run it there as root over ssh
#                                                     # (root@HOST); ARGS go to rehearse_repair.py (e.g. --scenario canary)
#   scripts/run_rehearsal.sh --here --i-know-this-is-disposable [-- ARGS]
#                                                     # run in place: ONLY when this machine is itself disposable
#
# The host needs: Docker with the compose plugin, python3 with this project's requirements (requirements.txt), and
# nothing running in Docker that you care about (the rehearsal refuses a host with foreign containers). Set
# PE_REHEARSAL_SKIP_GATE=1 to skip the side-door gate on a VM that has not been through the launcher setup (the record
# says it was skipped).
#
# Environment: PE_VM_DIR (--vm: where the tree is copied, default /root/pe-rehearsal), PE_PYTHON (default python3),
# PE_SSH_OPTS (extra ssh options). The JSON record goes to stdout; the exit status is the rehearsal's (0 pass, 1 a step
# failed, 2 refused).
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
MODE=
TARGET=
SURE=
while [ $# -gt 0 ]; do
  case "$1" in
    --vm) MODE=vm; shift; TARGET=${1:?--vm needs a host} ;;
    --here) MODE=here ;;
    --i-know-this-is-disposable) SURE=yes ;;
    --) shift; break ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "refusing: say where to run it (--vm HOST or --here --i-know-this-is-disposable)" >&2; exit 2; }

case "$MODE" in
  vm)
    DIR=${PE_VM_DIR:-/root/pe-rehearsal}
    # shellcheck disable=SC2086
    rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ -e "ssh ${PE_SSH_OPTS:-}" \
      "$HERE"/ "root@$TARGET":"$DIR"/
    QUOTED=""
    for a in "$@"; do QUOTED="$QUOTED '$(printf %s "$a" | sed "s/'/'\\\\''/g")'"; done
    # shellcheck disable=SC2086
    exec ssh ${PE_SSH_OPTS:-} "root@$TARGET" "cd '$DIR' && PE_REHEARSAL_OK=1 PE_REHEARSAL_SKIP_GATE='${PE_REHEARSAL_SKIP_GATE:-}' '${PE_PYTHON:-python3}' scripts/rehearse_repair.py --i-know-this-is-disposable $QUOTED"
    ;;
  here)
    [ "$SURE" = yes ] || { echo "refusing: --here needs --i-know-this-is-disposable" >&2; exit 2; }
    [ "$(id -u)" = 0 ] || { echo "refusing: --here must run as root" >&2; exit 2; }
    cd "$HERE"
    PE_REHEARSAL_OK=1 exec "${PE_PYTHON:-python3}" scripts/rehearse_repair.py --i-know-this-is-disposable "$@"
    ;;
esac
