# T46 — v2.2 launch links and container widgets

> **Resume here.** Branch `claude/pensive-keller-bj1gkg`, last pushed at the commit that
> added this note; no PR is open. Read "Before 2.2 ships" at the end of this file first: it
> lists the open owner decisions, the live-host checks, and the Codex pass that has not run.
> The design spec is `docs/designs/planet_express_design_v22/handoffs/V2.2-LAUNCH-LINKS-AND-WIDGETS.md`;
> the design harness is `python scripts/design_preview.py --port 8773` (fixtures, no host).

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

**Status: built** (`feat(dashboard): widget frame and states`). The widget sits second on the
detail view, after the verdict and before vitals. States: ok (stats, then rows or a line),
needs a key (names the exact env vars, says keys stay on the host), error (reason, the last
good values dimmed, "never marks the container as down"), and none (no box at all). Two
review rounds, Claude standing in for Codex under the user's standing permission: one high
(the 5s detail poll rewrote the log well's class and dropped the fold, leaving logs visible
but frozen) and five mediums (a failed refresh left a live-looking widget; `aria-live`
re-read the box every 30s; the skeleton re-armed every poll on widgetless containers; a
widget that went away left the logs folded; logs folded on key/error states) -- all fixed,
then a second round found one more medium (the screen-reader status still said "live" after
a failed refresh), fixed. Checked in the design harness at 1440×1000 and 390×844, including
the fold surviving a detail poll, a 503 run, and a widget going to none.

Deviations from the spec, deliberately:
- Vitals stay the v2.1 two cards (CPU, MEMORY), not the 3-up CPU · MEMORY · RESTARTS row;
  restarts are already in FACTS.
- The loading skeleton appears only if the page's first answer takes over 400ms. Most
  containers have no widget, and a skeleton on every one of them is the empty box the spec
  rules out.
- Logs fold away only when the page's first answer is a working widget -- not for needs-key
  or error, where the logs are what explains it, and never later under someone reading them.
- A failed refresh after a good answer keeps the last answer, dimmed, with the beacon off and
  "could not refresh · shown Ns ago".
- Five of the first wave's eight widgets exist: sonarr, radarr, prowlarr, immich, adguard.
  qBittorrent (cookie login) and SABnzbd (key in the query string) need auth kinds the
  fetcher does not have; adding one is a change to the gated fetcher, not a widget file.
  Jellyfin's key header differs across 10.x releases and it is host-networked here, which
  the fetcher never reaches. All three wait for the live host.
- Sonarr and Radarr show QUEUE · WANTED · HEALTH: SERIES and MOVIES need `/api/v3/series`
  or `/api/v3/movie`, unpaged, only to count; Radarr's WANTED carries no warn level, since
  Radarr counts announced movies too. Radarr needs 5.6+ (`/api/v3/wanted/missing`). Prowlarr
  shows FAILING · HEALTH, with the health messages as rows: INDEXERS needs `/api/v1/indexer`
  (every indexer's schema, enough to spend the whole budget) and GRABS 24H a date in the
  path. Immich leaves out "last upload"; its key must be an admin's, with server.statistics.
- None of the new widgets' paths, ports or answers has been seen on a live host. A wrong
  one shows the error state; it cannot send a key anywhere the provenance rules refuse.

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

---

## Before 2.2 ships

State at `c361878`: T46.1–T46.5 built and pushed on `claude/pensive-keller-bj1gkg`; 1999 tests
pass; ruff shows the 10 pre-existing errors only. The CHANGELOG entry sits under
`[Unreleased]` until the live checks below pass.

**How the gates were cleared.** Every gate in this brief since T46.1 P1 #1 was a Claude
adversarial `/code-review` standing in for `codex review`, on the owner's written permission
for that session. None of this code has had a Codex pass. Running
`codex review --base main` once over the branch before tagging is cheap and recommended;
T46.1 P1 #1 (`fc4579a`) and the fetcher (`668d57c`) are where a second reviewer matters most.

**Owner decisions still open.**
1. The Leela address-collection leftover (above, under P1 #1): keep or remove.
2. **The `planetexpress.widget` label override** -- see "Decision: the widget label" below.
3. Radarr needs 5.6+; the Immich key must be an admin's. Both are in INSTALL.md.

**Live-host checks** (nothing here was run against the real host):
- Router coverage: of the 70 live routes, how many get a launch link, and is every miss
  explained (host-networked, `server.url`, a shared namespace, a compound rule)?
- The Traefik naming mirror (`traefik_normalise`, `<service>_<project>` defaults) against
  `/api/http/routers` on the host.
- Each widget's port, paths and answers against the real app: sonarr, radarr, prowlarr,
  immich, adguard. Each image's `RepoDigests` must name its publisher, or its key is never
  sent (under the containerd image store, see INSTALL.md's caveat).
- The dashboard's 5s RPC deadline against `containers.routers` and `query.widget_target`
  with ~100 containers.
- The design at 1440×900 and 390×844 with real data: stack drawer, split pills, widget states.

**Not started:** T46.6 (retire homepage) needs the live host. qBittorrent, SABnzbd and
Jellyfin widgets need new auth kinds in the gated fetcher (cookie login, a query-string key)
or a live check of Jellyfin's header.

### Decision: the widget label

**What the spec said.** A `planetexpress.widget=<name>` container label overrides the image
match; `planetexpress.widget=none` disables it (V2.2 handoff, "Matched by image repo").

**What the code does** (`widget_target` in `planet_express/integrations/rpc.py`, and
`registry.match_widget`):

| Label | Result |
| --- | --- |
| none | The image's repo picks the widget. If two widgets claim the image, none shows. |
| `none` (on the container or baked into the image) | No widget. |
| `<name>` of a widget with no auth | That widget, for any image. |
| `<name>` of a keyed widget, image in its match list | That widget, if provenance passes. Picks between two widgets that both match. |
| `<name>` of a keyed widget, image not in its match list | **No widget.** This is where the code departs from the spec. |
| `<name>` baked into the image (`LABEL` in a Dockerfile) | Ignored. Only `none` counts from the image. |

In practice: **all five shipped widgets are keyed**, so today a label can only switch a widget
off, or choose between two that both match. It cannot turn a widget on for a custom image.

**Why.** The label is written in compose, and compose is exactly what `/install` drafts and
the operator approves. If a label could pick a keyed widget, one line
(`planetexpress.widget: sonarr` on any container) would send `SONARR_API_KEY` to that
container: a typo, a copy-pasted compose block or an unvetted image gets the key. Gate round 4
on T46.3 found this, and requiring the image match was the fix.

**What it costs.** A keyed app gets no widget when its image is:
- a custom or forked build (`myname/sonarr`), or a local build;
- pulled through a private registry mirror under another hostname (the digest names the
  mirror, so provenance also fails -- check this on the live host);
- a publisher missing from the match list (add it to the widget file; that is the intended
  fix, and it is a one-line reviewable change).

**Options.**
- **A. Keep it (recommended for 2.2).** Safe default; the cost only bites on custom images.
  Update the V2.2 spec's line to match.
- **B. Do what the spec said.** Any label picks any widget, keys included. Simplest, and the
  key then goes wherever an approved compose says.
- **C. Consent in config, not compose.** A core-enforced `widget_overrides:` map in
  `config.yaml` (`CASA_MYSONARR: sonarr`), marked sensitive so the dashboard cannot edit it.
  The key follows the operator's config, never a compose label; bridge-only and no shared
  namespace still apply. New capability: needs the gate. Worth building only if a container
  on the live host actually needs it.
