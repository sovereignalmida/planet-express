#!/bin/bash
# Set up test fixtures on MOS VM before deploying v3
# Creates simple init.d services that we can use to validate v3 provider

set -e

echo "========================================"
echo "MOS Test Fixtures Setup"
echo "========================================"
echo ""

# Create test service 1: dummy service that always runs
echo "1. Creating test-dummy service..."
sudo tee /etc/init.d/test-dummy > /dev/null << 'EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          test-dummy
# Required-Start:    $remote_fs
# Required-Stop:     $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Test dummy service for v3 provider validation
### END INIT INFO

case "$1" in
  start)
    echo "Starting test-dummy service"
    touch /var/run/test-dummy.pid
    exit 0
    ;;
  stop)
    echo "Stopping test-dummy service"
    rm -f /var/run/test-dummy.pid
    exit 0
    ;;
  restart)
    $0 stop
    $0 start
    exit 0
    ;;
  status)
    if [ -f /var/run/test-dummy.pid ]; then
      echo "test-dummy is running"
      exit 0
    else
      echo "test-dummy is not running"
      exit 3
    fi
    ;;
  *)
    echo "Usage: /etc/init.d/test-dummy {start|stop|restart|status}"
    exit 1
    ;;
esac
EOF

sudo chmod +x /etc/init.d/test-dummy
echo "✓ test-dummy created"

# Create test service 2: service that can fail
echo "2. Creating test-failing service..."
sudo tee /etc/init.d/test-failing > /dev/null << 'EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          test-failing
# Required-Start:    $remote_fs
# Required-Stop:     $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Test service that can be made to fail for v3 testing
### END INIT INFO

case "$1" in
  start)
    echo "Starting test-failing service"
    rm -f /var/run/test-failing.failed
    exit 0
    ;;
  stop)
    echo "Stopping test-failing service"
    exit 0
    ;;
  restart)
    $0 stop
    $0 start
    exit 0
    ;;
  status)
    if [ -f /var/run/test-failing.failed ]; then
      echo "test-failing has failed (marked)"
      exit 1
    else
      echo "test-failing is running"
      exit 0
    fi
    ;;
  *)
    echo "Usage: /etc/init.d/test-failing {start|stop|restart|status}"
    exit 1
    ;;
esac
EOF

sudo chmod +x /etc/init.d/test-failing
echo "✓ test-failing created"

# Create test service 3: slow-starting service
echo "3. Creating test-slow-start service..."
sudo tee /etc/init.d/test-slow-start > /dev/null << 'EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          test-slow-start
# Required-Start:    $remote_fs
# Required-Stop:     $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Test service with slow startup for v3 testing
### END INIT INFO

case "$1" in
  start)
    echo "Starting test-slow-start service (waiting 2 seconds)..."
    sleep 2
    touch /var/run/test-slow-start.pid
    exit 0
    ;;
  stop)
    echo "Stopping test-slow-start service"
    rm -f /var/run/test-slow-start.pid
    exit 0
    ;;
  restart)
    $0 stop
    $0 start
    exit 0
    ;;
  status)
    if [ -f /var/run/test-slow-start.pid ]; then
      echo "test-slow-start is running"
      exit 0
    else
      echo "test-slow-start is not running"
      exit 3
    fi
    ;;
  *)
    echo "Usage: /etc/init.d/test-slow-start {start|stop|restart|status}"
    exit 1
    ;;
esac
EOF

sudo chmod +x /etc/init.d/test-slow-start
echo "✓ test-slow-start created"

echo ""
echo "========================================"
echo "Starting test services"
echo "========================================"
echo ""

sudo service test-dummy start
echo "✓ test-dummy started"

sudo service test-failing start
echo "✓ test-failing started"

sudo service test-slow-start start
echo "✓ test-slow-start started"

echo ""
echo "========================================"
echo "Verifying services"
echo "========================================"
echo ""

echo "test-dummy status:"
sudo service test-dummy status && echo "  ✓ running" || echo "  ✗ not running"

echo "test-failing status:"
sudo service test-failing status && echo "  ✓ running" || echo "  ✗ not running"

echo "test-slow-start status:"
sudo service test-slow-start status && echo "  ✓ running" || echo "  ✗ not running"

echo ""
echo "========================================"
echo "Test Fixtures Ready"
echo "========================================"
echo ""
echo "Services available for v3 provider testing:"
echo "  1. test-dummy    - basic start/stop/status"
echo "  2. test-failing  - can be made to fail (touch /var/run/test-failing.failed)"
echo "  3. test-slow-start - validates timeout handling"
echo ""
echo "After deploying v3, test with:"
echo "  python3 << 'PYEOF'"
echo "  import config"
echo "  p = config.get_host_control()"
echo "  print(p.is_service_running('test-dummy'))"
echo "  result = p.restart_service('test-dummy')"
echo "  print(f'Restart result: {result}')"
echo "  PYEOF"
