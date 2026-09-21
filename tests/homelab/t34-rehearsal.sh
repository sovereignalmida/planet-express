#!/usr/bin/env bash
# Run inside the throwaway homelab guest after vm.sh push and a full scan. Installs a
# temporary Airlock operator, renders the Overview over HTTP and checks the T34 stack
# cards against the real fixture snapshot, then restores the dashboard env.
# KEEP=1 leaves the temporary operator installed (for a manual browser check) and
# prints the restore command instead of restoring.
set -euo pipefail

REPO=/home/casaroot/planet-express
BASE=http://127.0.0.1:8420
PASS='t34 rehearsal passphrase'
BACKUP=/tmp/t34-dashboard.env.backup
COOKIE=/tmp/t34-cookies.txt
LOGIN=/tmp/t34-login.html
PAGE=/tmp/t34-overview.html

restore() {
    sudo install -m 600 -o root -g root "$BACKUP" /etc/planetexpress-dashboard.env
    sudo systemctl reset-failed casa-dashboard
    sudo systemctl restart casa-dashboard
    sudo rm -f "$BACKUP"
    rm -f "$COOKIE" "$LOGIN" "$PAGE" /tmp/t34-monitor.json /tmp/t34-secret
}
if [[ "${1:-}" == restore ]]; then restore; echo restored; exit 0; fi

cleanup() {
    if [[ "${KEEP:-0}" == 1 ]]; then
        rm -f "$COOKIE" "$LOGIN" "$PAGE" /tmp/t34-monitor.json
        echo "operator t34 left installed; restore with: bash $0 restore"
    else
        restore
    fi
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
    values, "add", "t34", passphrase="t34 rehearsal passphrase", totp_secret=secret
)
ops._write_env(ops.render_env(text, values))
with open("/tmp/t34-secret", "w", encoding="utf-8") as stream:
    stream.write(secret)
PY
chmod 600 /tmp/t34-secret
sudo systemctl restart casa-dashboard
for _ in $(seq 1 30); do
    curl -fsS "$BASE/login" -o /dev/null 2>/dev/null && break
    sleep 1
done

curl -fsS -c "$COOKIE" "$BASE/login" -o "$LOGIN"
CSRF=$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$LOGIN" | head -1)
CODE=$(./venv/bin/python -c 'import time,web_auth; print(web_auth.totp_at(open("/tmp/t34-secret").read(), int(time.time() // 30)))')
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE" -c "$COOKIE" \
    --data-urlencode "csrf_token=$CSRF" --data-urlencode "passphrase=$PASS" \
    --data-urlencode "code=$CODE" --data-urlencode 'trust=on' "$BASE/login")
[[ "$STATUS" == 302 ]] || { echo "login failed: HTTP $STATUS"; exit 1; }

curl -fsS -b "$COOKIE" "$BASE/" -o "$PAGE"

# The snapshot the page was rendered from, read as root (state/ is core-only).
MONITOR=/tmp/t34-monitor.json
sudo cat "$REPO/state/latest_monitor.json" > "$MONITOR"
./venv/bin/python - "$PAGE" "$MONITOR" <<'PY'
import html, json, re, sys

monitor = json.load(open(sys.argv[2], encoding="utf-8"))
page = open(sys.argv[1], encoding="utf-8").read()
panel = page[page.index('aria-label="Services"'):page.index("<!-- Hull Diagnostics -->")]

assert monitor["mode"] == "full", monitor["mode"]
names = re.findall(r'data-stack-name="([^"]+)"', panel)
expected = {s["stack"] for s in monitor["stack_completeness"]}
assert set(names) == expected, (names, expected)
assert "running(healthy)" not in panel, "a chip rendered running(healthy)"

levels = {name: level for level, name in re.findall(
    r'class="pe-card pe-stack-card (\w+)[^"]*"\s+data-stack-name="([^"]+)"', panel)}
rank = {"crit": 0, "warn": 1, "idle": 2, "ok": 3}
order = [rank[levels[n]] for n in names]
assert order == sorted(order), f"not worst-first: {list(zip(names, order))}"

services = {(s["stack"], svc) for s in monitor["stack_completeness"] for svc in s.get("services", {})}
for stack, svc in services:
    assert f'href="/containers/{stack}/{svc}"' in panel, f"missing link {stack}/{svc}"

bad = [n for n in names if levels[n] != "ok"]
rail = 'data-services-rail' in panel
assert rail == bool(bad), (rail, bad)
if any(levels[n] == "crit" for n in bad):
    assert "NEEDS YOU NOW" in panel
print("stacks (worst first):", ", ".join(f"{n}={levels[n]}" for n in names))
rail_text = re.search(r'data-services-rail>(.*?)</div>', panel, re.S)
if rail_text:
    print("rail:", " ".join(html.unescape(re.sub(r"<[^>]+>", " ", rail_text.group(1))).split()))
summary = re.search(r'pe-services-summary">([^<]+)<', panel).group(1)
print("summary:", html.unescape(summary))
PY

for path in /static/dashboard.js /static/cockpit.css; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE" "$BASE$path")
    [[ "$code" == 200 ]] || { echo "$path HTTP $code"; exit 1; }
done
for path in /containers/crash-loop/worker /containers/unhealthy/web /containers/healthy/web; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE" "$BASE$path")
    [[ "$code" == 200 ]] || { echo "$path HTTP $code"; exit 1; }
done
echo "T34 rehearsal passed"
if [[ "${KEEP:-0}" == 1 ]]; then
    echo "passphrase: $PASS"
    echo "totp secret: $(cat /tmp/t34-secret)"
fi
