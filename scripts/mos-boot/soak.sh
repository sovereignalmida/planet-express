#!/bin/sh
# Soak monitor for a Planet Express host without systemd: appends one CSV row every INTERVAL
# seconds to $SOAK_DIR/soak.csv. A drop in uptime_s marks a reboot; a core_pid change marks a restart.
SOAK_DIR=${SOAK_DIR:-/mnt/data/pe/soak}
INTERVAL=${INTERVAL:-300}
CORE_PID_FILE=/var/run/casa-planetexpress.pid
CORE_LOG=/var/log/casa-planetexpress/casa-planetexpress.log
mkdir -p "$SOAK_DIR"
CSV="$SOAK_DIR/soak.csv"
[ -s "$CSV" ] || echo "ts,uptime_s,core_pid,core_rss_kb,core_fds,dash_alive,dash_http,rpc_sock,containers_up,containers_all,root_used_pct,data_used_pct,log_errors,log_warnings" > "$CSV"

while :; do
    pid=$(cat "$CORE_PID_FILE" 2>/dev/null)
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
        rss=$(awk '/VmRSS/ {print $2}' "/proc/$pid/status")
        fds=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
    else
        pid=0; rss=0; fds=0
    fi
    dpid=$(cat /var/run/casa-dashboard.pid 2>/dev/null)
    if [ -n "$dpid" ] && [ -d "/proc/$dpid" ]; then dalive=1; else dalive=0; fi
    http=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://localhost:8420/login)
    [ -S /run/planetexpress/core.sock ] && sock=1 || sock=0
    up=$(docker ps -q 2>/dev/null | wc -l)
    all=$(docker ps -aq 2>/dev/null | wc -l)
    rootp=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
    datap=$(df /mnt/data | awk 'NR==2 {gsub("%",""); print $5}')
    errs=$(grep -c ' ERROR ' "$CORE_LOG" 2>/dev/null)
    warns=$(grep -c ' WARNING ' "$CORE_LOG" 2>/dev/null)
    echo "$(date -Iseconds),$(cut -d. -f1 /proc/uptime),$pid,$rss,$fds,$dalive,$http,$sock,$up,$all,$rootp,$datap,${errs:-0},${warns:-0}" >> "$CSV"
    sleep "$INTERVAL"
done
