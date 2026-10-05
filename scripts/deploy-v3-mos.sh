#!/bin/bash
# Deploy Planet Express v3 to MOS/sysvinit host
# Usage: ssh root@<mos-host> 'bash -s' < scripts/deploy-v3-mos.sh

set -e

echo "=========================================="
echo "Planet Express v3 Deployment (MOS Host)"
echo "=========================================="
echo ""

# Step 1: Pre-flight checks
echo "STEP 1: Pre-flight Checks"
echo "------"

echo -n "Checking sysvinit... "
[ -d /etc/init.d ] && echo "✓" || { echo "✗ /etc/init.d not found"; exit 1; }

echo -n "Checking sudo... "
sudo -n true 2>/dev/null && echo "✓ (NOPASSWD)" || echo "✓ (password)"

echo -n "Checking service command... "
command -v service >/dev/null && echo "✓" || { echo "✗"; exit 1; }

echo -n "Checking git... "
command -v git >/dev/null && echo "✓" || { echo "⚠ (will skip git update)"; }

echo ""
echo "STEP 2: Backup Current Config"
echo "------"

if [ -f /etc/planetexpress/config.yaml ]; then
    BACKUP="/etc/planetexpress/config.yaml.backup.$(date +%s)"
    sudo cp /etc/planetexpress/config.yaml "$BACKUP"
    echo "✓ Backed up config to: $BACKUP"
else
    echo "⚠ No existing config found"
fi

echo ""
echo "STEP 3: Stop Core Service"
echo "------"

echo "Stopping casa-planetexpress..."
if sudo service casa-planetexpress status >/dev/null 2>&1; then
    sudo service casa-planetexpress stop
    sleep 2
    echo "✓ Service stopped"
else
    echo "⚠ Service not currently running"
fi

echo ""
echo "STEP 4: Update Code (if in git repo)"
echo "------"

if [ -d .git ]; then
    echo "Git repository detected, updating..."
    git fetch origin
    git checkout main
    echo "✓ Code updated to latest main"
else
    echo "⚠ Not in a git repository; skipping git update"
fi

echo ""
echo "STEP 5: Install Dependencies"
echo "------"

echo "Installing/updating Python dependencies..."
if [ -d .venv ]; then
    .venv/bin/pip install -e . -q
    echo "✓ Dependencies installed"
else
    echo "⚠ No venv found; using system python"
    pip install -e . -q
fi

echo ""
echo "STEP 6: Update Config for MOS"
echo "------"

if [ -f /etc/planetexpress/config.yaml ]; then
    # Check if MOS provider is set
    if grep -q "host_control_provider.*mos" /etc/planetexpress/config.yaml; then
        echo "✓ Config already has host_control_provider: mos"
    else
        echo "! Config doesn't have host_control_provider set"
        echo "  Manual action required:"
        echo "  1. Edit /etc/planetexpress/config.yaml"
        echo "  2. Add or update: host_control_provider: mos"
        echo "  3. Save and continue"
        read -p "Press Enter when ready, or Ctrl+C to cancel..."
    fi
else
    echo "! No config file; will need to create one"
fi

echo ""
echo "STEP 7: Start Core Service"
echo "------"

echo "Starting casa-planetexpress..."
sudo service casa-planetexpress start
sleep 3

if sudo service casa-planetexpress status >/dev/null 2>&1; then
    echo "✓ Service started successfully"
else
    echo "✗ Service failed to start; check logs:"
    echo "  sudo tail -20 /var/log/casa-planetexpress"
    exit 1
fi

echo ""
echo "STEP 8: Verify Provider"
echo "------"

echo "Testing provider interface..."
python3 << 'PYEOF'
import sys
sys.path.insert(0, '/path/to/planet-express')  # adjust if needed

try:
    import config
    provider = config.get_host_control()
    print(f"✓ Provider active: {provider.__class__.__name__}")

    # Quick test
    state = provider.is_service_running("ssh")
    print(f"✓ Provider works: ssh is {'running' if state else 'not running'}")
except Exception as e:
    print(f"✗ Provider test failed: {e}")
    sys.exit(1)
PYEOF

echo ""
echo "=========================================="
echo "DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Monitor logs: sudo tail -f /var/log/casa-planetexpress"
echo "2. Check dashboard: http://127.0.0.1:8420/status"
echo "3. Review docs/v3-mos-deployment-checklist.md for full validation"
