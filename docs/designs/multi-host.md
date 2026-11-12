# PE knows every Docker host: design (draft, 2026-11-12)

Status: proposal for the operator's decision. Nothing here is built. Supersedes nothing yet: T48
(`docs/handoff/T48-multi-host-spec.md`, observe-only via Beszel) stays the running design until a level below ships.

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
  .25, .154, .1, and this host's own non-Docker services). A Traefik label on another host's container is ignored by it, which is
  what the new host runs into.

The operator wants one picture: every container on every Docker host counted, checked and alerted on, in one place.

## 2. Three levels, each a separate decision

| Level | What PE does | Touches other hosts | Risk |
|---|---|---|---|
| 1. One inventory | Lists and counts every host's containers (state, health, image, labels, restarts) | Reads | Low |
| 2. One health picture | Leela checks them; problems become findings, Telegram alerts, NEEDS YOU; Observe missions can read them | Reads | Low |
| 3. Acting | Restart, update, repair on another host | Writes | High: a design of its own |

Levels 1 and 2 are proposed now. Level 3 is scoped at the end only to say where its lines would be, so 1 and 2 do not build anything
it would have to undo.

## 3. How PE reaches another host

Three candidates were considered.

| Transport | Gives | Against |
|---|---|---|
| **Read-only Docker API proxy** on each host (a socket-proxy container exposing only GET endpoints) | The real Docker API: list, inspect (labels, restart count, health log, mounts), info (engine ID), events | One small container per host; inspect returns container env, which can hold secrets |
| Docker over SSH (`DOCKER_HOST=ssh://`) | Everything, no new container | Docker access over SSH is root on that host: far more than levels 1 and 2 need, and the key would sit in Core |
| Beszel agent (today) | Metrics, a container list | No labels, no restart count, no inspect, health codes partly unmapped, no events; it cannot answer what Leela asks |

**Proposed: the read-only proxy**, the transport `v4-mission-control.md` §487 already anticipated for "the read/discovery endpoints
multi-host needs".

- One container per remote Docker host (for example `tecnativa/docker-socket-proxy` or its maintained successor, pinned by digest),
  mounting `docker.sock` read-only with only `CONTAINERS`, `INFO`, `VERSION` and `EVENTS` enabled and every POST/DELETE refused.
  Which image, and that it truly refuses writes, is verified before the first release (a write attempt in the test plan).
- Reachable from this host only: bound to the LAN address with a firewall rule (or the proxy's own allowlist) admitting
  192.168.1.94 alone. Optional mutual TLS where the operator wants it; config takes `tcp://` and `https://` with a client cert.
- **Secrets:** container inspect includes `Config.Env`. PE never asks for it where list is enough. Where it inspects, it drops
  `Config.Env` before anything is kept, and the redactor runs over the rest before persistence, as it does for local data today.
- Beszel stays for what it is good at: CPU, memory, disk, temperatures. A host without a Beszel agent still gets its containers.

PE's own host keeps the local socket. The proxy is only for the others.

## 4. Level 1: one inventory

**Config.** `multi_host.hosts` gains an optional `docker:` endpoint per host (and `system_id` becomes optional), so a host is a
Docker host, a Beszel host, or both:

```yaml
multi_host:
  hosts:
    - name: CASA UNRAID
      docker: tcp://192.168.1.171:2375      # the read-only proxy
      system_id: ptf3tn2gzpg913i             # Beszel, for metrics (optional)
      link: https://unraid.casalan.com
    - name: NEW HOST
      docker: tcp://192.168.1.x:2375
```

No schema change to the database; the inventory is config, as T48 decided (a host known only from a collector's answer vanishes
exactly when something is wrong).

**Collector in Core**, not the web process. Today's `FleetCache` lives in the dashboard and refreshes only while someone looks;
level 2 needs the data when nobody is looking. One collector per host, each with its own timeout, runs with every scan and every
60 s between, and writes a snapshot per host next to Leela's (`state/hosts/<name>.json`). The dashboard reads the files as it reads
Leela's today.

**Rules carried over from T48:**
- Absent is not zero. An unreachable host is `unknown` with a reason (no contact, refused, clock unusable), never "0 containers".
- Locality is derived, never declared. With explicit endpoints this becomes simple and exact: an endpoint whose engine ID
  (`GET /info` → `ID`) equals the local daemon's is refused at config apply ("that is this host"). The Beszel name-matching heuristic
  stays only for pairing a Beszel system with its Docker host when both are given (or the operator gives both on one entry, which is
  the proposal: no heuristic needed).

**Surfaces.**
- Overview: "87 here · 142 on 3 hosts" (unknown hosts said as such, not summed as zero).
- Hosts tab: each host's full container list with state, health and restarts, from the proxy; metrics from Beszel where present.
- The single container count is gone; a fleet total is shown only with the per-host breakdown beside it.

## 5. Level 2: one health picture

**Findings.** Leela's container checks (stopped, crash-looping, unhealthy, restart counts) run over each host's snapshot as well as
the local one. Each finding carries its host. Checks that need a shell on the host (NFS stat inside a container, gluetun logs,
qBittorrent `docker exec`) stay local: the proxy refuses exec by design. The existing SSH probe to Unraid for duplicate fsids stays
as is.

**Reports and alerts.** Hermes reads the findings as now and names the host. Telegram alerts say which host. NEEDS YOU lists
remote findings with no action button: the honest action is "look at it there" (the host's link).

**Boot settle** is per host: a remote host that just rebooted is noisy; a finding there waits until its containers have been up past
their health-check start period, as locally.

**Observe missions read remote hosts.** A new brokered read tool, `fleet.read` (list, inspect without env, logs tail through the proxy
with the redactor), lets an Observe or Propose mission investigate a remote container. It gets the second review like every brokered
tool. A Repair mission still cannot change another host: its approval card says "this host" and stays true.

**Network.** Two separate things:
- PE side: the Network tab joins a File route to a remote container when the route's backend IP and port match a container's
  published port on a host whose endpoint has that IP. The route then gets the container's state, icon and detail like a Docker
  route does.
- Traefik side (the operator's): Traefik's Docker provider watches one daemon. For the new host's labels to become routes, the
  options are File routes as today, or a label-forwarding helper (for example `traefik-kop`, which publishes a remote host's labels
  into Traefik's Redis provider). PE can show which remote containers carry `traefik.*` labels with no matching router, which is
  exactly the new host's situation. Choosing and installing the helper is the operator's; PE reports the gap.

## 6. Level 3: acting on another host (not proposed now)

Recorded only so levels 1 and 2 leave room for it. T48's four open questions stand, with these starting positions:

- **Transport:** a second, separate channel, never the read proxy. Likely a small PE agent on each host running the same typed
  runbook steps (restart, compose up, canary update) through its own `run_argv` with its own sudo scope, authenticated to Core.
  Not SSH-as-root.
- **Lock:** one host-mutation lock per host, plus the existing local lock; a change on host A does not wait on a scan of host B.
- **Risk class:** every remote mutation starts at R3 (approval each time, never automatic) until live use says otherwise.
- **Checkpoints and undo** live on the remote host and are taken by its agent; Core keeps the record.
- **Missions:** trust-mode sessions with native tools stay on this host. A remote repair is a typed runbook, approved per run.
- **Host vanishes mid-action:** the action is `unknown` and the host is re-scanned when it returns, never assumed done.

## 7. Releases

| Release | Content | Effort |
|---|---|---|
| A (level 1) | `docker:` endpoint in config, Core collector, per-host snapshots, engine-ID locality, Overview and Hosts tab | M |
| B (level 2) | Leela over remote snapshots, findings with a host, Hermes, Telegram, NEEDS YOU, per-host boot settle | M |
| C (level 2) | `fleet.read` brokered tool for Observe missions; Network joins File routes to remote containers; unrouted-label report | M |
| Level 3 | Its own design doc first | L |

Each release: characterization tests first, the second review (Codex and `/code-review`), live check on the host.

## 8. Decisions for the operator

1. The read-only proxy as the transport for levels 1 and 2 (vs SSH, vs Beszel only).
2. Which hosts run Docker and get a proxy: Unraid (.171)? The Mac mini? The new host (name and address)? Is .80 one of these?
3. Proxy reachable from this host only by firewall rule, or with mutual TLS as well.
4. Beszel kept for metrics only, or dropped once the proxy covers containers.
5. The new host's routes: File routes, or a label-forwarding helper (operator's setup; PE only reports the gap).
