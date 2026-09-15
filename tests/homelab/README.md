# Test homelab

A throwaway KVM guest shaped like the live Planet Express host, used to rehearse every 2.0
landing (1a, 1b, 1r, 1c, …) before it touches the real server. See "Prerequisite — local test
homelab" in `docs/designs/planet-express-2-0-slices.md`.

It is **not** run in CI: CI has no Docker or systemd. It runs on your machine.

## What it mirrors

| Live host (`casamediaserver`) | Homelab guest |
| --- | --- |
| Ubuntu 24.04 LTS | Ubuntu 24.04 cloud image (checksum-verified) |
| operator `casaroot`, home dir not world-readable | `casaroot`, `/home/casaroot` at `750` |
| stacks under `/home/casaroot/stacks` | four fixture stacks (below) |
| `stacks/network/.env` read by `casa_leela.py:474` | stub with a fake `GSP_GTN_API_KEY` |
| Traefik file-provider certs read at `casa_leela.py:755-757` | `dynamic/certs.yml` + two generated certs (60 days, 5 days) |
| `daily-borg-backup` / `weekly-borg-backup` oneshots + timers | same unit names, stub jobs (can be made to fail) |
| `casa-mounts` gate on `casa-stacks.service` | stub gate, added inline by `post-deploy.sh` |
| production Telegram bot | a separate **test** bot you create |

What it does not mirror: gluetun/qBittorrent (the VPN port-forward check takes its unreachable
path), the Unraid NFS exports, and the Chasing Portugal DAM queues. Those checks stay quiet or
report unreachable here.

## Fixture stacks

| Stack | Behaves | Rehearses |
| --- | --- | --- |
| `healthy` | nginx, passing healthcheck | typed restart → verification passes |
| `crash-loop` | exits 1 every few seconds, restart policy | crash-loop finding, verification fails |
| `unhealthy` | running, healthcheck always fails | unhealthy finding, "healthcheck failing" |
| `slow-start` | `starting` for 60s, then healthy | health grace period, 90s verify timeout |

## Requirements (your machine)

`qemu-system-x86_64`, `qemu-img`, usable `/dev/kvm`, `curl`, `python3`, an SSH key in
`~/.ssh/`. No libvirt, no root, no network bridge. Default size is 2 vCPU / 3 GB RAM / 20 GB
thin disk; override with `PE_VCPUS`, `PE_MEM_MB`, `PE_DISK_SIZE`. VM files live in
`~/.cache/planet-express-homelab/` (`PE_HOMELAB_DIR`), never in the repo.

## First run

1. Create a **test** Telegram bot with @BotFather and a test chat. Either copy
   `secrets.env.example` to `secrets.env` (gitignored) and fill it in, or skip the file and let
   `deploy.sh` prompt for the credentials in step 3. Don't hand-create an empty
   `/etc/planetexpress.env`: `deploy.sh` only prompts when that file is absent.
2. Boot and provision:
   ```bash
   tests/homelab/vm.sh up          # first boot installs Docker: ~5 minutes
   tests/homelab/vm.sh provision   # stubs, fixtures, config, starts the fixture stacks
   ```
3. Install 1.x the same way as on the live host:
   ```bash
   tests/homelab/vm.sh ssh
   cd ~/planet-express && bash deploy.sh      # stacks root /home/casaroot/stacks, port 8420
   sudo bash ~/planet-express/tests/homelab/guest/post-deploy.sh
   ```
4. Dashboard: <http://127.0.0.1:18420/>. In the test chat, `/status` and `/check` should answer.

This 1.x baseline is what each landing gets rehearsed against.

## Rehearsing a landing

```bash
git checkout v2.0.0-1a         # or the working branch, before tagging
tests/homelab/vm.sh push        # copy the working tree in; venv/state/logs are kept
tests/homelab/vm.sh ssh 'cd ~/planet-express && bash deploy.sh'   # or restart the services
```

Then walk the landing's success criteria in the design doc. To roll back, check out the
previous tag, push, and restart. To start completely fresh:
`tests/homelab/vm.sh destroy && tests/homelab/vm.sh up && tests/homelab/vm.sh provision`.

## Failure switches

- Failed backup job: `sudo systemctl edit daily-borg-backup.service`, add
  `[Service]` + `Environment=PE_FAIL_BACKUP=1`, then run the service.
- Failed mount gate: same with `casa-mounts.service` and `PE_FAIL_MOUNTS=1`.
- Expiring certificate: `expiring.homelab.test` is generated with 5 days left.
