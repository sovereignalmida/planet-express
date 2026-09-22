#!/usr/bin/env bash
# Run inside the throwaway homelab guest after vm.sh push (both units restarted on the new code, core
# idle: no scan running and no legacy plan awaiting approval). Drives T37's typed stack actions through
# the dashboard's RPC as planetexpress-web, exactly the calls Scruffy makes:
#   - compose.up_stack (R1) operator-direct: healthy stack passes; unhealthy and crash-loop fail
#   - compose.down_stack (R2) via proposal + approval, then up_stack brings it back (slow-start)
#   - compose.up_all (R2) via proposal + approval stops at the first failing stack
# Each execution is polled to a terminal status and compared with real `docker ps` state.
set -euo pipefail
REPO=/home/casaroot/planet-express

rpc() {  # rpc <method> <json params>
    sudo -u planetexpress-web env CASA_CONFIG=/etc/planetexpress/config.yaml PYTHONPATH="$REPO" \
        "$REPO/venv/bin/python" -c '
import json, sys, config
from planet_express.integrations.rpc import call
print(json.dumps(call(config.RPC_SOCKET, sys.argv[1], json.loads(sys.argv[2]), timeout=10)))' "$1" "$2"
}
field() { "$REPO/venv/bin/python" -c 'import json,sys; d=json.loads(sys.argv[1]); [d := d[k] for k in sys.argv[2:]]; print(d)' "$@"; }

wait_execution() {  # wait_execution <id> -> prints "status|reason"
    for _ in $(seq 1 120); do
        out=$(rpc execution.get_status "{\"execution_id\": \"$1\"}")
        status=$(field "$out" result status)
        case "$status" in passed|failed|interrupted)
            echo "$status|$(field "$out" result reason)|$(field "$out" result summary)"; return 0;; esac
        sleep 3
    done
    echo "timeout|execution $1 did not finish"; return 1
}
direct() {  # direct <action> <stack>; a scheduled scan holding the lock is retried, not a failure
    for _ in $(seq 1 40); do
        out=$(rpc action.request "{\"action\": \"$1\", \"stack\": \"$2\", \"operator\": \"t37\"}")
        outcome=$(field "$out" result outcome)
        [[ "$outcome" == busy ]] || break
        sleep 5
    done
    [[ "$outcome" == started ]] || { echo "  $1 $2: $outcome — $(field "$out" result message)"; return 1; }
    wait_execution "$(field "$out" result execution_id)"
}
proposed() {  # proposed <action> <stack> -> approve via approval.decide, wait
    out=$(rpc proposal.create "{\"action\": \"$1\", \"stack\": \"$2\", \"service\": \"-\", \"requested_by\": \"t37\"}")
    [[ "$(field "$out" result ok)" == True ]] || { echo "  propose refused: $out"; return 1; }
    id=$(field "$out" result approval_id)
    approval=$(rpc approval.get "{\"approval_id\": \"$id\"}")
    echo "  proposed $1 $2: approval $id risk=$(field "$approval" result risk) status=$(field "$approval" result status) summary='$(field "$approval" result summary)'" >&2
    for _ in $(seq 1 40); do
        out=$(rpc approval.decide "{\"approval_id\": \"$id\", \"approve\": true, \"decided_by\": \"t37\"}")
        [[ "$(field "$out" result outcome)" == busy ]] || break
        sleep 5
    done
    [[ "$(field "$out" result outcome)" == started ]] || { echo "  decide: $out"; return 1; }
    wait_execution "$(field "$out" result execution_id)"
}
containers() { docker ps -a --filter "label=com.docker.compose.project=$1" --format '{{.Names}}:{{.Status}}' | tr '\n' ' '; }

echo "== state: $(grep -o '"state": "[a-z_]*"' "$REPO/state/run_status.json")"

echo "== 1. up_stack healthy (R1, operator-direct) — already up, must verify good"
r=$(direct compose.up_stack healthy); echo "  $r"; [[ "$r" == passed* ]]

echo "== 2. up_stack unhealthy — must fail on the healthcheck"
r=$(direct compose.up_stack unhealthy || true); echo "  $r"; [[ "$r" == failed* ]]

echo "== 3. up_stack crash-loop — must fail (restarting / restarted)"
r=$(direct compose.up_stack crash-loop || true); echo "  $r"; [[ "$r" == failed* ]]

echo "== 4. down_stack slow-start (R2) needs approval; approve and verify gone"
echo "  before: $(containers slow-start)"
r=$(direct compose.down_stack slow-start 2>&1 || true); echo "  direct attempt: $r"
r=$(proposed compose.down_stack slow-start); echo "  $r"; [[ "$r" == passed* ]]
echo "  after: [$(containers slow-start)]"
[[ -z "$(containers slow-start)" ]]

echo "== 5. up_stack slow-start brings it back and waits for its slow healthcheck"
r=$(direct compose.up_stack slow-start); echo "  $r"; [[ "$r" == passed* ]]
echo "  after: $(containers slow-start)"

echo "== 6. up_all (R2) via approval stops at the first failing stack"
r=$(proposed compose.up_all all || true); echo "  $r"; [[ "$r" == failed* && "$r" == *"not attempted"* ]]

echo "== 7. ingress guard: down_stack on the 'network' stack is refused, pointing at the R3 action"
out=$(rpc proposal.create '{"action": "compose.down_stack", "stack": "network", "service": "-", "requested_by": "t37"}')
echo "  $(field "$out" result reason)"

echo "== audit"
sudo env CASA_CONFIG=/etc/planetexpress/config.yaml PYTHONPATH="$REPO" "$REPO/venv/bin/python" - <<'PY'
import sqlite3, config
c = sqlite3.connect(f"file:{config.ACTIONS_DB}?mode=ro", uri=True)
for row in c.execute("select a.action, a.risk, a.requested_via, a.status, e.status from approvals a "
                     "left join executions e on e.approval_id = a.id where a.requested_by = 't37' "
                     "order by a.created_at"):
    print("  ", *row)
PY
echo "T37 rehearsal passed"
