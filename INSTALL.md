# Installing Planet Express

This walks through a fresh install on your own Docker Compose homelab. It assumes you're
comfortable with `sudo`, systemd, and editing a YAML file if something needs a tweak after
the wizard runs.

## Prerequisites

- Linux with systemd, Docker, and the `docker compose` plugin (`docker compose version`
  should work — the standalone `docker-compose` binary is not enough).
- Python 3.11+.
- The `acl` package (`setfacl`): `deploy.sh` uses it to give the dashboard's separate
  `planetexpress-web` user read-only access to exactly the files it needs.
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
4. **Configuration wizard** — runs `scripts/setup_wizard.py`, split into two parts:
   - **Basics** (always asked): your stacks directory (auto-discovers stacks under it) and
     any stacks to leave alone entirely. This alone is enough for a working install.
   - **Advanced** (one yes/no gate, defaults to skip): any containers intentionally stopped
     right now, any mounts to track, whether Bender (the executor) should be allowed to
     restart specific systemd/mount units as part of an approved remediation, and the
     LAN-only domain the `/install` command uses for its auto-router feature. Skipping this
     is safe — every advanced field defaults to off/empty. Note that re-running the wizard
     later will **not** revisit these answers (it reuses an existing `config.yaml`
     unchanged) — to add any of this after the fact, hand-edit `config.yaml` directly (see
     `config.example.yaml`).

   Writes `config.yaml` (default `/etc/planetexpress/config.yaml`). If you opted into any
   sudo-scoped actions, it also generates and (with your confirmation) installs the matching
   `/etc/sudoers.d/planetexpress` grant — generated from the same data you just declared, so
   the OS-level permission and the code-level allowlist Bender enforces can never drift apart.
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

## Upgrading and rolling back

Run these commands as the unprivileged core user, from the repository, with `CASA_CONFIG`
and `CASA_DATA_DIR` set to the same paths used by the service if you changed their defaults
(`/etc/planetexpress/config.yaml` and the repository's `data/`). The snapshot tool does not
load or validate the config, so it also works with a config written by newer code.

`deploy.sh` automatically snapshots existing config and database state before the
configuration wizard runs, and aborts if the snapshot fails. Before a manual upgrade,
stop both units and take a snapshot before checking out the new tag:

```bash
sudo systemctl stop casa-dashboard casa-planetexpress
venv/bin/python scripts/state_snapshot.py create --label pre-upgrade
git checkout <tag>
bash deploy.sh
```

Always stop **both** units before checking out another tag: the dashboard renders templates
from disk, so changing the checkout while it runs can mix old code with new templates.
Snapshots live in `DATA_DIR/snapshots/`; creation prints the path. List them with
`venv/bin/python scripts/state_snapshot.py list`. The database copy includes committed WAL
writes, and the manifest records file checksums, schema version and Git revision.

To roll back, select the snapshot taken before that upgrade:

```bash
sudo systemctl stop casa-dashboard casa-planetexpress
venv/bin/python scripts/state_snapshot.py restore <snapshot> --yes
git checkout <old-tag>
venv/bin/pip install -r requirements.txt
sudo systemctl start casa-planetexpress casa-dashboard
```

Restore **before** checking out the old tag: older tags do not contain
`scripts/state_snapshot.py`. The script uses only the Python standard library and nothing from
this repository, so a copy kept outside the clone works too. A restore verifies checksums,
refuses unless systemd positively reports `casa-planetexpress` as `inactive` or `failed`, and
takes a `pre-restore` snapshot to make the operation reversible. If the service state cannot be
read (no systemctl, no bus), it refuses unless you add `--no-service-check`; only do that with
the core certainly stopped. Each restored file is replaced atomically, keeping the live file's
owner and access ACL. A database the snapshot recorded as absent is removed; a config it recorded
as absent makes the restore refuse, since the config probably lived at another path.

On a default install the config lives in root-owned `/etc/planetexpress`, which the core user
cannot write. Restore detects that before changing anything and refuses with the exact command.
Run it in two steps:

```bash
venv/bin/python scripts/state_snapshot.py restore <snapshot> --yes --skip-config
sudo cp --no-preserve=all data/snapshots/<snapshot>/config.yaml /etc/planetexpress/config.yaml
```

`cp` onto the existing file rewrites it in place, so its owner, mode and the dashboard's read ACL
are kept. The first command prints the second with the real paths.

If core already tried to start on the newer state (it refuses: "database schema is newer" or
"Invalid config"), systemd's start limit (5 starts in 300s) may be exhausted and the next start
fails with "Start request repeated too quickly" even though the restore worked. Clear it first:

```bash
sudo systemctl reset-failed casa-planetexpress
sudo systemctl start casa-planetexpress casa-dashboard
```

Restoring discards approvals and events recorded after the snapshot. If the core refuses
to start with “database schema is newer”, the pre-upgrade state restoration step was
skipped: restore the matching snapshot or upgrade the code again.
