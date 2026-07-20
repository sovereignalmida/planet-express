# Installing Planet Express

This walks through a fresh install on your own Docker Compose homelab. It assumes you're
comfortable with `sudo`, systemd, and editing a YAML file if something needs a tweak after
the wizard runs.

## Prerequisites

- Linux with systemd, Docker, and the `docker compose` plugin (`docker compose version`
  should work — the standalone `docker-compose` binary is not enough).
- Python 3.11+.
- One or more stacks under a single directory, each with its own `docker-compose.yml`
  (e.g. `~/stacks/media/docker-compose.yml`, `~/stacks/network/docker-compose.yml`, ...).
  Planet Express discovers stacks this way — it doesn't manage stacks that live elsewhere.
- An API key for an LLM provider: either an Anthropic key (`ANTHROPIC_API_KEY`) or an
  OpenAI key (`OPENAI_API_KEY`). Planet Express uses this for finding-analysis and
  plan-generation calls; costs are bounded by the pipeline's own schedule (a status pass
  every 6h by default, deeper diagnosis only on an already-failed remediation).
- A Telegram bot token and a chat id (see below) — Telegram is how you approve/deny
  everything Planet Express proposes, and the only control surface in v1.

### Getting a Telegram bot token and chat id

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the
   prompts. You'll get back a token that looks like `123456789:AAF...`.
2. Send your new bot any message (e.g. "hi") so Telegram has a chat to report.
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser — the JSON
   response includes `"chat":{"id": ...}`. That number is your chat id.

(The setup wizard does not do this lookup for you automatically — polling Telegram mid-install
is a real network dependency for what's ultimately a one-time lookup, so it stays a manual
step here rather than another way the installer can fail.)

## Install

```bash
git clone https://github.com/sovereignalmida/planet-express.git
cd planet-express
bash deploy.sh
```

`deploy.sh` walks through, in order:

1. **Preflight** — checks `docker`, `docker compose`, and `python3` are present.
2. **Runtime directories** — creates `state/` and `logs/` under the repo (already
   gitignored).
3. **Python virtualenv** — creates `venv/` and installs `requirements.txt` into it.
4. **Configuration wizard** — runs `scripts/setup_wizard.py`, which asks about your stacks
   directory, any stacks/containers to leave alone, any mounts to track, and whether Bender
   (the executor) should be allowed to restart specific systemd units or mount units as
   part of an approved remediation. Writes `config.yaml` (default
   `/etc/planetexpress/config.yaml`) — see `config.example.yaml` for the full schema if you
   want to hand-edit afterward. If you opted into any sudo-scoped actions, it also generates
   and (with your confirmation) installs the matching `/etc/sudoers.d/planetexpress` grant —
   generated from the same data you just declared, so the OS-level permission and the
   code-level allowlist Bender enforces can never drift apart.
5. **Secrets setup** — prompts for your LLM provider choice + API key, and your Telegram
   bot token/chat id, writing them to `/etc/planetexpress.env` (mode 600, outside the repo).
6. **Systemd units** — renders `systemd/casa-planetexpress.service.template` (the always-on
   agent) and `systemd/casa-stacks.service.template` (boot-time `docker compose up -d` for
   every discovered stack) with your install path/user/config location, installs them under
   `/etc/systemd/system/`. Also prompts for a dashboard port and, if you opt in, renders
   `systemd/casa-dashboard.service.template` (the read-only web dashboard, `casa_scruffy.py`)
   the same way.
7. **Smoke test** — runs Leela (the monitor) once in status mode against your new config, so
   you see real container counts before anything is enabled.
8. **Enable and start** — optionally enables and starts `casa-planetexpress.service` right
   away.

No step requires editing a file by hand for a standard install — if you need something the
wizard doesn't ask about (e.g. `exclude_services`, to keep specific stack services out of
canary auto-updates), edit `config.yaml` directly; see `config.example.yaml` for the shape.

## What the sudo grant is for

By default Bender (the executor) can run `docker compose`/`docker` commands but nothing
privileged at all — `sudo_allowlist` in `config.yaml` is empty until you declare something.
If you opt in during the wizard, it's scoped to exactly `sudo systemctl <start|stop|restart>
<unit>` for the specific unit names/glob patterns you declared — nothing else. This is
enforced twice: once in code (`casa_bender.py`'s `_safety_check()`, independent of whatever a
generated remediation plan claims it needs) and once by the OS-level `NOPASSWD` sudoers.d
grant itself. Skipping this step is safe — Planet Express still monitors, diagnoses, and
proposes remediations, it just can't execute anything that needs `sudo`.

## Verifying it's running

```bash
journalctl -u casa-planetexpress -f
```

You should see a startup banner, the scheduler intervals it registered, and (once its first
scheduled pass runs) a monitor → findings → idle cycle with no tracebacks. In Telegram, try:

- `/status` — quick health check
- `/check` — run a full scan + plan immediately (don't wait for the schedule)

If a `/check` produces a finding, you'll get an approve/cancel prompt — nothing executes
without you tapping approve.

## Homepage widget

If you use [gethomepage.dev](https://gethomepage.dev), the dashboard also exposes a
`customapi`-compatible JSON endpoint at `/api/widget`. Add an entry like this to your
`services.yaml`:

```yaml
- Planet Express:
    icon: mdi-rocket-launch
    href: http://<dashboard-host>:8420/
    description: Sysadmin agent status
    widget:
      type: customapi
      url: http://<dashboard-host>:8420/api/widget
      mappings:
        - field: status
          label: Status
        - field: open_findings
          label: Findings
          format: number
        - field: last_scan
          label: Last Scan
          format: relativeDate
```

## What this does not cover

- **No authentication on the web dashboard.** It's read-only and meant for a LAN-trust
  environment, the same posture as most homelab dashboards (Homepage, etc.) — don't expose it
  to the open internet without putting your own reverse-proxy auth in front of it.
- **Single Telegram chat only.** `TG_CHAT_ID` is one recipient; there's no multi-user
  approval flow.
- **Docker Compose only.** No plain `docker run` fleets, no Portainer, no Kubernetes.
- **CI does not exercise real Docker/sudo behavior.** The automated test suite covers pure
  logic (safety-check allow/deny matching, config schema validation, state-schema
  round-trips) — the actual container/sudo behavior on your box is exactly what this install
  walkthrough and the smoke test above are for.
