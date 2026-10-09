#!/bin/sh
# Planet Express first-run setup: starts the browser wizard.
#
#   git clone https://github.com/sovereignalmida/planet-express.git
#   cd planet-express
#   sudo ./setup.sh
#
# It builds a throwaway virtualenv from the hash-pinned requirements-bootstrap.txt, runs the wizard from it, and
# deletes the virtualenv when the wizard ends. Nothing is installed on the host until you approve a plan in the
# browser. Extra arguments go to the wizard (for example --port 9443 or --bind 192.168.1.50).
#
# This is plain POSIX sh on purpose: it has to run on a host with nothing installed, including MOS.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)

say()  { printf '%s\n' "$*"; }
die()  { printf 'setup: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root (sudo ./setup.sh). It writes system files only after you approve the plan, but it needs to be able to."

PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null 2>&1 || die "python3 was not found. Install Python 3.11 or newer, then run this again."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required; this host has $("$PYTHON" --version 2>&1)."
[ -f "$HERE/requirements-bootstrap.txt" ] || die "requirements-bootstrap.txt is missing: this checkout is incomplete."

WORK=$(mktemp -d "${TMPDIR:-/tmp}/pe-setup-venv.XXXXXX")
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM HUP

say "Preparing the setup environment (this needs internet access, once)..."
if ! "$PYTHON" -m venv "$WORK/venv" >/dev/null 2>&1; then
    # MOS ships Python without ensurepip: make the venv without pip and fetch pip into it.
    rm -rf "$WORK/venv"
    "$PYTHON" -m venv --without-pip "$WORK/venv" || die "could not create a virtual environment (is python3-venv installed?)."
    "$WORK/venv/bin/python" -c 'import urllib.request, sys; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", sys.argv[1])' \
        "$WORK/get-pip.py" || die "could not download pip. Does this host have internet access?"
    "$WORK/venv/bin/python" "$WORK/get-pip.py" --quiet --disable-pip-version-check >/dev/null \
        || die "could not install pip into the setup environment."
fi
"$WORK/venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes \
    -r "$HERE/requirements-bootstrap.txt" \
    || die "could not install the setup dependencies. Check the host's internet access and try again."

# Not exec: the trap must still run to remove the environment when the wizard ends.
cd "$HERE"
PYTHONPATH="$HERE" "$WORK/venv/bin/python" -u -m planet_express.setup serve --repo-root "$HERE" "$@"
