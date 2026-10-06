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

# Read config file if it exists
[ -r /etc/default/$NAME ] && . /etc/default/$NAME

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

    # Source environment variables (Telegram credentials, etc.) and export them
    if [ -r /etc/planetexpress.env ]; then
        set -a  # Mark new variables as exported
        . /etc/planetexpress.env
        set +a  # Turn off auto-export
    fi

    # Start PE in background with nohup to survive logout
    nohup python3 casa_farnsworth.py > "$LOGFILE" 2>&1 &
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
