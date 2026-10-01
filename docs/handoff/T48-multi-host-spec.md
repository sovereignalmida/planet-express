# T48 · Other hosts — retiring the beszel UI

Replaces the third leg of the homepage/dockge/beszel migration. Homepage is gone
from this host (T46) and dockge is gone (T47). This one is different from both:
dockge and homepage were things PE could simply absorb, because everything they
knew was already readable from the local docker socket. The other hosts are not.

Everything in "What is actually true" below was measured on the live host on
2026-10-01, not assumed. Read it before designing anything, because the
connection direction is the opposite of what it looks like.

## What is actually true

beszel is already deployed and already watching four hosts:

| system id         | name                 | address       | agent  | containers reported |
|-------------------|----------------------|---------------|--------|---------------------|
| `n7n7ppta55karj9` | casamediaserver      | 192.168.1.94  | 0.20.0 | 85 — **this host**  |
| `ptf3tn2gzpg913i` | CASA UNRAID          | 192.168.1.171 | 0.17.0 | 5                   |
| `vw53pk01zei80wt` | CASA MAC MINI        | 192.168.1.79  | 0.20.0 | 6                   |
| `7y4fosy0ebhtk9x` | CASA SOLAR ASSISTANT | 192.168.1.154 | 0.20.0 | 0 (stats only)      |

Those ids are `systems.id` and are the stable handle. The name is editable in
beszel's UI and the address can change, so neither is an identity.

The hub runs in the `services` stack on :8090, behind traefik at
`beszel.casalan.com`. The local agent runs `network_mode: host` with
`/var/run/docker.sock` mounted **read-only**.

**The agents dial the hub. The hub does not dial the agents.** From the agent log:

```
INFO Starting SSH server addr=:45876 network=tcp
INFO WebSocket connected host=192.168.1.94:8090
INFO Stopping SSH server
```

An 0.20.0 agent opens an outbound WebSocket to its `HUB_URL`, and once that is up
it shuts down its own inbound listener. Port 45876 is refused on three of the four
hosts. Solar Assistant still answers there, so the fleet is running mixed
connection modes.

The consequence that drives this whole spec: **an agent registers with exactly one
hub**, via one `HUB_URL` and one `TOKEN`. There is no way for PE to passively
observe a fleet that is already pointed somewhere else. PE must either read the
hub, or become the hub.

Three further measured facts:

- `restart: unless-stopped` is **commented out** on both `beszel` and
  `beszel-agent` (`services/docker-compose.yml:320` and `:337`). Neither survives
  a reboot. Anything that depends on beszel must fix that first.
- `homepage` is **still running on Unraid**, up 6 weeks. The homepage retirement
  is complete on this host only.
- beszel's `containers` table already carries an `updatable` boolean. It tracks
  per-container image-update availability.

## Decisions already taken

**Read the hub now; keep becoming the hub possible later.** PE reads beszel's
PocketBase REST API. beszel stops being a thing anyone visits and becomes a
headless collector behind PE. Nothing changes on Unraid, the Mac Mini or the
Solar Assistant appliance. PE's host/metric model is designed behind a provider
seam so the collector can be swapped later without touching routes, templates or
stored shape.

Rejected for now: PE implementing the agent WebSocket protocol and repointing all
four agents. That is a true replacement, but it means owning an undocumented
protocol against a fleet with version skew (0.17.0 and 0.20.0), on an Unraid box
and an appliance, with agent auto-update as a standing source of silent breakage.
Revisit only if beszel's container merely existing turns out to bother us.

**Observe only. Design the acting path, build none of it.** PE shows remote hosts,
their stats, their containers and a link out to each host's own UI. No control of
anything that is not this host. The acting model is specced below so the read model
does not foreclose it.

## Decisions taken here, with reasons

**Remote container labels are not ingested, and the data model already enforces
it.** beszel's `containers` table carries only `cpu`, `health`, `image`, `memory`,
`name`, `net`, `status`, `ports` and `updatable`. No labels. So remote icon
resolution must run off `image` alone through the existing
`icons.slug_for(image, labels=None)` path. This is the outcome we would have chosen
anyway: a label on an Unraid container is authored in a different trust domain, and
PE already lets labels drive icon selection. Nothing from another host may steer
rendering on this one. If a future collector does expose remote labels, they stay
untrusted and out of the icon path.

**A host PE cannot read is shown as unreachable, never as absent and never as
zero.** `systems.status` is one of up/down/paused/pending, and `systems.updated` is
a timestamp. PE treats a reading older than twice the collection interval as stale
and renders the age instead of the number. A host missing from the collector's
answer is rendered as unknown, not dropped from the list. This is the
"unreadable is not absent" rule that has already caused four separate bugs in this
project: an empty container list, unreadable labels, cancelled downloads, and
containers that were simply not seen.

**Which means the host list is configuration, not collector output.** Stating the
rule above is not enough to get it: if the only source of host identity is the
collector's answer, then a host absent from that answer has no name, no link and no
row, and it silently disappears — which is the bug, not the fix. A collector that
is down takes the whole list with it, and a dashboard restart loses even the memory
that the host existed.

So PE carries an **expected-host inventory** in config: for each host, the beszel
system id (stable, `systems.id`), a display name, and the URL of that host's own
UI. No entry declares whether it is the local host — locality is derived, below.
PE renders that inventory. Live values are looked up per host by system id and
filled in; a host the collector does not answer for renders from the inventory with
its state as unknown and the reason shown. The inventory is also the only source of
the outbound link — beszel stores no link, and a link is exactly the field we would
not want a remote host to be able to set.

A host the collector reports that the inventory does not list is surfaced, not
hidden: a new agent appearing should be visible rather than silently ignored. It
renders with its collector-supplied name and no link, flagged as unconfigured.
Names from the collector are display-only text and get the same treatment as any
other remote string.

**beszel being down is the whole-fleet unreachable case, not an error page.** PE
renders every host as unknown with the reason, and the local host keeps rendering
from the local docker socket, which does not depend on beszel at all.

## What this adds

- A `HostProvider` seam with one implementation, `BeszelHubProvider`, reading
  the PocketBase API over HTTP on localhost. One method to list hosts, one to list
  a host's containers. Returns PE's own types, never PocketBase records.
- A read-only beszel service account for PE. Note the `systems` listRule:
  `@request.auth.id != "" && users.id ?= @request.auth.id`. A user only sees
  systems they are listed on, so creating the account is not sufficient — it must
  be added to each system's `users` relation. Credentials go in
  `/etc/planetexpress-dashboard.env`, root-only, like every other secret.
- Remote hosts surfaced as their own entries, each with a link to that host's own
  UI from the inventory, carrying stats and container list but no controls.
- The local host keeps its existing behaviour untouched. It reads the docker
  socket directly and must not start depending on beszel.

  **And it must not appear twice.** The hub's own system list includes
  `casamediaserver` at 192.168.1.94, which is this machine — the same host PE
  already renders from the docker socket with working controls. Taking every
  `list_hosts()` result as a remote entry shows this host twice: once controllable
  and once as read-only remote data, with two sets of numbers collected different
  ways that will not agree.

  **Which system is local is derived, not declared.** Three review rounds were
  spent trying to validate an operator-declared `local: true` flag, and each fix
  left a way for the flag to be wrong: unique ids and a single flag are cardinality
  checks, and they pass happily when the flag points at the wrong system. A
  declared answer to a question PE can answer itself is a defect generator.

  PE already knows its own containers exactly, from the docker socket. So the local
  system is the one whose reported container set matches that. Measured on
  2026-10-01:

  | system               | container names shared with the local docker socket |
  |----------------------|-----------------------------------------------------|
  | casamediaserver      | 85 of 85 — exact                                    |
  | CASA UNRAID          | 1                                                   |
  | CASA MAC MINI        | 0                                                   |
  | CASA SOLAR ASSISTANT | 0                                                   |

  The separation is not close, but note the Unraid 1: that is `beszel-agent`, a
  container name that exists on every host. Any-overlap matching would be wrong.
  Name and address are not identity either: beszel's name is editable in its UI and
  addresses change.

  **The predicate, stated so two implementations cannot disagree.** Let `L` be the
  set of container names from the local docker socket, and for each system `S` the
  set of container names the collector reports for it. Compare by exact name.

  - `coverage(S) = |S ∩ L| / |L|` — the denominator is always the local set, never
    the remote one, so a host reporting thousands of containers cannot win by volume.
  - Derivation is **not attempted** when `|L| < 5`. Too few names to discriminate,
    and the answer would turn on one coincidence.
  - A system is the local one when `coverage(S) >= 0.5` **and** `|S ∩ L|` is at
    least three times the second-best `|S ∩ L|`. On the measured data: coverage
    1.0 and 85 against a runner-up of 1.
  - Anything else — no system clearing 0.5, two systems within the 3× margin, or a
    tie — leaves locality **unknown**. No guessing.

  Config may pin the local system id as an optional override for an operator who
  needs one. It is a single pinned id, not a per-entry boolean. If it is pinned and
  derivation disagrees, that is a config validation failure refused at apply time —
  loudly, because one of the two is wrong and PE cannot tell which.

  **While locality is unknown, configured hosts still render; unconfigured ones do
  not.** This is where the earlier draft of this section had it backwards. It said
  no remote host renders at all, which hides every host in the inventory exactly
  when the collector is failing — the opposite of the invariant six paragraphs up,
  and the same "unreadable is not absent" mistake a third time. Correct behaviour:
  every inventory entry renders, with unknown state and the reason, because its
  identity comes from config and does not depend on the collector. What is withheld
  is only the *unconfigured* collector rows, since surfacing those before locality
  is known is precisely what would render this host a second time.

  **An empty inventory means PE does not query the collector.** Not "queries it and
  shows nothing" — that is the state where every returned row is unlisted, including
  the local one, and the surface-unlisted rule would recreate the duplicate. Off
  means no request, so there are no rows for any rule to act on, and the local host
  renders from the docker socket exactly as it does today.

## The read contract

Measured against the live hub on 2026-10-01. PE reads four collections over HTTP on
localhost and nothing else.

**Auth.** `POST /api/collections/users/auth-with-password` with `identity` and
`password` returns a token. Confirmed present: an empty body answers 400 with
per-field validation, not 404. Credentials from
`/etc/planetexpress-dashboard.env`. Every collection PE reads has a `listRule` of
the form `@request.auth.id != "" && users.id ?= @request.auth.id`, so the account
sees only systems it is listed on — it must be added to each system's `users`
relation, which is a step beyond creating it.

**`systems`** — identity and liveness. Fields `id`, `name`, `host`, `port`,
`status`, `updated`. `status` is one of up/down/paused/pending. `updated` is the
staleness input.

**`system_details`** — hardware, and all of it named. `system` (the relation),
`hostname`, `cores`, `threads`, `arch`, `kernel`, `cpu` (model string), `memory`,
`os_name`, `podman`. One row per system, verified 1:1 across all four. This is what
a host card shows.

**`system_stats`** — the time series. `system` (the relation), a `stats` JSON blob,
and a `type` column that buckets it. Buckets present: `1m`, `10m`, `20m`, `120m`,
`480m`. PE reads the newest `1m` row **for that system**. The `1m` bucket retains
about 85 minutes; the longer buckets are history and phase one does not need them.

**`containers`** — `system` (the relation), `name`, `image`, `status`, `health`,
`cpu`, `memory`, `net`, `ports`, `updatable`. No labels, which is why remote labels
cannot reach the icon path.

### Every per-system read is filtered and sorted explicitly

Each of the three child collections carries a `system` relation, and a read that
omits it is not merely incomplete — it silently attaches the wrong host's data.
"The newest `1m` row" without a filter returns the newest row in the *fleet*. At
the time of writing that row belongs to CASA MAC MINI, so an unfiltered read would
paint the Mac Mini's CPU, memory and disk onto whichever card was being rendered,
with no error anywhere.

    GET /api/collections/system_details/records?filter=(system='<id>')&perPage=1
    GET /api/collections/system_stats/records?filter=(system='<id>'%26%26type='1m')&sort=-created&perPage=1
    GET /api/collections/containers/records?filter=(system='<id>')&perPage=500

The `sort=-created` is required, not a default: without it the ordering is
unspecified and "newest" is whatever the server returns first. `?expand=system`
works and `perPage=500` is accepted.

### Decoded stats keys, verified against the host

| key  | meaning                       | check against ground truth |
|------|-------------------------------|----------------------------|
| `cpu`| CPU percent                   | —                          |
| `m`  | memory total, **GiB**         | 15.54 vs 15.5              |
| `mu` | memory used, GiB              | —                          |
| `mp` | memory used percent           | 51.25 vs 51.47             |
| `d`  | disk total, GiB               | 136.45 vs 136.4            |
| `du` | disk used, GiB                | 109.52 vs 109.5            |
| `dp` | disk used percent             | 84.6 vs 85                 |
| `la` | load averages, 3 values       | 4.12/4.52/5.1 vs 4.40/4.57/5.11 |
| `t`  | map of sensor name → °C       | `acpitz` 27.8 vs 27.8      |
| `s`/`su` | swap total / used, GiB    | —                          |
| `dr`/`dw`| disk read / write         | —                          |
| `b`  | network sent / recv           | —                          |
| `ni` | per-interface counters        | —                          |
| `cpus`| per-core percent             | 4 values on a 4-thread host|

Note `memory` in `system_details` is **bytes** (16688291840) while `m` in `stats` is
**GiB** (15.54). Same quantity, different unit in different tables.

### Do not read `systems.info`

`systems` also carries an `info` JSON summary with single-letter keys. PE must not
depend on it. Several keys could not be decoded against ground truth, and one is
actively dangerous: **`t` is the integer `4` in `info` and a sensor-name→temperature
map in `stats`.** The same letter means different things in two blobs of the same
application. Anything reverse-engineered from abbreviations in an app we do not
control is a silent-breakage source on the next agent update, and `system_details`
already provides the same facts under real names.

### Fields can be empty

Unraid reports an empty `os_name`, and its agent is 0.17.0 against 0.20.0 elsewhere.
A missing field renders as unknown, never as a zero or a blank that looks like a
measurement. Same rule as a missing host.

### beszel does not promise a stable API

`beszel.dev/guide/rest-api` documents no schema. It defers entirely to PocketBase and
states that the structure and content of returned data **may change in minor
releases**. So nothing here is documented upstream: the `containers.health` codes, the
`systems.info` keys, `updated` semantics and the stats buckets were all established by
measuring a live host, which is the only source of truth that exists for them.

Two consequences. PE must treat every field as possibly-absent and degrade to unknown
rather than failing — which is the posture the provider already takes. And the beszel
images should be **pinned to explicit versions rather than `:latest`**, because a minor
release can change field shapes under a container that now restarts automatically. That
is a host decision for Chris, recorded here and in the deploy notes rather than taken.

### Readings are aged on the stats row, not on `systems.updated`

Stated earlier in this spec as `systems.updated`, and corrected here. The hub keeps
touching the system record after an agent stops reporting, so `systems.updated` can stay
fresh for a host that is no longer sending anything — a host that looks current while
being silent. The age therefore comes from the newest `1m` stats row's `created`, which
only moves when a reading actually arrives. Pinned by a test in S2.

### The staleness threshold has a measured basis

The `1m` bucket is a 60-second interval, so "older than twice the collection
interval" is 120 seconds. That number comes from the bucket type, not from guessing
at `info.dt`, whose meaning is undetermined.

## What it must not do

- Must not put remote containers in the same list as local ones in a way that lets
  a remote container be mistaken for a controllable one. If there are no buttons
  there is no confusion, which is most of why phase one is observe-only.
- Must not let any remote value reach the action layer. No remote stack or
  container name may be resolvable by `actions.resolve_stack_target()`.
- Must not add anything to `actions.REGISTRY`. Adding an action is adding a
  capability and gets its own spec.
- Must not block the monitoring scan on a network call to the hub. The icon warmer
  already taught this lesson three times: budget it, bound it, then get it off the
  critical path entirely.
- Must not write to beszel. The service account is read-only and PE holds no
  write path to another host's state.
- Must not log or render the agent `TOKEN`, the hub's `id_ed25519`, or the service
  account password.
- Must not let any part of the inventory's meaning depend on `model_fields_set`.
  Config changes are compared by dumped **value**, so a rule that turns on whether
  a field was written is invisible to the diff: it could be changed by deleting a
  line, with no diff, no locked field and no passphrase. That was the sharpest
  finding of T47 and the fix was to remove presence-dependence entirely. An
  inventory is a list of explicit entries for exactly this reason.

## The acting path, specced and not built

Four questions a future remote-action spec has to answer. Recording them now so
the read model does not quietly decide them.

1. **Does the host-mutation lock span hosts?** Today one lock serialises mutations
   on this host. Two defensible answers: one global lock, which is safe and makes
   a slow remote host block local work; or one lock per host, which needs the lock
   identity to carry a host and needs a story for an action that touches two.
2. **What risk class does a remote action carry?** A remote `stack.down` is harder
   to recover from than a local one, because PE cannot see the host it just broke.
   Starting position: every remote mutation is R3 regardless of its local class.
3. **Is an elevated session elevated everywhere?** The elevation marker currently
   binds device token, operator epoch and passphrase fingerprint. Per-host
   elevation means a fourth component and a visible host in the prompt. Global
   elevation is simpler and strictly more permissive.
4. **What happens to an in-flight action when a host goes away?** Unknown is not
   failed. An action whose host vanishes mid-run has an unknown outcome and must
   be reported that way rather than retried.

A fifth, which is really a prerequisite: acting on a remote host needs a channel
that beszel's agents do not provide. They are read-only by design and mount the
docker socket `:ro`. Remote action is a second transport, not an extension of this
one.

## Still open

- Whether beszel's container existing at all bothers Chris enough to justify
  phase two. Decide after phase one is live.
- The Unraid `homepage` instance. Out of scope here but the homepage retirement is
  not finished while it runs.
- Solar Assistant reports zero containers because it is not a docker host: an
  aarch64 Debian 12 appliance with 951 MB of RAM. Its `system_details` are complete
  and its stats are current, so it is a first-class host that simply has no
  containers — which is the case the UI has to render without looking broken. Closed
  as a question, kept as a UI requirement.
- It is also the one host still answering on 45876 while the other three have shut
  their listeners down, and it reports fine either way. The conclusion for phase two
  is not "find out why" but "the hub serves both modes, so anything replacing the
  hub must too."
- Agent version skew: Unraid on 0.17.0, everything else on 0.20.0, and Unraid is
  the host with the empty `os_name`. Older agents report less, which is a reason the
  renderer treats absent fields as unknown rather than assuming every host answers
  every field.
- Whether `updatable` should surface in PE. beszel already computes it, and we
  did that work by hand on 2026-10-01.
