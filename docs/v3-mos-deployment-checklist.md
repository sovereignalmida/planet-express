# v3 MOS Deployment Checklist

This document guides deployment of v3 (Planet Express with HostControlProvider abstraction) to a MOS/sysvinit host. v3 maintains backward compatibility with systemd; MOS deployment adds a new provider path that routes all service control through `service` instead of `systemctl`.

## Pre-Flight Checklist

Before deploying, verify:

- [ ] Host is running MOS (or another sysvinit-based system)
- [ ] `/etc/init.d/` directory exists and is readable
- [ ] `sudo` is configured with passwordless grants for needed `service` commands
- [ ] Backup of current `/etc/planetexpress/config.yaml` is taken
- [ ] `casa-planetexpress` service is stopped: `sudo service casa-planetexpress status`
- [ ] No active deployments or monitoring scans are in progress

## Deployment Steps

### 1. Verify Provider Configuration

Check the configured provider type in `/etc/planetexpress/config.yaml`:

```yaml
host_control_provider: "mos"  # or "systemd" for Ubuntu/systemd hosts
```

If not present, add it to the config. MOS hosts must use `"mos"`.

### 2. Deploy v3 Code

Standard deployment (git pull, install dependencies, run migrations):

```bash
cd /path/to/planet-express
git fetch origin
git checkout v3.0.0  # or your branch
pip install -e .
python scripts/migrate_config.py  # if updating from v2
```

### 3. Start Core Service

```bash
sudo service casa-planetexpress start
sudo service casa-planetexpress status  # verify running
```

Watch logs for startup errors:

```bash
tail -f /var/log/casa-planetexpress  # or equivalent
```

## Post-Deployment Validation

Once running, validate the provider is working:

### 1. Verify Service Control

Test start/stop on a non-critical service (e.g., monitoring timer):

```bash
# Check current state
sudo service casa-monitor status

# Stop it
sudo service casa-monitor stop

# Verify via provider (from Python)
import config
provider = config.get_host_control()
print(provider.is_service_running("casa-monitor"))  # should print False

# Restart
sudo service casa-monitor start
```

### 2. Verify Monitoring

Check that casa_leela can read service logs:

```bash
# From the core Python environment
import config
provider = config.get_host_control()
logs = provider.get_host_logs("casa-planetexpress", lines=10)
print(logs)  # should show recent log lines
```

### 3. Run Integration Tests

```bash
cd /path/to/planet-express
pytest tests/test_host_control_factory.py tests/test_engine.py -v
```

All tests should pass.

### 4. Check Sudo Allowlist

Verify the sudo allowlist is configured correctly for MOS:

```bash
sudo visudo -c  # syntax check
grep planetexpress /etc/sudoers.d/*  # show grants
```

Expected format for MOS (note: `service <unit> <action>`, not `systemctl <action> <unit>`):

```
%planetexpress ALL=(ALL) NOPASSWD: /usr/bin/service casa-stacks.service start, /usr/bin/service casa-stacks.service stop, /usr/bin/service casa-stacks.service restart
```

## Monitoring Points

Once deployed, watch for these issues:

### Provider Health Indicators

**Normal operation:**
- Logs appear in `/var/log/casa-planetexpress` with no provider errors
- `casa_leela` reports service status correctly on scan loops
- Unit actions (start/stop/restart) complete without timeout or allowlist errors

**Warning signs:**
- Logs show "could not read the state of <service>" — init.d script may not be returning LSB-compatible exit codes
- Logs show "sudo service <unit> <action> failed" — sudo allowlist may need adjustment
- Service status reads return `None` — provider unable to parse `service status` output

### Rollback Triggers

Consider rolling back if:
- Service control hangs on restart (timeout > 30 seconds)
- Repeated "permission denied" or "not in sudo allowlist" errors
- Dashboard or monitoring stops responding (core service not restarting correctly)
- Provider reads consistently fail on a critical service

## Rollback Procedure

If deployment fails:

### 1. Revert Code

```bash
cd /path/to/planet-express
git checkout v2.x.x  # previous stable version
pip install -e .
```

### 2. Revert Config (if changed)

```bash
sudo cp /etc/planetexpress/config.yaml.bak /etc/planetexpress/config.yaml
# or set host_control_provider: "systemd" if on a systemd-compatible host
```

### 3. Restart Core

```bash
sudo service casa-planetexpress restart
```

### 4. Verify

```bash
sudo service casa-planetexpress status
curl http://localhost:8420/status  # dashboard health check
```

## Known Limitations (v3 on MOS)

1. **Service property reads** — `casa_stackctl.py` reads systemd-specific properties (Result, ExecMainStatus); backup job monitoring will report "unknown" status on MOS until a sysvinit-compatible monitoring path is added.

2. **Snapshot/restore** — `scripts/state_snapshot.py` uses `systemctl` directly for safety during rollback; it will fail on MOS hosts. Use manual snapshot/restore or add MOS support to that script before relying on automated rollback.

3. **Directory-shaped logs** — `get_host_logs()` uses `tail -n` on `/var/log/<unit>`; directory logs (e.g., nginx, samba) will fail with "Is a directory". Only flat-file logs are supported in v3.

## Post-Deployment Monitoring

Add these to your monitoring dashboard:

- Provider type in use (from config)
- Service control error rate (failed start/stop/restart operations)
- Log read success rate (percentage of successful `get_host_logs` calls)
- State read anomalies (where `is_service_running` returns `None`)

## Support

If deployment issues occur, collect:

1. Core service logs: `sudo tail -n 100 /var/log/casa-planetexpress`
2. Provider info: `grep host_control_provider /etc/planetexpress/config.yaml`
3. Init.d script state: `sudo service <unit> status` for failing services
4. Sudo allowlist: `sudo visudo -c && grep planetexpress /etc/sudoers.d/*`
5. Recent plan attempts: grep "unit.action" in core logs
