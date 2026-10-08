#!/bin/sh
### BEGIN INIT INFO
# Provides:          casa-planetexpress
# Required-Start:    $remote_fs $network
# Required-Stop:     $remote_fs $network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Planet Express core service (v3)
### END INIT INFO

set -e

PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
DESC="Planet Express core service"
NAME="casa-planetexpress"
PIDFILE=/var/run/$NAME.pid
LOGDIR=/var/log/casa-planetexpress
LOGFILE=$LOGDIR/$NAME.log
DAEMON_USER="root"
DAEMON_DIR="/root/planet-express"

PYTHON="python3"
ENV_FILE="/etc/planetexpress.env"
WEB_USER="planetexpress-web"
RPC_GROUP="planetexpress-rpc"

# Read config file if it exists. Anything it assigns (PYTHON, DAEMON_DIR, ENV_FILE, CASA_*) is
# exported to the daemon; on MOS point these at persistent storage, since / is RAM.
if [ -r /etc/default/$NAME ]; then
    set -a
    . /etc/default/$NAME
    set +a
fi

# Ensure log directory exists
mkdir -p "$LOGDIR"
chmod 755 "$LOGDIR"

# Source LSB init functions
. /lib/lsb/init-functions

start() {
    log_daemon_msg "Starting $DESC" "$NAME"

    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            log_progress_msg "already running"
            log_end_msg 0
            return 0
        fi
    fi

    cd "$DAEMON_DIR"

    # The dashboard talks to core over a root-owned socket gated by this user and group. MOS keeps
    # / in RAM, so they have to be recreated on every boot.
    getent group "$RPC_GROUP" >/dev/null || groupadd -r "$RPC_GROUP"
    getent group "$WEB_USER" >/dev/null || groupadd -r "$WEB_USER"
    id "$WEB_USER" >/dev/null 2>&1 || \
        useradd -r -g "$WEB_USER" -G "$RPC_GROUP" -s /bin/false -M "$WEB_USER"
    mkdir -p /run/planetexpress

    # envfile_exec.py reads the env file the way systemd does; `. file` would expand `$`.
    nohup "$PYTHON" scripts/envfile_exec.py "$ENV_FILE" -- "$PYTHON" casa_farnsworth.py \
        > "$LOGFILE" 2>&1 < /dev/null &
    PID=$!
    echo "$PID" > "$PIDFILE"

    sleep 1

    if kill -0 "$PID" 2>/dev/null; then
        log_end_msg 0
        return 0
    else
        log_end_msg 1
        return 1
    fi
}

stop() {
    log_daemon_msg "Stopping $DESC" "$NAME"

    if [ ! -f "$PIDFILE" ]; then
        log_progress_msg "not running"
        log_end_msg 0
        return 0
    fi

    PID=$(cat "$PIDFILE")

    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        log_progress_msg "not running"
        log_end_msg 0
        return 0
    fi

    # Graceful shutdown
    kill -TERM "$PID" 2>/dev/null || true

    # Wait up to 10 seconds for graceful shutdown
    for i in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            rm -f "$PIDFILE"
            log_end_msg 0
            return 0
        fi
        sleep 1
    done

    # Force kill if still running
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PIDFILE"

    log_end_msg 0
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if [ ! -f "$PIDFILE" ]; then
        echo "$NAME is not running (no pidfile)"
        exit 3
    fi

    PID=$(cat "$PIDFILE")

    if kill -0 "$PID" 2>/dev/null; then
        echo "$NAME is running (pid $PID)"
        exit 0
    else
        echo "$NAME is not running (stale pidfile)"
        exit 1
    fi
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart|force-reload)
        restart
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: /etc/init.d/$NAME {start|stop|restart|force-reload|status}"
        exit 1
        ;;
esac

exit 0
