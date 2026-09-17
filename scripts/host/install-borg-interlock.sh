#!/usr/bin/env bash
# install-borg-interlock.sh — move the root-run borg backup script to root ownership and add the
# Planet Express maintenance marker (T25, decision D32). Run as the operator user (casaroot):
#
#     bash install-borg-interlock.sh            # sudo asks for your password once
#
# Why: daily-/weekly-borg-backup.service run /home/casaroot/apps/borg-backup.sh as ROOT, but that
# file is casaroot-owned 775 inside /home/casaroot/apps, which is mode 777 — so any local user,
# including the dashboard's deliberately unprivileged planetexpress-web, could replace it and get
# root on the next run. This installs a root-owned copy in /usr/local/sbin, points both units at it
# with drop-ins, retires the old copy, and removes world-write from /home/casaroot/apps.
#
# The copy is the existing script unchanged except for one inserted block right after
# `check_prerequisites || exit 1`: it writes /var/lib/planetexpress/maintenance for the whole run
# (core pauses scans and host mutations while it exists; core can read it but never clear it) and
# removes it on exit, including on SIGTERM. Idempotent: re-running re-applies from the old copy
# only if the installed one is missing.
set -euo pipefail

SRC="${PE_BORG_SRC:-/home/casaroot/apps/borg-backup.sh}"
DST="${PE_BORG_DST:-/usr/local/sbin/planetexpress-borg-backup.sh}"
APPS_DIR="$(dirname "$SRC")"
MARKER="/var/lib/planetexpress/maintenance"
ANCHOR='check_prerequisites || exit 1'
UNITS=(daily weekly)
STAMP="$(date +%Y%m%d)"
RETIRED="$SRC.retired-$STAMP"

ok()   { echo -e "\033[0;32m[ OK ]\033[0m $*"; }
info() { echo -e "\033[0;36m[....]\033[0m $*"; }
die()  { echo -e "\033[0;31m[FAIL]\033[0m $*" >&2; exit 1; }

[[ "$(id -u)" != 0 ]] || die "run as the operator user, not root (sudo is used for the privileged steps)"
sudo true   # authenticate up front (prompts for the password on a normal host)

for mode in "${UNITS[@]}"; do
    state="$(systemctl show -p ActiveState --value "$mode-borg-backup.service")"
    [[ "$state" == inactive || "$state" == failed ]] \
        || die "$mode-borg-backup.service is $state; wait for it to finish"
done

if sudo test -f "$DST"; then
    info "$DST already installed; verifying instead of rebuilding"
else
    [[ -f "$SRC" ]] || die "$SRC not found (and $DST not installed)"
    n="$(grep -cFx "$ANCHOR" "$SRC" || true)"
    [[ "$n" == 1 ]] || die "expected exactly one line '$ANCHOR' in $SRC, found $n; not patching blind"
    grep -q "planetexpress/maintenance" "$SRC" && die "$SRC already mentions the marker; inspect it by hand"

    work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
    awk -v anchor="$ANCHOR" '
        { print }
        $0 == anchor {
            print ""
            print "# BEGIN Planet Express maintenance marker (T25)"
            print "# Core pauses scans and host mutations while this file exists, because this job tears"
            print "# every stack down. Root-owned directory: core can read the marker but never clear it."
            print "# Core ignores a marker older than 6 hours, so a killed run cannot wedge it."
            print "PE_MAINTENANCE_MARKER=/var/lib/planetexpress/maintenance"
            print "install -d -m 755 -o root -g root \"$(dirname \"$PE_MAINTENANCE_MARKER\")\""
            print "printf '"'"'{\"reason\": \"borg-backup %s\", \"started_at\": %s, \"pid\": %s}\\n'"'"' \"$MODE\" \"$(date +%s)\" \"$$\" > \"$PE_MAINTENANCE_MARKER\""
            print "trap '"'"'rm -f \"$PE_MAINTENANCE_MARKER\"'"'"' EXIT"
            print "trap '"'"'exit 143'"'"' TERM INT   # make the EXIT trap run when systemd stops the job"
            print "# END Planet Express maintenance marker (T25)"
        }' "$SRC" > "$work/patched.sh"
    bash -n "$work/patched.sh" || die "patched script fails bash -n"
    [[ "$(grep -c "planetexpress/maintenance" "$work/patched.sh")" == 1 ]] || die "marker block not inserted exactly once"
    # Byte-for-byte: drop exactly the inserted lines after the anchor and compare with the original.
    block_lines="$(awk -v anchor="$ANCHOR" '$0 == anchor { start = NR }
        start && $0 == "# END Planet Express maintenance marker (T25)" { print NR - start; exit }' "$work/patched.sh")"
    [[ "$block_lines" =~ ^[0-9]+$ ]] || die "could not measure the inserted block"
    awk -v anchor="$ANCHOR" -v n="$block_lines" 'skip > 0 { skip--; next } { print } $0 == anchor { skip = n }' \
        "$work/patched.sh" | cmp -s - "$SRC" || die "patched script differs from the original beyond the inserted block"

    info "installing $DST (root:root 755)"
    sudo install -o root -g root -m 755 "$work/patched.sh" "$DST"
fi

sudo install -d -o root -g root -m 755 "$(dirname "$MARKER")"

for mode in "${UNITS[@]}"; do
    dropin="/etc/systemd/system/$mode-borg-backup.service.d"
    info "pointing $mode-borg-backup.service at $DST"
    sudo install -d -m 755 "$dropin"
    printf '[Service]\n# T25/D32: root-owned copy; the old path was writable by non-root users.\nExecStart=\nExecStart=%s --%s\n' "$DST" "$mode" \
        | sudo tee "$dropin/planetexpress-interlock.conf" >/dev/null
done
sudo systemctl daemon-reload

for mode in "${UNITS[@]}"; do
    exec_line="$(systemctl show -p ExecStart --value "$mode-borg-backup.service")"
    grep -qF "path=$DST ;" <<<"$exec_line" || die "$mode-borg-backup.service does not run $DST: $exec_line"
    grep -qF -- "--$mode" <<<"$exec_line" || die "$mode-borg-backup.service lost its --$mode argument"
done
ok "both units run $DST"

if [[ -f "$SRC" ]]; then
    mv "$SRC" "$RETIRED"
    chmod 444 "$RETIRED"
    ok "old copy retired to $RETIRED (read-only, no longer run by anything)"
fi

perms="$(stat -c %a "$APPS_DIR")"
if (( 8#$perms & 8#002 )); then
    sudo chmod o-w "$APPS_DIR"
    ok "$APPS_DIR: $perms -> $(stat -c %a "$APPS_DIR") (no longer world-writable)"
fi

[[ "$(sudo stat -c '%U:%G %a' "$DST")" == "root:root 755" ]] || die "$DST ownership/mode wrong"
[[ "$(stat -c '%U:%G %a' "$(dirname "$MARKER")")" == "root:root 755" ]] || die "marker dir ownership/mode wrong"
ok "installed. Next backup run writes $MARKER for its duration."
echo "     Dry run to watch it:  sudo systemctl start weekly-borg-backup.service  (tears stacks down!)"
