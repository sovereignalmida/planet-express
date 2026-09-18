#!/usr/bin/env bash
# Run inside the throwaway homelab guest after vm.sh push. Installs a temporary
# Airlock operator, exercises T31 over HTTP, and restores the dashboard env.
set -euo pipefail

REPO=/home/casaroot/planet-express
BASE=http://127.0.0.1:8420
PASS='t31 rehearsal passphrase'
BACKUP=/tmp/t31-dashboard.env.backup
COOKIE=/tmp/t31-cookies.txt
LOGIN=/tmp/t31-login.html

cleanup() {
    sudo install -m 600 -o root -g root "$BACKUP" /etc/planetexpress-dashboard.env
    sudo systemctl reset-failed casa-dashboard
    sudo systemctl restart casa-dashboard
    sudo rm -f "$BACKUP"
    rm -f "$COOKIE" "$LOGIN" /tmp/t31-secret /tmp/t31-proposal.json
}
sudo cp /etc/planetexpress-dashboard.env "$BACKUP"
trap cleanup EXIT

cd "$REPO"
./venv/bin/python - <<'PY'
from scripts import dashboard_operators as ops
import web_auth

text = ops._read_env()
values = ops.parse_env(text)
for name in list(ops.list_operators(values)):
    values = ops.apply_operator_change(values, "remove", name)
secret = web_auth.new_totp_secret()
values = ops.apply_operator_change(
    values, "add", "t31", passphrase="t31 rehearsal passphrase", totp_secret=secret
)
ops._write_env(ops.render_env(text, values))
with open("/tmp/t31-secret", "w", encoding="utf-8") as stream:
    stream.write(secret)
PY
chmod 600 /tmp/t31-secret
sudo systemctl restart casa-dashboard
for _ in $(seq 1 30); do
    curl -fsS "$BASE/login" -o /dev/null 2>/dev/null && break
    sleep 1
done
curl -fsS "$BASE/login" -o /dev/null

curl -fsS -c "$COOKIE" "$BASE/login" -o "$LOGIN"
CSRF=$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$LOGIN" | head -1)
CODE=$(./venv/bin/python -c 'import time,web_auth; print(web_auth.totp_at(open("/tmp/t31-secret").read(), int(time.time() // 30)))')
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE" -c "$COOKIE" \
    --data-urlencode "csrf_token=$CSRF" --data-urlencode "passphrase=$PASS" \
    --data-urlencode "code=$CODE" --data-urlencode 'trust=on' "$BASE/login")
[[ "$STATUS" == 302 ]] || { echo "login failed: HTTP $STATUS"; exit 1; }

CSRF=$(curl -fsS -b "$COOKIE" "$BASE/" | sed -n 's/.*name="csrf-token" content="\([^"]*\)".*/\1/p' | head -1)
STARTED_BEFORE=$(docker inspect fixture-healthy --format '{{.State.StartedAt}}')

proposal() {
    sudo -u planetexpress-web env CASA_CONFIG=/etc/planetexpress/config.yaml \
        PYTHONPATH="$REPO" "$REPO/venv/bin/python" -c \
        'import json,config; from planet_express.integrations.rpc import call; print(json.dumps(call(config.RPC_SOCKET,"proposal.create",{"action":"docker.restart_service","stack":"healthy","service":"web","requested_by":"t31"})["result"]))'
}

proposal > /tmp/t31-proposal.json
APPROVAL=$(./venv/bin/python -c 'import json; print(json.load(open("/tmp/t31-proposal.json"))["approval_id"])')
./venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); aid=sys.argv[1]; assert any(x["id"] == aid and x["status"] == "pending" for x in data["pending"])' \
    "$APPROVAL" < <(curl -fsS -b "$COOKIE" "$BASE/api/approvals")

BUSY_SEEN=0
for _ in $(seq 1 60); do
    DECISION=$(curl -fsS -b "$COOKIE" --data-urlencode "csrf_token=$CSRF" --data-urlencode 'approve=1' \
        "$BASE/api/approvals/$APPROVAL/decide")
    OUTCOME=$(./venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["outcome"])' <<<"$DECISION")
    if [[ "$OUTCOME" == started ]]; then break; fi
    if [[ "$OUTCOME" != busy ]]; then echo "unexpected approval outcome: $OUTCOME"; exit 1; fi
    BUSY_SEEN=1
    curl -fsS -b "$COOKIE" "$BASE/api/approvals/$APPROVAL" | \
        ./venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"] == "pending"'
    sleep 2
done
[[ "$OUTCOME" == started ]] || { echo "approval stayed busy"; exit 1; }
EXECUTION=$(./venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["execution_id"])' <<<"$DECISION")

for _ in $(seq 1 30); do
    EXECUTION_JSON=$(curl -fsS -b "$COOKIE" "$BASE/api/executions/$EXECUTION")
    EXECUTION_STATUS=$(./venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$EXECUTION_JSON")
    [[ "$EXECUTION_STATUS" == passed ]] && break
    [[ "$EXECUTION_STATUS" =~ ^(failed|interrupted)$ ]] && { echo "execution ended $EXECUTION_STATUS"; exit 1; }
    sleep 1
done
[[ "$EXECUTION_STATUS" == passed ]] || { echo "execution did not pass"; exit 1; }
./venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); assert data["reason"] == "healthy for 15s"; assert data["approval"]["decided_by"] == "t31"; assert data["capabilities"] == {"abortable":False,"rollbackable":False,"resumable":False}' <<<"$EXECUTION_JSON"
STARTED_AFTER=$(docker inspect fixture-healthy --format '{{.State.StartedAt}}')
[[ "$STARTED_BEFORE" != "$STARTED_AFTER" ]] || { echo "container StartedAt did not move"; exit 1; }

proposal > /tmp/t31-proposal.json
DENIAL=$(./venv/bin/python -c 'import json; print(json.load(open("/tmp/t31-proposal.json"))["approval_id"])')
curl -fsS -b "$COOKIE" --data-urlencode "csrf_token=$CSRF" --data-urlencode 'approve=0' \
    "$BASE/api/approvals/$DENIAL/decide" >/dev/null
curl -fsS -b "$COOKIE" "$BASE/api/approvals/$DENIAL" | \
    ./venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "denied"; assert data["decided_by"] == "t31"; assert data["denial_reason"] == "Denied by t31."'

echo "T31 VM rehearsal passed: pending card data, AUTHORISE -> passed, StartedAt moved, DENY attribution persisted (busy_seen=$BUSY_SEEN)"
