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

(The browser wizard does this lookup for you with its *Find my chat* button. The scripted
`deploy.sh` path does not, so use the manual steps above there.)

## Install with the wizard (recommended)

```bash
git clone https://github.com/sovereignalmida/planet-express.git
cd planet-express
sudo ./setup.sh
```

Open the HTTPS link it prints in a browser on your LAN (one-time link, 15 minutes; the certificate is
self-signed, so accept the warning once, and compare the fingerprint the terminal printed if you want to
be careful). The wizard works the same on Ubuntu (systemd) and on MOS. On MOS, clone onto a pool (for
example `/mnt/data/pe/planet-express`), because MOS keeps `/` in RAM.

What it does for you, in order: **scan** (it tells you what is missing and how to fix it; it does not
install Docker or create storage pools), **where it lives**, **what it may do**, **Telegram** (paste the
bot token, send your bot any message, press *Find my chat*, then *Send test message*), an **operator
account** for the dashboard (scan the QR code with an authenticator app and type a code to prove it works;
there are no recovery codes, another operator can re-enrol you), an optional **LLM key** (checked against
the provider), and a **review** of the exact plan. Approving it installs; every step is read back from the
host to confirm, and it stops at the first thing that is not right.

- **Undo** puts back what the install changed. It only touches what it can prove it made, and refuses
  the moment anything has changed since (a file you edited, a directory that now has your files in it).
  Accounts and the pre-install snapshot are left in place and named.
- **Repair** re-checks an existing install against the host and lists only what would change.
- **Uninstall** removes the services, units, MOS boot hooks and sudo grant after taking a snapshot, and
  keeps your configuration, secrets, state, data and the checkout. It can be undone too.
- **Preview.** `sudo ./setup.sh` is the real thing. To only look at the screens, run
  `python -m planet_express.setup serve --preview` as a normal user from a checkout that has
  `requirements-bootstrap.txt` installed. It shows everything and installs nothing.

The wizard needs internet access once, to build its own small virtualenv from hash-pinned packages.

## Install with the scripted path

The older installer, still supported:

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
enforced twice: once in code (`casa_bender.py`'s `_check_sudo_allowlist()`, independent of whatever a
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

## Container widget keys

A container's detail view can show live stats from the app's own API (Sonarr's and
Radarr's queues, Prowlarr's failing indexers, Immich's library size, AdGuard's query counts). The dashboard fetches them server-side; the keys stay on the host
and never reach the page. Put each key in `/etc/planetexpress-dashboard.env` (root-owned,
read by `casa-dashboard.service`), then `sudo systemctl restart casa-dashboard`:

```sh
SONARR_API_KEY=...                  # Sonarr → Settings → General → API Key
RADARR_API_KEY=...                  # Radarr → Settings → General → API Key
PROWLARR_API_KEY=...                # Prowlarr → Settings → General → API Key
IMMICH_API_KEY=...                  # Immich, signed in as an admin → Account Settings → API Keys,
                                    # with the server.statistics permission (or "all")
ADGUARD_USERNAME=...                # the same pair the Network tab already uses
ADGUARD_PASSWORD=...
```

Radarr's widget needs Radarr 5.6 or later. A widget reads only `<WIDGET>_API_KEY`, `_TOKEN`, `_USERNAME` or `_PASSWORD`, named after
itself. A key is sent only to a container whose image was pulled, as that app, from a
registry the widget names (Docker Hub, `lscr.io`, `ghcr.io`), over a docker bridge network,
and never across a shared network namespace. Treat that as hardening, not a guarantee: a
compose file you approve decides what a container runs, so only approve compose files you
would trust with the key.

## Other hosts through beszel

Planet Express can read an existing beszel hub's PocketBase API over HTTP to show other
hosts. beszel continues collecting; its own UI becomes something you no longer need to
visit. Nothing changes on the monitored hosts or their agents.

First, make beszel reliable enough to depend on: add `restart: unless-stopped` to both the
`beszel` and `beszel-agent` services in their compose file. Those lines commonly ship
commented out, which is acceptable while beszel is an occasional dashboard but means neither
service survives a reboot.

Create a beszel user with the `readonly` role, then add that user to **every** system Planet
Express should see. This is a separate, essential step: the systems list rule is
`@request.auth.id != "" && users.id ?= @request.auth.id`, so an account sees only systems
that explicitly list it. A user assigned to no systems authenticates successfully and gets
HTTP 200 with an empty list, which otherwise looks exactly like a fleet with no hosts. beszel's
own `admin` role is not a PocketBase superuser: the `users` collection limits ordinary accounts
to listing themselves. Use the superuser identity from the `/_/` console to grant the user
access across systems.

Put that account in `/etc/planetexpress-dashboard.env` (root-owned, read by
`casa-dashboard.service`), then restart the dashboard as in [Container widget
keys](#container-widget-keys):

```sh
BESZEL_USER=...
BESZEL_PASSWORD=...
```

As with widget keys, these credentials never belong in `config.yaml` and never reach the
browser.

Declare the hosts PE should render in `config.yaml`:

```yaml
multi_host:
  hosts:
    - system_id: ptf3tn2gzpg913i
      name: CASA UNRAID
      link: https://unraid.casalan.com
  local_system_id: n7n7ppta55karj9  # optional pin, only if derivation cannot decide
```

`system_id` is beszel's stable `systems.id`: the name is editable in beszel's UI and addresses
change, so neither identifies a host. `name` is PE's display name and `link` is that host's own
UI (omit it when there is no UI to link to). An empty `hosts` list disables multi-host entirely:
PE does not query the collector at all.

There is deliberately no per-host “this is local” flag. PE compares each system's reported
container names with its own Docker socket to derive which system is local; asking for a
declared answer would only create a configuration value that can be wrong. Use the one optional
`local_system_id` pin only where that derivation cannot decide.

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
