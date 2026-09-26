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

Server-side, GET-only, 3s timeout, 30s cache, keys from the host secrets file and never into
the page. This is the one part of v2.2 with a security surface: a new outbound fetch path with
credentials. **Do not land this unreviewed.**

### T46.4 — Launch links in the UI

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

### 2. The dashboard never re-reads edited config

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

## Status, 2026-09-26 evening

v2.1.1 is deployed and verified. Since then, on `main`:

| | |
| --- | --- |
| `ab89b2b` | the approvals 503 (shipped in v2.1.1) |
| `7228984` | four gate findings from T45.5/T46.2 |
| `dbd849a` | **SCAN scans** — the header button was `<a href="/">` |
| `bb4d871` | the VPN sensor alerts on stuck, not on rotating |
| `46ab9bf` | T46.1's join rewritten onto the container address |

### Scan on demand

Chris asked for it mid-v2.2 and chose the full pipeline, the same one Telegram's
`/check` runs, so SCAN means one thing wherever it is pressed. `admit_pipeline_run()`
now answers synchronously — run_pipeline could only report a refusal by sending a
Telegram message, which is no use to someone holding a mouse button.

### The VPN sensor

The alert Chris approved on 2026-09-26 was a true positive about a condition that
fixes itself. ProtonVPN rotates the forwarded port every 2h; a refused NAT-PMP renewal
(7 of 24 over 48h) leaves it empty until the next cycle. The check now asks gluetun's
log how long the port has actually been gone. Replayed over the real 48h: 0 alerts,
where the shipped version alerted on all 6 gaps.

**The first attempt was wrong and the gate killed it.** Carrying an "unhealthy since"
timestamp between scans cannot establish continuity: all three of 2026-09-26's samples
were unhealthy, but gluetun held a port for hours between them. It would have alerted
claiming six continuous portless hours that never happened — and the test only passed
because it inserted healthy scans the real scheduler never performs. Worth remembering:
a fixture that makes the sensor look right is the same defect as a tile that does.

### What is left

| Slice | State |
| --- | --- |
| T46.1 join | done, **last gate round owed** (quota, resets 09-27 00:58) |
| T46.2 registry | done and gated |
| **T46.3 fetcher** | **not started — the one security surface, must not land unreviewed** |
| T46.4 links in the UI | **pills done** (`cc55996`); tile `↗ n`, drawer and detail buttons blocked, see below |
| T46.5 widget frame | not started |
| T46.6 retire homepage | not started |

Also owed: the `links:` contract changed to container names, so the live config's
eventual entries must use `CASA_*`, not service names.


## T46.4's remaining blocker: containers do not know their stack

The Overview tile's `↗ n` and the 420px stack drawer both need "which launchable
containers are in this stack". Nothing in the snapshot answers that:

* `stack_completeness.services` is keyed by compose service and holds only
  `{status, state}` — no container name;
* `containers[]` holds `name`, `status`, `image` — no compose project or service.

So a stack cannot be joined to its containers, and launch links are keyed by container.
The fix is two fields on `check_containers()`'s existing `docker ps --format`:
`{{.Label "com.docker.compose.project"}}` and `{{.Label "com.docker.compose.service"}}`.
Targeted labels, not `{{.Labels}}` — that is 111KB across 85 containers and `.Labels` is
a string, not a map, so `index` does not work on it.

It is a small change in scan-path code that runs unattended, which is why it did not go
in ungated at 21:30. It also unblocks the `planetexpress.widget` label T46.2 needs.

The container detail header's `OPEN ↗` / `WEB ↗` is a separate, smaller piece: that page
resolves its container through core rather than from ctx, so the URLs have to ride along
on `/api/containers/<stack>/<service>` rather than being rendered server-side.
