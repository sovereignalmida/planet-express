#!/bin/sh
# Thin launcher for scripts/oc_delegate.py: stdlib-only Python, system interpreter, no environment-driven imports (-E) and no
# user site (-s). See the docstring there. Usage: oc_delegate.sh run BRIEF.json [--dry-run] | budget | claims | release SLUG | selftest
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 -E -s "$HERE/oc_delegate.py" "$@"
