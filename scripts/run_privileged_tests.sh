#!/bin/sh
# Run the root-only launcher tests (tests/test_launcher_privileged.py) somewhere they cannot hurt anything.
#
# Those tests install a root-owned helper under /usr/libexec and /etc, create accounts, write a sudoers fragment and
# move every process out of the cgroup namespace root. They are skipped everywhere else, and they refuse to run unless
# PE_PRIVILEGED_TESTS_OK=1, which THIS SCRIPT sets only inside a disposable environment. Never run them on a
# developer machine or on a host that matters.
#
#   scripts/run_privileged_tests.sh                  # default: a throwaway privileged Docker container
#   scripts/run_privileged_tests.sh --docker         # the same, explicitly
#   scripts/run_privileged_tests.sh --vm HOST        # rsync this tree to a disposable VM (e.g. the MOS test VM) and
#                                                    # run it there as root over ssh (root@HOST)
#   scripts/run_privileged_tests.sh --here --i-know-this-is-disposable
#                                                    # run in place: ONLY when this machine is itself disposable
#
# Environment: PE_VM_DIR (--vm: where the tree is copied, default /root/pe-privileged-tests; use a world-searchable
# directory such as /opt/pe-privileged-tests for the sudo strategy, whose unprivileged Core must read the tests), PE_TEST_IMAGE (docker image, default python:3.13-bookworm), PE_PYTHON (interpreter for --here and
# --vm, default python3), PE_SSH_OPTS (extra ssh options). Extra arguments after `--` go to pytest.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
MODE=docker
TARGET=
SURE=
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) MODE=docker ;;
    --vm) MODE=vm; shift; TARGET=${1:?--vm needs a host} ;;
    --here) MODE=here ;;
    --i-know-this-is-disposable) SURE=yes ;;
    --) shift; break ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

TESTS="tests/test_launcher_privileged.py tests/test_missions_containment_cgroup.py tests/test_launcher_helper.py tests/test_checkpoint_privileged.py tests/test_tripwire_privileged.py"

case "$MODE" in
  docker)
    command -v docker >/dev/null 2>&1 || { echo "docker is not installed; use --vm HOST" >&2; exit 2; }
    IMAGE=${PE_TEST_IMAGE:-python:3.13-bookworm}
    # --privileged + a private cgroup namespace: the container gets a writable cgroup2 subtree of its own.
    exec docker run --rm --privileged --cgroupns=private -e PE_PRIVILEGED_TESTS_OK=1 \
      -v "$HERE":/src:ro "$IMAGE" sh -c '
        set -eu
        apt-get update -qq && apt-get install -y -qq sudo passwd iproute2 >/dev/null
        cp -a /src /work && cd /work
        pip install -q -r requirements-dev.txt
        python -m pytest '"$TESTS"' -v -p no:cacheprovider "$@"
      ' sh "$@"
    ;;
  vm)
    # The VM must be disposable AND have cgroup v2, python3 with this project'"'"'s requirements, and sudo (for the
    # sudo strategy; its tests are skipped without it).
    # shellcheck disable=SC2086
    rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ -e "ssh ${PE_SSH_OPTS:-}" \
      "$HERE"/ "root@$TARGET":${PE_VM_DIR:-/root/pe-privileged-tests}/
    # shellcheck disable=SC2086
    QUOTED=""
    for a in "$@"; do QUOTED="$QUOTED '$(printf %s "$a" | sed "s/'/'\\\\''/g")'"; done
    # shellcheck disable=SC2086
    exec ssh ${PE_SSH_OPTS:-} "root@$TARGET" "chmod -R o+rX ${PE_VM_DIR:-/root/pe-privileged-tests} && cd ${PE_VM_DIR:-/root/pe-privileged-tests} && PE_PYTHON='${PE_PYTHON:-python3}' sh scripts/run_privileged_tests.sh --here --i-know-this-is-disposable -- $QUOTED"
    ;;
  here)
    [ "$SURE" = yes ] || { echo "refusing: --here needs --i-know-this-is-disposable" >&2; exit 2; }
    [ "$(id -u)" = 0 ] || { echo "refusing: --here must run as root" >&2; exit 2; }
    PY=${PE_PYTHON:-python3}
    cd "$HERE"
    PE_PRIVILEGED_TESTS_OK=1 exec "$PY" -m pytest $TESTS -v -p no:cacheprovider "$@"
    ;;
esac
