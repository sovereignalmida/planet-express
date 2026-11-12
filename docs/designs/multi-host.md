# PE knows every Docker host: design (draft 2, 2026-11-12)

Status: operator decisions taken (§8); release A next. Nothing here is built. T48 (`docs/handoff/T48-multi-host-spec.md`,
observe-only via Beszel) stays the running design until release A ships.

## 1. The problem

PE is a one-host system with other hosts beside it.

- Leela, Hermes, the Overview count ("87 healthy"), Network, Zoidberg, repairs and missions see the local Docker daemon only. Every
  Docker call is a direct `docker` CLI argv with no host parameter (`casa_leela.py:192`, `planet_express/execution/actions.py`,
  `casa_zoidberg.py:63`, `casa_boot.py:97`, the checkpoint provider), through three separate runners.
- Other hosts are a read-only Beszel view on the Hosts tab (`planet_express/integrations/beszel.py`, `application/multi_host.py`):
  metrics plus container name, image, status and a partial health word, no labels, refreshed only while the page is open, held in
  the web process's memory. Nothing else reads it: a container crash-looping on the Unraid box is never a finding, never a Telegram
  alert, never in a count.
- Traefik on this host has two providers: Docker (local containers only) and File (11 hand-written routes to .171 Unraid, .80,
  .25, .154, .1, and this host's own non-Docker services). A Traefik label on another host's container is ignored by it.

The operator wants one picture: every container on every Docker host counted, checked, alerted on and routed, in one place.

## 2. Levels

| Level | What PE does | On other hosts | Risk |
|---|---|---|---|
| 1. One inventory | Lists and counts every host's containers (state, health, image, labels, restarts) and host metrics | Reads | Low |
| 2. One health picture | Leela checks them; findings, Telegram alerts, NEEDS YOU; Observe missions can read them | Reads | Low |
| 2b. One ingress | Remote containers' `traefik.*` labels become routes on this host's Traefik | Reads there, writes one file here | Medium |
| 3. Acting | Restart, update, repair on another host | Writes | High: its own design |

## 3. Transport: SSH (decided)

The operator chose SSH: level 3 will need a channel that can change the host, and building a read-only proxy first would mean a
second transport later. One channel, from the start.

- **Docker over SSH** (`docker -H ssh://<alias>` / the API through `docker system dial-stdio`) for containers, and plain commands for
  host metrics. The existing aliases are the model: `unraid` (root@192.168.1.171) and `casamacmini` (casamac@192.168.1.156) are
  already in Core's `~/.ssh/config`; CASALAB (192.168.1.80) needs one.
- **What it grants, said plainly:** Docker access over SSH is root-equivalent on that host. The key is held by Core only, like the
  model key: never in a session's environment (sessions already drop `DOCKER_HOST`, `grants.py:144`), never logged. Host keys are
  pinned in `known_hosts` (`StrictHostKeyChecking=yes`); a changed host key stops collection for that host and says so.
- **Read-only by code until level 3:** levels 1, 2 and 2b issue only read commands to other hosts, through one runner with an
  allowlist of argv shapes (`docker ps`, `docker inspect`, `docker info`, `docker events --since`, `docker logs --tail`, metric reads).
  Anything else is refused in PE before it reaches SSH. That allowlist is the reviewed surface (second review on every addition), as
  the step catalogue is locally.
- **TLS:** not needed. TLS certificates are how a Docker TCP socket is protected; with SSH, the key pair and pinned host key do that
  job, and nothing listens on a new port. Flexibility comes from the alias: the address, user and key live in `~/.ssh/config`, PE's
  config names the alias.
- **Secrets:** `docker inspect` includes `Config.Env`. PE uses `--format` to take only the fields it needs, so env is never read; logs
  pass the redactor before anything is kept.
- **Per-host quirks:** Unraid allows SSH as root only (as today). macOS needs `docker` on the non-interactive PATH
  (`/usr/local/bin` or `/opt/homebrew/bin`) and Docker Desktop running; collection says which when it fails.

PE's own host keeps the local socket.

## 4. Level 1: one inventory (release A)

**Config.** A host names its SSH alias; Beszel is no longer needed:

```yaml
multi_host:
  hosts:
    - name: CASA UNRAID
      ssh: unraid                 # an alias in Core's ~/.ssh/config
      link: https://unraid.casalan.com
    - name: CASA MAC MINI
      ssh: casamacmini
    - name: CASALAB
      ssh: casalab
    - name: CASA SOLAR ASSISTANT  # no Docker: metrics only, or a link only
      link: http://192.168.1.154
```

`system_id` (Beszel) stays accepted during the move and is dropped once release A's metrics are live (decision 4). No database
schema change; the inventory is config, as T48 decided.

**Collector in Core**, not the web process. One collector per host, its own timeout, run with every scan and every 60 s between;
a snapshot per host next to Leela's (`state/hosts/<name>.json`). The dashboard reads the files as it reads Leela's.

- Containers: `docker ps -a` and a batched `docker inspect --format` (state, health, restart count, started at, image, labels,
  published ports).
- Metrics: CPU, memory, disk, load, temperatures where readable, by OS: Linux (Unraid, CASALAB) from `/proc` and `df`; macOS from
  `sysctl`, `vm_stat` and `df`. Replaces Beszel.

**Rules carried over from T48.**
- Absent is not zero: an unreachable host is `unknown` with a reason (no contact, refused, host key changed, docker not found),
  never "0 containers".
- Locality is derived, never declared: an alias whose Docker engine ID (`docker info --format '{{.ID}}'`) equals the local daemon's
  is refused at config apply ("that is this host").

**Surfaces.** Overview: "87 here · N on 3 hosts" (an unknown host said as such, never summed as zero). Hosts tab: each host's full
container list with state, health and restarts, and its metrics.

## 5. Level 2: one health picture (release B, C)

**Findings.** Leela's container checks (stopped, crash-looping, unhealthy, restart counts) run over every host's snapshot. Each
finding carries its host. Checks that need a shell inside a container (NFS stat, gluetun logs, qBittorrent) stay local for now.
Hermes names the host; Telegram alerts say which host; NEEDS YOU lists remote findings with the host's link and no action button
until level 3.

**Boot settle** per host: a remote host that just rebooted waits past its containers' health-check start periods, as locally.

**Observe missions** get a brokered read tool, `fleet.read` (list, inspect without env, logs tail with the redactor), going through
the same allowlisted runner. Second review, like every brokered tool. A Repair still changes this host only; its card says so.

## 6. Level 2b: one ingress, PE as the label forwarder (release D)

The operator's question: rather than making Traefik aware of each Docker daemon, why not have one component that lets any Docker
host work this way? PE already reads every host's labels (level 1), so PE can be that component:

- For each remote container with `traefik.enable=true`, PE translates its `traefik.http.routers.*` and `traefik.http.services.*`
  labels into a Traefik dynamic-config entry whose service URL is `http://<host address>:<published port>` (the container port in
  the label mapped to the port the host publishes it on).
- PE writes **one generated file** in Traefik's File-provider directory (`pe-remote.yml`), never touching the hand-written ones.
  Traefik reloads it on its own.
- **Safety, as for any ingress change (the AdGuard/network-guard area, second review):** the file is validated before writing
  (names unique and not clashing with Docker or hand-written routers, rules parse, every middleware it names exists); written
  atomically with the previous version kept; after the write PE reads Traefik's API and rolls the file back if a router it wrote
  reports an error. Each change is recorded and posted to Telegram ("route added: app.casalan.com → CASALAB:8080").
- **What stops a route:** a label that cannot map (the container port is not published on the host; Traefik cannot reach a container
  network on another machine) is not written. The Network tab lists it as "labelled, not routable: publish port N", which is the
  fix.
- Turned on once by config (`multi_host.ingress: true`); after that PE keeps the file in step with the labels it sees.
- The Network tab shows these routes with their remote container, state and icon, like local Docker routes.

This does what a label-forwarding helper (traefik-kop and similar) does, inside PE, with PE's validation and record, and no extra
component on each host.

## 7. Level 3: acting on another host (not proposed now)

Starting positions, so levels 1 to 2b leave room for it: the same SSH channel with a wider allowlist (typed runbook steps only:
restart, compose up, canary update); one mutation lock per host; every remote change R3 (approved each time) until live use says
otherwise; checkpoints taken on the remote host and recorded in Core; trust-mode sessions stay on this host; a host that vanishes
mid-change leaves the change `unknown` and is re-scanned when it returns. Its own design doc first.

## 8. Decisions (operator, 2026-11-12)

1. Transport: **SSH**, one channel for every level.
2. Docker hosts: **CASALAB (192.168.1.80), the Mac mini (192.168.1.156), Unraid (192.168.1.171)**. Solar Assistant: no Docker.
3. TLS: **not needed with SSH** (keys and pinned host keys do that job).
4. Beszel: **dropped** once release A's metrics are live.
5. Ingress: **PE forwards remote labels into Traefik** (level 2b) instead of a per-daemon Traefik setup.

Still needed from the operator before release A goes live: an SSH alias for CASALAB (user, key) in Core's `~/.ssh/config`, and the
key's public half on CASALAB; confirm the `casamac` user on the Mac mini can run `docker`.

## 9. Releases

| Release | Content | Effort |
|---|---|---|
| A (level 1) | `ssh:` per host, allowlisted read runner, Core collector, per-host snapshots and metrics, engine-ID locality, Overview and Hosts tab | M |
| B (level 2) | Leela over every host, findings with a host, Hermes, Telegram, NEEDS YOU, per-host boot settle | M |
| C (level 2) | `fleet.read` for Observe missions; Beszel removed | S |
| D (level 2b) | Label forwarding into `pe-remote.yml`, validation, rollback, Network tab | M |
| Level 3 | Its own design doc first | L |

Each release: characterization tests first, the second review (Codex and `/code-review`), live check on the host.
