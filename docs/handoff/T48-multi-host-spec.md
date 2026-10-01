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

| name                 | address       | agent  | containers reported |
|----------------------|---------------|--------|---------------------|
| casamediaserver      | 192.168.1.94  | 0.20.0 | 85                  |
| CASA UNRAID          | 192.168.1.171 | 0.17.0 | 5                   |
| CASA MAC MINI        | 192.168.1.79  | 0.20.0 | 6                   |
| CASA SOLAR ASSISTANT | 192.168.1.154 | 0.20.0 | 0 (stats only)      |

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
  UI, carrying stats and container list but no controls.
- The local host keeps its existing behaviour untouched. It reads the docker
  socket directly and must not start depending on beszel.

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
- Solar Assistant reports zero containers and is the one host still in SSH-listener
  mode. Worth understanding why before phase two assumes WebSocket everywhere.
- Agent version skew: Unraid on 0.17.0, everything else on 0.20.0.
- Whether `updatable` should surface in PE. beszel already computes it, and we
  did that work by hand on 2026-10-01.
