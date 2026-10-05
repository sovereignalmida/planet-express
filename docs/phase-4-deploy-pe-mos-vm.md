# Phase 4: Deploy Planet Express on MOS VM

Deploy the full Planet Express application (not just the provider) on the MOS test VM. This creates a working PE instance that can monitor and control services.

## Prerequisites

- v3 code cloned to `/root/planet-express` ✓
- Python dependencies installed ✓
- Config at `/etc/planetexpress/config.yaml` with `host_control_provider: mos` ✓
- Test services created (test-dummy, etc.) ✓

## Deployment Steps

### 1. Copy init.d Script

```bash
sudo cp /root/planet-express/scripts/casa-planetexpress.init.d /etc/init.d/casa-planetexpress
sudo chmod +x /etc/init.d/casa-planetexpress
```

### 2. Create Required Directories

```bash
sudo mkdir -p /var/log/casa-planetexpress
sudo mkdir -p /root/stacks
sudo chmod 755 /var/log/casa-planetexpress
```

### 3. Configure PE for MOS VM

Edit `/etc/planetexpress/config.yaml`:

```yaml
# Minimal config for MOS VM testing
stacks_root: /root/stacks
host_control_provider: "mos"
dashboard_port: 8420
telegram_bot_token: "test_token_vm"
telegram_chat_id: "test_chat_id"

# Optional: monitoring settings
attempt_cooldown_seconds: 5
autonomy_attempt_limit: 3
```

### 4. Start PE Service

```bash
sudo service casa-planetexpress start
sudo service casa-planetexpress status
```

Check logs:

```bash
sudo tail -f /var/log/casa-planetexpress/casa-planetexpress.log
```

### 5. Verify PE is Running

```bash
# Check service status
sudo service casa-planetexpress status

# Check if listening on dashboard port
netstat -tlnp | grep 8420
curl http://127.0.0.1:8420/status 2>/dev/null | head -20

# Check logs for errors
sudo tail -20 /var/log/casa-planetexpress/casa-planetexpress.log
```

## Validation Tests

### Test 1: Provider is Active

```bash
PYTHONPATH=/root/planet-express:$PYTHONPATH CASA_CONFIG=/etc/planetexpress/config.yaml python3 << 'PYEOF'
import config
provider = config.get_host_control()
print(f"Provider: {provider.__class__.__name__}")
print(f"test-dummy running: {provider.is_service_running('test-dummy')}")
PYEOF
```

### Test 2: PE Can Control Services

```bash
python3 << 'PYEOF'
import sys
sys.path.insert(0, '/root/planet-express')

# Simulate what PE's engine does
from planet_express.execution.engine import PipelineEngine
import config

# Get the provider (MOS)
provider = config.get_host_control()

# Test restart of test-dummy
print("Testing service restart via PE engine...")
result = provider.restart_service('test-dummy')
print(f"Result: {result}")
print(f"  ok: {result.ok}")
print(f"  before: {result.before}")
print(f"  after: {result.after}")
print(f"  effect: {result.effect}")
PYEOF
```

### Test 3: PE Can Read Logs

```bash
python3 << 'PYEOF'
import config
provider = config.get_host_control()

# PE's monitoring reads logs this way
logs = provider.get_host_logs('test-dummy', lines=5)
print(f"Got {len(logs)} log lines from test-dummy:")
for line in logs:
    print(f"  {line}")
PYEOF
```

### Test 4: Dashboard Accessibility

```bash
# From another terminal or host that can reach the VM
curl -s http://127.0.0.1:8420/status | python3 -m json.tool
```

Should return JSON with PE status.

## Troubleshooting

### PE Service Won't Start

```bash
# Check logs
sudo tail -50 /var/log/casa-planetexpress/casa-planetexpress.log

# Run manually to see errors
cd /root/planet-express
python3 -m planet_express.main

# Check if port 8420 is in use
netstat -tlnp | grep 8420

# Check config is valid
python3 << 'PYEOF'
import config
print(f"Config loaded: {config.HOST_CONTROL_PROVIDER}")
PYEOF
```

### Provider Not Working

```bash
# Verify MOS provider is being used
grep host_control_provider /etc/planetexpress/config.yaml

# Test provider directly
CASA_CONFIG=/etc/planetexpress/config.yaml python3 << 'PYEOF'
import config
p = config.get_host_control()
print(type(p))
PYEOF
```

### Services Not Controlled Properly

```bash
# Verify test services exist
ls -la /etc/init.d/test-*

# Test service directly
sudo service test-dummy status
sudo service test-dummy restart
sudo service test-dummy status

# Test via provider
python3 -c "import config; p = config.get_host_control(); print(p.is_service_running('test-dummy'))"
```

## What This Enables

Once PE is running on the MOS VM:

1. **Full End-to-End Testing** — PE's engine can monitor and control services
2. **Provider Validation at Scale** — Test with real monitoring loops
3. **Integration Testing** — Verify casa_leela, engine, planner all work on MOS
4. **Performance Testing** — Measure overhead on sysvinit vs systemd
5. **Failure Scenario Testing** — Simulate outages, errors, recovery

## Next Steps After Deployment

- Run PE's built-in tests: `pytest tests/ -v`
- Monitor logs for errors over time
- Test failure scenarios (kill PE, corrupt service, etc.)
- Benchmark performance vs systemd
- Document any MOS-specific issues

## Known Issues

- PE might take 5-10 seconds to start (Python startup time on VM)
- Dashboard may be slow initially (first data collection)
- Logs are not rotated automatically (add logrotate config if running long-term)

## Success Criteria

✓ PE starts without errors
✓ Dashboard responds on port 8420
✓ Provider is MosHostControlProvider (confirmed in logs)
✓ PE can read service status
✓ PE can control test services
✓ Logs are being written to `/var/log/casa-planetexpress/`
