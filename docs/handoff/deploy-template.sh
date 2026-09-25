#!/usr/bin/env bash
# TEMPLATE — copy to /tmp/pe-deploy-<version>.sh on the live host and fill in the four
# variables below. Not meant to be run as-is.
#
# This lived in a scratch directory for six releases and was reconstructed from memory three
# times, losing its accumulated lessons each time. It lives here now.
#
# ── The shape of a deploy ─────────────────────────────────────────────────────────────
#
#   1. refuse to start unless the host is exactly where you think it is
#   2. keep the current code on a branch, and snapshot config + DB
#   3. stop the dashboard FIRST, then core
#   4. check out the tag, replay the host-only commit, restore the local note
#   5. start core, then the dashboard
#   6. verify — and verify the things THIS release could have broken, not a fixed list
#
# ── Three rules paid for in incidents ─────────────────────────────────────────────────
#
# STOP THE DASHBOARD BEFORE THE CHECKOUT. Jinja loads templates from disk on each render, so
# a checkout under a running dashboard 500s it immediately — before any restart can help.
# Dashboard down, then core; on the way back up, core first, then dashboard.
#
# A VERIFICATION STEP MUST ONLY READ WHAT THE RELEASE IT VERIFIES ACTUALLY HAS. A check
# carried forward from an earlier release printed `config.LEGACY_PLANS_ENABLED`, an attribute
# deleted in 2.0.0, and crashed the verify block on three consecutive deploys before anyone
# fixed it rather than re-reading it. Prefer checks written against behaviour over checks
# written against a name — and never grep for a name the code mentions only in a comment
# explaining why it is no longer used. (That one cost a rewrite during 2.1.0's gate.)
#
# A VERIFY STEP THAT ONLY PRINTS IS NOT A CHECK. Five of these shipped in six releases: HTTP
# codes echoed rather than asserted, `pragma integrity_check` printed but not gated, and
# `|| bad "..."` after a Python check -- which always succeeds, because bad() succeeds. Every
# check here now exits nonzero on the thing it is named after.
#
# STAGE THE FILES A COMMIT IS ABOUT, BY NAME. `git add -A` once swept an unfinished slice into
# a release commit that was then tagged and deployed, putting un-gated code into production.
# That is a repo hygiene rule rather than a deploy rule, but it is what this script ships.
#
# ── Before you run it ─────────────────────────────────────────────────────────────────
#
# Read the live host, read-only, and fill in what you find:
#
#   ssh casaroot@HOST 'cd /home/casaroot/apps/planetexpress \
#     && git rev-parse --abbrev-ref HEAD && git log --oneline -1 && git describe --tags \
#     && git status --porcelain --untracked-files=no \
#     && git branch --list "live-backup-*" | sort -V | tail -1'
#
# Then run every Python check in the verify block locally, against the tagged code, before
# copying this to the host. A verify block that has never been executed is not a check.

set -euo pipefail
cd /home/casaroot/apps/planetexpress

TAG=vX.Y.Z
LOCAL_COMMIT=0000000   # the host-only commit to replay on top (`git describe` shows it)
BACKUP=live-backup-NN  # one past the highest existing live-backup-*
LOCAL_NOTE="$(mktemp /tmp/pe-local-note.XXXXXX.patch)"

ok()   { echo -e "\033[0;32m[ OK ]\033[0m $*"; }
info() { echo -e "\033[0;36m[....]\033[0m $*"; }
bad()  { echo -e "\033[0;31m[FAIL]\033[0m $*"; }

sudo true   # authenticate up front, so nothing stalls mid-swap waiting for a password

[[ "$(git rev-parse --abbrev-ref HEAD)" == live/deployed ]] || { bad "not on live/deployed"; exit 1; }
[[ "$(git rev-parse --short=7 HEAD)" == "$LOCAL_COMMIT" ]] || { bad "HEAD is not $LOCAL_COMMIT; stopping"; exit 1; }
git rev-parse --verify -q "$BACKUP" >/dev/null && { bad "$BACKUP already exists; pick the next number"; exit 1; }
# The only tracked modification allowed is the known local note.
OTHER="$(git status --porcelain --untracked-files=no | grep -v ' docs/handoff/2026-09-18-weekend-handoff.md$' || true)"
[[ -z "$OTHER" ]] || { bad "unexpected tracked modifications:"; echo "$OTHER"; exit 1; }
# `git diff` against HEAD, not the worktree: plain `git diff` omits the index, so a note with
# staged changes produced an empty patch and the hard reset below destroyed it silently.
git diff HEAD > "$LOCAL_NOTE"

info "fetching $TAG"
git fetch -q origin "refs/tags/$TAG:refs/tags/$TAG"
git branch -f "$BACKUP" HEAD
ok "previous code kept on branch $BACKUP ($(git log --oneline -1 "$BACKUP"))"

info "snapshotting config + DB before the swap"
venv/bin/python scripts/state_snapshot.py create --label "pre-${TAG//./-}"
SNAPSHOT="$(ls data/snapshots | tail -1)"
ok "snapshot: $SNAPSHOT"

restore_note() {
    # An empty patch is normal (no local note). A non-empty one that will not apply is not:
    # `|| true` used to swallow the conflict, drop the note, and still report success.
    [[ -s "$LOCAL_NOTE" ]] || return 0
    git apply "$LOCAL_NOTE" || { bad "the local note did not apply cleanly; it is kept at $LOCAL_NOTE"; return 1; }
}

rollback() {
    bad "deploy failed — restoring $BACKUP and restarting on the previous code"
    git cherry-pick --abort 2>/dev/null || true
    git reset -q --hard "$BACKUP"
    restore_note || bad "could not restore the local note; it is kept at $LOCAL_NOTE"
    sudo systemctl reset-failed casa-planetexpress casa-dashboard 2>/dev/null || true
    sudo systemctl start casa-planetexpress casa-dashboard
    sleep 5
    systemctl is-active casa-planetexpress casa-dashboard || true
    bad "rolled back to $(git log --oneline -1). $SNAPSHOT is there if the release touched"
    bad "the schema or config; a code-only release needs nothing from it."
    exit 1
}

# Dashboard first. See the rule above.
info "stopping casa-dashboard, then casa-planetexpress"
sudo systemctl stop casa-dashboard casa-planetexpress
trap rollback ERR

info "checking out $TAG and replaying the host-only commit"
git reset -q --hard "$TAG"
git -c user.name=casaroot -c user.email=casaroot@casamediaserver cherry-pick "$LOCAL_COMMIT" >/dev/null
restore_note
ok "code: $(git log --oneline -1)  ($(git describe --tags))"

info "starting casa-planetexpress and casa-dashboard"
sudo systemctl start casa-planetexpress casa-dashboard
sleep 12
[[ "$(systemctl is-active casa-planetexpress)" == active ]] || { bad "core not active"; false; }
[[ "$(systemctl is-active casa-dashboard)" == active ]] || { bad "dashboard not active"; false; }
if sudo journalctl -u casa-planetexpress -u casa-dashboard --since "-20s" --no-pager | grep -q "CRITICAL\|Traceback\|Invalid config"; then
    bad "errors in the journal:"; sudo journalctl -u casa-planetexpress -u casa-dashboard --since "-20s" --no-pager | tail -20; false
fi
ok "core and dashboard active, journal clean"

# The ERR trap stays armed through the HTTP assertions. "Services up, journal clean" is not the
# point of no return -- a dashboard answering 500 on every route is a failed deploy and should
# roll back, not be announced.
#
# Asserted, not printed. These used to be bare `echo`s: a 500 on every route still read as a
# successful deploy, because the only thing that had failed was the operator's attention.
#
# curl already prints 000 via -w on a connection failure and then exits nonzero; `|| true`
# keeps that single 000 instead of appending a second one.
code() { curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8420$1" 2>/dev/null || true; }
for _ in $(seq 1 10); do [[ "$(code /login)" == 200 ]] && break; sleep 2; done
http_is() {
    local path="$1" want="$2" got
    got="$(code "$path")"
    printf "  %-24s -> %s   (expect %s)\n" "$path" "$got" "$want"
    [[ "$got" == "$want" ]] || { bad "$path returned $got, expected $want"; return 1; }
}
http_is /                   302
http_is /login              200
http_is /static/cockpit.css 200

# Past here the release is serving. The checks below observe state this deploy did not change,
# so they fail the script loudly rather than rolling back a working release over, say, a
# database that was already damaged before it started.
trap - ERR
rm -f "$LOCAL_NOTE"

info "checking the database"
CASA_CONFIG="$PWD/config.yaml" venv/bin/python -c '
import sqlite3, config, sys
c = sqlite3.connect("file:%s?mode=ro" % config.ACTIONS_DB, uri=True)
version = c.execute("pragma user_version").fetchone()[0]
violations = c.execute("pragma foreign_key_check").fetchall()
integrity = c.execute("pragma integrity_check").fetchone()[0]
print("  schema:", version, "| foreign keys:", violations or "clean", "| integrity:", integrity)
# All three gate. Printing a corrupt database and exiting 0 is how a bad one passes a check
# that exists to catch it.
sys.exit(0 if version == 5 and not violations and integrity == "ok" else 1)' \
    || { bad "the database did not pass verification"; false; }

# ── Release-specific verification goes here ───────────────────────────────────────────
#
# One check per thing this release could plausibly have broken, each printing what it found
# so the output is readable when it passes and not only when it fails. For a presentation-only
# release that means asserting the execution surface did NOT move:
#
#   CASA_CONFIG="$PWD/config.yaml" venv/bin/python -c '
#   import casa_bender as bender
#   from planet_express.execution import runbook
#   print("  sudo allowlist enforced:", hasattr(bender, "_check_sudo_allowlist"))
#   print("  step types:", len(runbook.STEP_TYPES))
#   raise SystemExit(0 if hasattr(bender, "_check_sudo_allowlist") else 1)'
#
# ──────────────────────────────────────────────────────────────────────────────────────

info "checking the dashboard can still reach core over RPC (its user cannot open the DB)"
# `|| bad "..."` is not error handling: bad() succeeds, so the failure was swallowed and the
# script went on to print the green "deployed" line.
sudo -u planetexpress-web env CASA_CONFIG="$PWD/config.yaml" PYTHONPATH="$PWD" venv/bin/python -c '
import sys
import config
from planet_express.integrations.rpc import call
print("  canary.candidates ->", call(config.RPC_SOCKET, "canary.candidates", {})["result"] or "none open")
r = call(config.RPC_SOCKET, "config.get", {})["result"]
active = r["sha256"] == r["loaded_sha256"]
print("  file sha", r["sha256"][:12], "| running core loaded", (r["loaded_sha256"] or "?")[:12],
      "| ACTIVE" if active else "| MISMATCH")
# A core running different config from the file on disk is a failed deploy, not a footnote.
sys.exit(0 if active else 1)' \
    || { bad "the dashboard could not verify core over RPC, or core is running stale config"; false; }
echo
ok "deployed $TAG. Previous code kept on branch $BACKUP."
echo "     Rollback: git reset --hard $BACKUP, then"
echo "     sudo systemctl restart casa-planetexpress casa-dashboard."
