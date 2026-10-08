#!/bin/sh
### BEGIN INIT INFO
# Provides:          casa-dashboard
# Required-Start:    $remote_fs $network casa-planetexpress
# Required-Stop:     $remote_fs $network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Planet Express read-only web dashboard (Scruffy)
### END INIT INFO

# sysvinit equivalent of systemd/casa-dashboard.service.template. Core (casa-planetexpress) creates
# the planetexpress-web user, the planetexpress-rpc group and /run/planetexpress; start it first.

PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
DESC="Planet Express dashboard"
NAME="casa-dashboard"
PIDFILE=/var/run/$NAME.pid
LOGDIR=/var/log/casa-planetexpress
LOGFILE=$LOGDIR/$NAME.log
DAEMON_DIR="/root/planet-express"
PYTHON="python3"
GUNICORN="gunicorn"
ENV_FILE="/etc/planetexpress-dashboard.env"
WEB_USER="planetexpress-web"
CASA_DASHBOARD_PORT=8420

if [ -r /etc/default/$NAME ]; then
    set -a
    . /etc/default/$NAME
    set +a
fi

. /lib/lsb/init-functions

start() {
    log_daemon_msg "Starting $DESC" "$NAME"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        log_progress_msg "already running"
        log_end_msg 0
        return 0
    fi
    id "$WEB_USER" >/dev/null 2>&1 || { log_progress_msg "$WEB_USER missing; start casa-planetexpress first"; log_end_msg 1; return 1; }
    mkdir -p "$LOGDIR"
    touch "$LOGFILE"
    chown "$WEB_USER" "$LOGFILE"
    cd "$DAEMON_DIR" || { log_end_msg 1; return 1; }

    # setpriv drops to the unprivileged user with the RPC group; envfile_exec.py loads the
    # root-only env file as root first, then execs gunicorn with those variables.
    setsid "$PYTHON" scripts/envfile_exec.py "$ENV_FILE" -- \
        setpriv --reuid="$WEB_USER" --regid="$WEB_USER" --groups=planetexpress-rpc,"$WEB_USER" \
        env PYTHONUNBUFFERED=1 CASA_DASHBOARD_PORT="$CASA_DASHBOARD_PORT" \
        "$GUNICORN" --workers 2 --threads 4 --bind "0.0.0.0:$CASA_DASHBOARD_PORT" \
        --worker-tmp-dir /dev/shm --no-control-socket --access-logfile - --error-logfile - \
        --pid "$PIDFILE" "casa_scruffy:create_app()" > "$LOGFILE" 2>&1 < /dev/null &
    sleep 3
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        log_end_msg 0
    else
        log_end_msg 1
        return 1
    fi
}

stop() {
    log_daemon_msg "Stopping $DESC" "$NAME"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill -TERM "$(cat "$PIDFILE")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$(cat "$PIDFILE")" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    log_end_msg 0
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "$NAME is running (pid $(cat "$PIDFILE"))"
        exit 0
    fi
    echo "$NAME is not running"
    exit 3
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart|force-reload) stop; sleep 1; start ;;
    status) status ;;
    *) echo "Usage: /etc/init.d/$NAME {start|stop|restart|force-reload|status}"; exit 1 ;;
esac
exit 0
