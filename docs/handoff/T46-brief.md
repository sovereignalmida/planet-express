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
