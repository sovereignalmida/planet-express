#!/usr/bin/env bash
# Run inside the throwaway homelab guest after vm.sh push (both units restarted on the new code).
# Exercises T35 end to end over HTTP with a temporary Airlock operator:
#   1. root-owned default config (pre-D29 install): apply fails safe with write_failed
#   2. the setup wizard's D29 ownership commands, then web_access.py re-grants the dashboard ACL
#   3. locked (sensitive / host wiring), conflict, and an editable apply that re-execs core in
#      place (same MainPID), comes back on RPC with the new sha, and records config.applied
#   4. PE_ALLOW_SENSITIVE_CONFIG_EDITS=1 unlocks a sensitive field
# Restores the original config text, env file and operator list afterwards.
set -euo pipefail

REPO=/home/casaroot/planet-express
BASE=http://127.0.0.1:8420
CONFIG=/etc/planetexpress/config.yaml
PASS='t35 rehearsal passphrase'
T=/tmp/t35
mkdir -p "$T"; chmod 700 "$T"
PY="$REPO/venv/bin/python"

sudo cp /etc/planetexpress-dashboard.env "$T/dashboard.env.backup"
sudo cp /etc/planetexpress.env "$T/core.env.backup"
sudo cp -p "$CONFIG" "$T/config.yaml.backup"
ORIG_OWNER=$(stat -c %U:%G "$CONFIG"); ORIG_MODE=$(stat -c %a "$CONFIG")
ORIG_DIR_OWNER=$(stat -c %U:%G "$(dirname "$CONFIG")"); ORIG_DIR_MODE=$(stat -c %a "$(dirname "$CONFIG")")

cleanup() {
    set +e
    echo "--- restoring"
    sudo install -m 600 -o root -g root "$T/core.env.backup" /etc/planetexpress.env
    sudo install -m 600 -o root -g root "$T/dashboard.env.backup" /etc/planetexpress-dashboard.env
    sudo cp "$T/config.yaml.backup" "$CONFIG"
    sudo chown "$ORIG_OWNER" "$CONFIG"; sudo chmod "$ORIG_MODE" "$CONFIG"
    sudo chown "$ORIG_DIR_OWNER" "$(dirname "$CONFIG")"; sudo chmod "$ORIG_DIR_MODE" "$(dirname "$CONFIG")"
    env CASA_CONFIG="$CONFIG" "$PY" "$REPO/scripts/web_access.py" >/dev/null
    sudo systemctl reset-failed casa-planetexpress casa-dashboard
    sudo systemctl restart casa-planetexpress casa-dashboard
    sleep 3
    systemctl is-active casa-planetexpress casa-dashboard
    sudo rm -rf "$T"
}
trap cleanup EXIT

# ── temporary operator + login ──────────────────────────────────────────────
cd "$REPO"
"$PY" - <<'PY'
from scripts import dashboard_operators as ops
import web_auth
text = ops._read_env()
values = ops.parse_env(text)
for name in list(ops.list_operators(values)):
    values = ops.apply_operator_change(values, "remove", name)
secret = web_auth.new_totp_secret()
values = ops.apply_operator_change(values, "add", "t35", passphrase="t35 rehearsal passphrase", totp_secret=secret)
ops._write_env(ops.render_env(text, values))
open("/tmp/t35/secret", "w").write(secret)
PY
sudo systemctl restart casa-dashboard
for _ in $(seq 1 30); do curl -fsS "$BASE/login" -o /dev/null 2>/dev/null && break; sleep 1; done

login() {
    rm -f "$T/cookies"
    curl -fsS -c "$T/cookies" "$BASE/login" -o "$T/login.html"
    local csrf code status step
    csrf=$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$T/login.html" | head -1)
    # One login per TOTP step: the replay guard refuses a second use of the same step.
    # Always start in a fresh step: an earlier run may have used this one for operator t35.
    step=$(( $(date +%s) / 30 ))
    until [[ $(( $(date +%s) / 30 )) -gt $step ]]; do sleep 1; done
    code=$("$PY" -c 'import time,web_auth; print(web_auth.totp_at(open("/tmp/t35/secret").read(), int(time.time() // 30)))')
    status=$(curl -sS -o /dev/null -w '%{http_code}' -b "$T/cookies" -c "$T/cookies" \
        --data-urlencode "csrf_token=$csrf" --data-urlencode "passphrase=$PASS" \
        --data-urlencode "code=$code" --data-urlencode 'trust=on' "$BASE/login")
    [[ "$status" == 302 ]] || { echo "login failed: HTTP $status"; exit 1; }
    CSRF=$(curl -fsS -b "$T/cookies" "$BASE/" | sed -n 's/.*name="csrf-token" content="\([^"]*\)".*/\1/p' | head -1)
}
login

field() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d[sys.argv[2]] if not isinstance(d[sys.argv[2]], list) else ",".join(d[sys.argv[2]]))' "$@"; }
get_config() { curl -fsS -b "$T/cookies" "$BASE/api/config" -o "$T/config.json"; }
# draft <out> <python expression over `d` (the parsed config)> -- appends a comment so text differs
draft() {
    "$PY" - "$T/config.json" "$1" "$2" <<'PY'
import json, sys, yaml
live = json.load(open(sys.argv[1]))["text"]
d = yaml.safe_load(live)
exec(sys.argv[3])
open(sys.argv[2], "w").write(yaml.safe_dump(d, sort_keys=False))
PY
}
apply() {  # apply <draft file> <base sha> -> writes $T/apply.json
    curl -sS -b "$T/cookies" --data-urlencode "csrf_token=$CSRF" \
        --data-urlencode "text@$1" --data-urlencode "base_sha256=$2" \
        "$BASE/api/config/apply" -o "$T/apply.json" -w '%{http_code}\n'
}
expect_status() {
    local got; got=$(field "$T/apply.json" status)
    [[ "$got" == "$1" ]] || { echo "expected $1, got:"; cat "$T/apply.json"; exit 1; }
    echo "  $1: changed=[$(field "$T/apply.json" changed_fields)] locked=[$(field "$T/apply.json" locked_fields)] $(field "$T/apply.json" reason)"
}
wait_core_back() {  # wait until the RUNNING core reports it loaded sha $1 (not just the file)
    for _ in $(seq 1 60); do
        if curl -fsS -b "$T/cookies" "$BASE/api/config" -o "$T/config.json" 2>/dev/null \
           && [[ "$(field "$T/config.json" loaded_sha256)" == "$1" ]]; then return 0; fi
        sleep 1
    done
    echo "core did not come back with sha $1"; exit 1
}

get_config
SHA0=$(field "$T/config.json" sha256)
[[ "$(field "$T/config.json" loaded_sha256)" == "$SHA0" ]] || { echo "loaded_sha256 != file sha at start"; exit 1; }
echo "sensitive_edits_enabled=$(field "$T/config.json" sensitive_edits_enabled) sha=${SHA0:0:12}"
[[ "$(field "$T/config.json" sensitive_edits_enabled)" == False ]]

echo "== 1. pre-D29 root-owned config: editable apply fails safe"
draft "$T/editable.yaml" 'd["paused_containers"] = sorted(set(d.get("paused_containers") or []) | {"t35-rehearsal"})'
apply "$T/editable.yaml" "$SHA0"
expect_status write_failed
sudo cmp -s "$CONFIG" "$T/config.yaml.backup" && echo "  file unchanged"

echo "== 2. setup wizard D29 ownership, then web_access.py"
sudo env CASA_CONFIG="$CONFIG" PYTHONPATH="$REPO" "$PY" - "$CONFIG" <<'PY'
import subprocess, sys
from pathlib import Path
from scripts.setup_wizard import _config_directory_command, _config_ownership_commands
target = Path(sys.argv[1])
for command in [_config_directory_command(target, "casaroot", "casaroot"),
                *_config_ownership_commands(target, "casaroot", "casaroot")]:
    subprocess.run(command[1:], check=True)   # already root; drop the leading "sudo"
PY
env CASA_CONFIG="$CONFIG" "$PY" "$REPO/scripts/web_access.py" >/dev/null
stat -c '  %U:%G %a %n' "$(dirname "$CONFIG")" "$CONFIG"
getfacl -p "$CONFIG" 2>/dev/null | grep -q '^user:planetexpress-web:r' && echo "  dashboard read ACL present"
sudo -u planetexpress-web cat "$CONFIG" >/dev/null && echo "  planetexpress-web can read the config"

echo "== 3a. sensitive field is locked with the switch off"
draft "$T/sensitive.yaml" 'd["forbidden_stacks"] = sorted(set(d.get("forbidden_stacks") or []) | {"t35-rehearsal"})'
apply "$T/sensitive.yaml" "$SHA0"; expect_status locked
echo "== 3b. host wiring is locked"
draft "$T/wiring.yaml" 'd["lan_only_domain"] = "t35.invalid"'
apply "$T/wiring.yaml" "$SHA0"; expect_status locked
echo "== 3c. stale base sha is a conflict"
apply "$T/editable.yaml" "$(printf '0%.0s' $(seq 1 64))"; expect_status conflict
sudo cmp -s "$CONFIG" "$T/config.yaml.backup" && echo "  file unchanged after all refusals"

workers() { pgrep -P "$(systemctl show casa-dashboard -p MainPID --value)" | sort | tr '\n' ' '; }
echo "== 3d. editable apply re-execs core in place"
PID_BEFORE=$(systemctl show casa-planetexpress -p MainPID --value)
DASH_MAIN=$(systemctl show casa-dashboard -p MainPID --value); WORKERS_BEFORE=$(workers)
HTTP=$(apply "$T/editable.yaml" "$SHA0"); echo "  HTTP $HTTP"
expect_status activating
SHA1=$(sha256sum "$T/editable.yaml" | cut -d' ' -f1)
wait_core_back "$SHA1"
PID_AFTER=$(systemctl show casa-planetexpress -p MainPID --value)
echo "  MainPID $PID_BEFORE -> $PID_AFTER; NRestarts=$(systemctl show casa-planetexpress -p NRestarts --value)"
[[ "$PID_BEFORE" == "$PID_AFTER" ]] || { echo "core was restarted, not re-exec'd"; exit 1; }
echo "  running core now reports loaded_sha256=${SHA1:0:12} (activation proven, not just the write)"
# Any request lets a dashboard worker notice the new file and SIGHUP its gunicorn master.
for _ in $(seq 1 20); do
    curl -fsS -o /dev/null "$BASE/login" || true
    [[ "$(workers)" != "$WORKERS_BEFORE" && -n "$(workers)" ]] && break
    sleep 1
done
echo "  dashboard workers: [$WORKERS_BEFORE] -> [$(workers)]; master $DASH_MAIN -> $(systemctl show casa-dashboard -p MainPID --value)"
[[ "$(workers)" != "$WORKERS_BEFORE" ]] || { echo "dashboard workers were not reloaded"; exit 1; }
[[ "$(systemctl show casa-dashboard -p MainPID --value)" == "$DASH_MAIN" ]] || { echo "dashboard master restarted"; exit 1; }
curl -fsS -b "$T/cookies" "$BASE/api/config" -o /dev/null && echo "  dashboard session survived the worker reload"
getfacl -p "$CONFIG" 2>/dev/null | grep -q '^user:planetexpress-web:r' && echo "  dashboard read ACL survived the atomic replace"
stat -c '  %U:%G %a %n' "$CONFIG"
sudo env CASA_CONFIG="$CONFIG" PYTHONPATH="$REPO" "$PY" - <<'PY'
import sqlite3, config, json
c = sqlite3.connect(f"file:{config.ACTIONS_DB}?mode=ro", uri=True)
for kind, payload in c.execute("select kind, payload from events where kind like 'config.%' order by ts desc limit 6"):
    print("  event", kind, json.loads(payload).get("status", ""), json.loads(payload).get("changed_fields"))
PY

echo "== 4. switch on: sensitive field applies"
echo 'PE_ALLOW_SENSITIVE_CONFIG_EDITS=1' | sudo tee -a /etc/planetexpress.env >/dev/null
sudo systemctl restart casa-planetexpress
sleep 5
get_config
[[ "$(field "$T/config.json" sensitive_edits_enabled)" == True ]] && echo "  sensitive_edits_enabled=True"
draft "$T/sensitive2.yaml" 'd["forbidden_stacks"] = sorted(set(d.get("forbidden_stacks") or []) | {"t35-rehearsal"})'
apply "$T/sensitive2.yaml" "$(field "$T/config.json" sha256)"; expect_status activating
wait_core_back "$(sha256sum "$T/sensitive2.yaml" | cut -d' ' -f1)"

echo "== 5. restore the original text through the API (switch still on)"
sudo cp "$T/config.yaml.backup" "$T/original.yaml"; sudo chown casaroot "$T/original.yaml"
apply "$T/original.yaml" "$(field "$T/config.json" sha256)"; expect_status activating
wait_core_back "$SHA0"
echo "T35 rehearsal passed"
