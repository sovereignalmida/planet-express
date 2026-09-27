# T46 — v2.2 launch links and container widgets

Source spec: `docs/designs/planet_express_design_v22/handoffs/V2.2-LAUNCH-LINKS-AND-WIDGETS.md`
Visual reference: `docs/designs/planet_express_design_v22/reference/Dashboard v2.2 - Launch Links and Widgets.dc.html`

The v2.2 design system is byte-identical to v2.1's; only `DATA-CONTRACT.md` moved. So this is
again adapter layer plus backend derivation, with no re-skin.

Together these two additions retire the `homepage` container.

---

## What the live host actually looks like

Written against the real Traefik API rather than fixtures, because `group_routers()`'s gate
round found three bugs that my fixtures had hidden by being tidier than the host. Pulled
read-only from `127.0.0.1:8079/api/http/routers`:

| | |
| --- | --- |
| routers | 70, all `enabled` |
| entrypoints | 67 `websecure`, 2 `traefik`, 1 `web` |
| providers | 57 docker, 10 file, 3 internal |
| **multi-host rules** | **15** — `Host(a) \|\| Host(b)`, one LAN one public |
| compound (`&&`) rules | 6 |
| no `Host()` at all | 3 (all `@internal`) |
| `-lan` twins | 5 |

**The correction that matters: multi-host rules, not `-lan` twins, are how most services get
both a LAN and a public address.** `actual@docker` is one router serving
`actual.casalan.com` and `actual.casaalmida.com`. The five `-lan` twins are a different and
rarer thing — separate routers with different paths (`subwave-api` is
`radio.casaalmida.com/api`). v2.1's matrix already handles both; v2.2's links must not assume
the twin pattern is the common one.

**`fetch_traefik_routers()` drops `entryPoints` today** and the URL scheme comes from it. That
field has to start being collected.

---

## The rule for emitting a URL

The spec says "build a URL only from a simple `Host()` rule… a missing link is better than a
wrong one." Applied to the live host, the honest version is:

> Emit a URL only when the rule is one or more `Host()` terms joined by `||` **and nothing
> else**. One URL per host. Any rule containing `&&`, `!`, `HostRegexp` or a bare `PathPrefix`
> emits nothing.

Measured against the live 70: **61 launchable routers → 74 URLs**, 9 skipped.

Eight of the nine are skipped correctly:

| Skipped | Why that is right |
| --- | --- |
| `subwave-api`, `subwave-api-lan` | `…/api` — JSON, not a page |
| `subwave-stream`, `subwave-stream-lan` | `…/stream.mp3` — a raw audio stream |
| `adventurelog-admin` | `&& (PathPrefix(/media) \|\| /static \|\| …)` — asset routes |
| `api@internal`, `dashboard@internal` | Traefik's own, no host |
| `web-to-websecure@internal` | `HostRegexp(^.+$)` — the HTTP→HTTPS redirect |

The ninth, `adventurelog@docker`, is a real loss: its rule is
`(Host(a) || Host(b)) && !(PathPrefix(/media) || …)`, so the host root *is* served by it — the
negation only excludes asset paths.

**Decision: take the loss and cover it with the config `links:` escape hatch the spec already
defines.** A parser that reasons about negated path groups to recover one service is a
maintenance liability that will eventually emit a confidently wrong link; an explicit line in
config is auditable and says who decided. Revisit only if a second service needs it.

---

## Slices

Sequenced so the part that can be held to unit tests lands first — the UI and the widget
fetcher want a reviewer, and the Codex gate is unavailable until 2026-09-28.

### T46.1 — URL derivation (no UI)

- `fetch_traefik_routers()` also collects `entryPoints`.
- `container_urls(routers, *, lan_domain)` in `casa_scruffy_net.py`: pure, returns
  `{router_service: [{"href", "zone"}]}`. `zone` is `lan` when the host's registrable domain is
  the LAN domain, else `public`. Scheme from the entrypoint (`websecure` → https, `web` →
  http), never hardcoded.
- Merge onto containers in `dashboard_data.py` as `"urls": [...]`, `[]` when there is no route.
- Config `links:` escape hatch, as an **editable** key, for services with no router.

Tests, all against the shapes above rather than invented ones: a multi-host router yields two
URLs with opposite zones; every compound, negated, regexp and host-less rule yields none; the
scheme follows the entrypoint; a `-lan` twin and its sibling do not produce a duplicate.

### T46.2 — Widget matcher and contract (no fetching)

- `widgets/<name>.py` declaring `WIDGET` + `summarise(responses)`, exactly as the spec shapes it.
- Match by image repo, `planetexpress.widget=<name>` label overrides, `=none` disables.
- The registry and matcher only. **No HTTP in this slice**, so it stays unit-testable.

### T46.3 — The widget fetcher  ⚠ *needs the gate*

**Status: built, gate cleared** (commit after `6543b3a`). Route
`GET /api/containers/<stack>/<service>/widget`; core RPC `query.widget_target` resolves which
widget and where; `planet_express/widgets/fetcher.py` fetches. Eight adversarial security
rounds (Codex stand-in, owner-authorised); round 8 had nothing at medium or above. What the
rounds changed, so the reasons survive:

| Round | Found | Now |
| --- | --- | --- |
| 1 | key sent in cleartext to a **macvlan** address (the host cannot reach its macvlan children -- ARP answered by the LAN); trickling server held threads; urllib3 decompression bomb | bridge-driver addresses only (core checks `docker network inspect`); background slots with a wall-clock wait; identity encoding |
| 2 | requests reads **and gunzips** a whole 3xx body inside `session.get()`; urllib3 reads chunk lines unbounded | `http.client` directly; socket watchdog |
| 3 | watchdog blind on `Connection: close` (http.client drops `conn.sock`); header flood outside the budget | watchdog holds its own socket; a capped reader charges every byte |
| 4 | compose **label** could route a key; parse amplification | keyed widgets need the image to match; 256 KiB default budget |
| 5 | compose **image name** could route a key (`evil.example/linuxserver/sonarr`, local `build:`) | registry-digest provenance |
| 6 | provenance registry-blind (`ghcr.io/adguard` != Docker Hub `adguard`); namespace owner's image unvetted | exact (registry, repo) pairs; no keys across shared namespaces |
| 7 | containerd image store gives local tags digests too | documented: hardening, not proof |
| 8 | none at medium+ | **clear** |

**The trust boundary, stated plainly:** widget keys are safe among containers the operator
approved. An approved compose sets entrypoint, volumes and env, and can run the genuine image
with a hostile listener -- the same approval that could mount the docker socket. Every check
above narrows accidents and label/name tricks; none makes an unreviewed compose safe. Worth a
line in the `/install` approval text: *this compose may receive widget keys.*

**Decisions for the owner:**
- A `planetexpress.widget` label can no longer pick a *keyed* widget for an image the widget
  does not already match (the spec allowed label overrides). Custom builds of a keyed app get
  no widget. Keyless widgets and `=none` still work.
- Sonarr shows QUEUE · WANTED · HEALTH, not the spec's SERIES: counting series means
  `/api/v3/series`, several MB parsed ~25x. Three tiles is within the contract.
- On the containerd image store, provenance cannot tell a local build from a pull.

**Verify on the live host:** each first-wave widget's image digest (`docker image inspect
--format '{{json .RepoDigests}}'`) names a registry in its match list; the widget's port is
reachable from the host on the container's bridge address.

Server-side, GET-only, 3s timeout, 30s cache, keys from the host secrets file and never into
the page. This is the one part of v2.2 with a security surface: a new outbound fetch path with
credentials. **Do not land this unreviewed.**

### T46.4 — Launch links in the UI

**Status: built** (commit after `668d57c`). Leela now records each stack service's container
(`services[svc].container`, only when exactly one container backs it), which is how a tile's
members meet the container-keyed links; snapshots from before this show no links until the
next full scan. Reviewed once (`/code-review high`): three mediums fixed -- the tile had
`role=button` around links (now a real button on the stack name), the drawer's listener
stacked per refresh (now bound once), and a drawer left open froze the page's refresh and
floated over other tabs (now closes on leaving Overview). The detail header's links load
from `/api/containers/<stack>/<service>/links` after render. Screenshots from the design
harness were checked at 1440×900 and 390×844.

Deviations from the spec, deliberately:
- A router serving both a LAN and a public host is a split pill too, not only a `-lan` twin
  (15 of the live 70 are one router with both). At rest the split segment reads `LAN`, like
  v2.1's `+LAN`; it becomes `LAN ↗` on hover, so the matrix stays calm.
- `PathPrefix` rules do not append the path (the spec said they should): T46.1's measured
  rule emits no link for any compound rule, which keeps `/api` and `/stream.mp3` off.
- ◉ is a hint from the image alone; the container's `planetexpress.widget` label is not in
  the snapshot. The detail view is authoritative.

Overview tile `↗ n`, the 420px stack drawer, and Network pills becoming links (including the
split pill that replaces v2.1's `+LAN` tag).

### T46.5 — Widget frame and states

Detail view placement, the four states, mobile.

### T46.6 — Retire homepage

The spec's checklist. Nothing is removed until what replaces it is working.

---

## Process per slice

Unchanged from T45, with one addition: **fixtures are derived from the live shape, not
invented.** T45.3 shipped a `group_routers()` whose tests passed against fixtures tidier than
the host — twins on different domains never merged, and two providers sharing a short name
silently dropped a route. Both were found by a reviewer, not by the suite.

While the gate is unavailable, a slice may be implemented and committed with the debt recorded
in its commit message, but T46.3 waits.

---

## Gate ledger

| Commit | Round | Findings | Status |
| --- | --- | --- | --- |
| `5bc5e58` harness + deploy template | 1 | 2 (1 P1, 1 P2) | fixed in `7228984` |
| `65bf547` launch links | 1 | 2 P1 | **open — see below** |
| `4556424` config docs | 1 | 0 | clean |
| `fee8041` widget registry | 1 | 2 P2 | fixed in `7228984` |
| `7228984` those fixes | 3 | 2 (a P1 in each of the first two rounds' own fixes) | clean on round 3 |
| `ab89b2b` approvals 503 | 1 | 0 | clean |

Two rounds on `7228984` each found a P1 *inside the previous round's fix* — a validator
added to isolate broken widgets that could itself raise. Worth remembering: a fix to an
isolation guarantee needs the same scrutiny as the thing it isolates.

## T46.1's two open P1s

Both are in code that is computed and not yet consumed — `ctx["launch_urls"]` is built in
`casa_scruffy.py` and read by no template, because T46.4 is not built. Nothing is wrong on
the live host today. Both must be settled before T46.4 wires them up.

### 1. The router-to-container join is wrong

`container_urls()` keys on the router's `service` with `@provider` stripped, on the
assumption that this is the compose service name. Measured against the live host, it is
mostly not:

| | |
| --- | --- |
| distinct router services | 64 |
| join to a compose service by name | **27** |
| do not | **37** |

The name is a Traefik service label, not a compose service: router `actual` is compose
service `actual_server`, `adguard` is `adguardhome`, `wiki` is `wiki-go`, `sabnzbd` is
`SabNZBD` (case differs), `billarr` is `frontend`, `immich-server-media` is `immich-server`.
Adding a `<service>-<project>` rule recovers only 5 more.

Worse than the misses: compose service names are **not unique across projects**.
`CASA_SUBWAVE_WEB` and `CASA_KARA_KEEP` are both service `web`. Keying by bare name would
attach one service's URL to another's container — the same collision shape as T45.3's
`api@file`.

**The join that works is the container IP.** `/api/http/services` gives each service's
`loadBalancer.servers[].url`, which for a docker-provider service is the container's own
address. Measured on the live host: 108 container IPs, **zero claimed by more than one
container**, and **56 of 70 enabled routers resolve to exactly one container**. The 14 that
do not are all genuinely not container routes — the 3 `@internal`, the file-provider routes
to other machines (opnsense `.1`, unraid `.171`, solar `.154`, adguard-secondary `.25`), and
the host-networked services on `.94` (jellyfin, plex, qbit, planetexpress, musicassistant),
which are exactly what the config `links:` escape hatch is for. It also drops non-container
routes for free, which was the second half of the finding.

Cost: container IPs are not collected today, so this needs a field in `casa_leela.py`'s
inspect pass — scan-path code.

**Status: settled -- the join is by label, read live from core.** Superseded the address
join of `9d52e24`, whose history is worth keeping because it is why:

1. `9d52e24` joined by backend IP from Leela's monitor snapshot. The snapshot is up to 6h old,
   and a canary recreate can hand an address to another container: a wrong link for hours.
2. A live core RPC (`containers.addresses`) fixed staleness, but four adversarial rounds each
   found another moment an address is held by the wrong container -- `docker restart A B`
   swapping them with no id change, a removed namespace sharer, `network connect`, Traefik's
   provider lag. Each fix was a new special case.
3. Round 4 named the altitude problem, and the join moved to **labels**: a router is joined to
   the container whose `traefik.http.routers.<name>.*` labels declare it (or whose compose
   service/project names Traefik's default router), and a link is emitted only when that same
   container declares the service the router forwards to. Labels change only on recreate (a
   new id) and names only on `docker rename`, so the RPC (`containers.routers`) caches its
   inspect on the exact `[id, name, status]` list from one `docker ps -a` per call.

What the label join refuses, deliberately (every one costs a link, never misplaces one):

- routers on a container whose network namespace another shares (gluetun carries qbit's
  labels) -- `links:` places those;
- names two claimants declare, counting compose one-offs and paused/restarting containers;
- services with `loadbalancer.server.url`, or weighted/mirroring/failover;
- file-provider and cross-provider routes;
- the whole read, when a `container:<ref>` cannot be resolved the way Docker resolves it
  (full id, name, 12+ hex prefix) -- a warning names the container responsible.

**Must verify on the live host before T46.4 ships:** `_declared()` mirrors Traefik's
docker-provider naming by hand (default router `Normalize(service_project)`, the >1-service
rule, TCP/UDP-only, `traefik.enable`). Count how many of the 70 routers get a link through
`containers.routers`, and that none lands on the wrong row. The address join measured 56.

**Also verify:** the dashboard's RPC client deadline (5s) against `RPC_DOCKER_TIMEOUT_SECONDS`
(4s) with ~100 containers in one `docker inspect`.

**Leftover, owner decision:** `9d52e24` added address collection to Leela's scan (`c["ips"]`,
`_inspect_network_addresses`, `dashboard_data.container_ips`, Hermes stripping `ips`). Nothing
reads it now. Removing it was blocked by the session's permission classifier as a revert, so
it is left for you: one extra `docker inspect` per scan, otherwise inert.

### Gate for this change (T46.1 P1 #1)

Codex CLI was not available in the session that wrote it. The owner authorised a Claude
adversarial review as the gate instead; twelve rounds of `/code-review high`:

| Round | Wrong-container / escaping findings | Outcome |
| --- | --- | --- |
| 1-2 | partial inspect, ID-keyed cache vs `docker restart`, namespace sharers | fixed, then redesigned |
| 3-4 | short-id owner ref; removed sharer; `network connect`; altitude | **moved to labels** |
| 5 | router on a container that doesn't own its service; rename kept id | router+service must agree; key id+name |
| 6 | sidecar labels on gluetun; rename between list and inspect | namespace owners withheld; key from inspect |
| 7 | `server.url` services; enable/one-off fail-open | withheld; claimants-not-owners |
| 8 | owner recreated alone (regression from 7); stopped sharer | `ps -a`; dangling ref fails closed |
| 9 | paused/restarting key mismatch; weighted services; name refs | status key; withheld; names refused |
| 10 | hex-looking names taken as id prefixes | Docker's resolution order |
| 11 | short hex ref matched a stranger's id | prefixes need 12+ hex |
| 12 | none at medium or above | **clear** |

### 2. The dashboard never re-reads edited config

**Status: fixed** (commit after `fc4579a`). New read-only core RPC `config.enforced` returns
core's loaded `paused_containers`, `backup_jobs` and `links`; `index()` reads it once per page
and validates each field on its own (links through the schema's `LaunchLink`, jobs by the
schema's rules), so one bad field falls back alone. A pause edit re-derives each container's
issue, so the fleet cells, the healthy count and the unhealthy count move together; stack
completeness stays scan-time until the next scan. Without core's answer -- core down, and
always for the unauthenticated `/api/widget` -- pause decisions are the scan's own (Leela
excuses a stopped container only for being paused), never this process's import, which
follows the file and can be ahead of core. Gate: three adversarial rounds, the last clean.

Original finding:

`config.LAUNCH_LINKS` is computed at import, and activation (`_reexec_core`) re-execs **core
only**; nothing restarts `casa-dashboard`. An operator edits `links:` in the Config tab, the
UI reports it activated, and the dashboard serves the old value until someone restarts it by
hand.

**This is not a links bug — links joined it.** `config.PAUSED_CONTAINERS` and
`config.BACKUP_JOBS` are read at module level in `dashboard_data.py` and are stale in exactly
the same way today. All three of `EDITABLE_FIELDS` that the dashboard reads are affected.

Direction: source them from core rather than from the dashboard's own import, so the
dashboard shows what is **enforced** rather than what merely sits on disk. That distinction
already has teeth — the deploy template fails a release when core's loaded sha does not match
the file, for this reason. Restarting `casa-dashboard` from core is the alternative and is
worse: it needs sudo that core does not otherwise want, and it bounces the operator's session
on every edit.
