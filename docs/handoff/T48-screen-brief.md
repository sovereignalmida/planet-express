# Design brief: other hosts

Paste-ready for Claude Design. Everything below is measured from the real fleet on
2026-10-01, not invented.

## What Planet Express is, and what already exists

Planet Express is a self-hosted dashboard for a home server. It already renders the host
it runs on — `casamediaserver` — from the local Docker socket: stacks, containers, launch
links, widgets, and working controls (start, stop, restart, stack up/down behind an
elevated session). That screen is finished and is not being redesigned.

The design system is the **Cockpit** system already in the package: `system/cockpit.css`,
`system/COMPONENTS.md`, and the shipped v2.1–v2.3 handoffs. Match it. The first 582 lines
of `static/cockpit.css` are sealed byte-for-byte and cannot change, so new work must
compose from existing tokens and components, or add new ones *after* line 582.

## What this screen adds

**Other hosts on the LAN, read-only.** Planet Express reads an existing beszel hub's API
and shows the machines that are not this one. There are no controls of any kind on this
screen — it is observe-only by design, and acting on a remote host is a separate piece of
work with its own security model.

The host Planet Express runs on is **deliberately absent** from this screen. It is already
rendered elsewhere with real controls; showing it again here would mean two cards for one
machine with two sets of numbers collected different ways, which will not agree.

## The fleet, as it really is

| host | hardware | containers | notes |
|---|---|---|---|
| CASA UNRAID | 2 cores / 2 threads, x86_64, 7.6 GiB | 5 | older agent: reports **no OS name at all** |
| CASA MAC MINI | 2 / 2, x86_64, Ubuntu 24.04.2, 3.6 GiB | 6 | |
| CASA SOLAR ASSISTANT | 4 / 4, **aarch64**, Debian 12, 907 MiB | **0** | an appliance; genuinely runs no containers |

Real container names on Unraid: `CASA_ADGUARD_SECONDARY`, `CASA_UNRAID_DOCKERPROXY`,
`airconnect`, `beszel-agent`, `homepage`. Container names are long and
`SCREAMING_SNAKE_CASE` in this house — design for that, not for `nginx`.

## What there is to show, per host

**Identity:** display name, and a link out to that host's own UI. The link comes from
local config, never from the remote host.

**Hardware:** hostname, cores, threads, architecture, kernel, CPU model, total memory, OS
name. Any of these can be missing — Unraid's OS name is empty right now.

**Metrics:** CPU %, memory % (and used/total GiB), disk % (and used/total GiB), swap, three
load averages, and a map of temperature sensors to °C (one host reports 8 sensors:
`acpitz` 27.8, four `coretemp` cores 57–60, a package reading, a `dell_smm` reading).
Fan speeds exist too. Any of these can be missing.

**Containers:** name, image, status text (e.g. `Up 6 weeks`), health, CPU, memory, network,
ports, and an "update available" flag the collector already computes.

## The five states to design — four of them are failures

This is the unusual part of this screen. The happy path is the least interesting state, and
the project has a hard rule behind it: **unreadable is not absent, and absent is not zero.**
A value nobody could read must never render as `0`, as blank, or as a dash that looks like
a measurement. It has shipped four separate bugs in this project.

1. **Current** — read moments ago. Everything present. The straightforward case.

2. **Stale** — this host reported once and has not recently. Show *how old* the reading is.
   A stale number must not be able to pass for a live one at a glance.

3. **Unknown** — in config, so it always renders, but nothing could be read. **It carries a
   reason, and the reasons are materially different:**
   - the collector could not be reached at all
   - the collector answered fine, but **our account is not permitted to see this system** —
     which returns success and an empty list, and looks exactly like "you have no hosts"
   - the reading's timestamp is unusable (a host whose clock is far wrong)

   These need to be visibly different, because they lead to different fixes. The middle one
   cost real time during setup precisely because nothing looked wrong.

4. **Zero containers** — Solar Assistant runs none. This must read as "this host has no
   containers", not as an empty state caused by a failure. It is a healthy, normal host.

5. **Unconfigured** — a host beszel knows about that our config does not list. A new machine
   appearing should be *visible* rather than silently ignored, but it has **no link** (links
   only come from config) and it should read as "known to the collector, not yet set up
   here". Its name is text supplied by a remote machine, so treat it as untrusted display
   text.

A sixth situation worth a thought: when the collector is entirely unreachable, *every*
configured host is in state 3 at once. A page of unknowns should still look deliberate
rather than broken.

## Constraints

- No controls, no buttons, no actions. Read-only.
- Do not restructure the existing dashboard screen; this is additional.
- Cannot modify the first 582 lines of `static/cockpit.css`.
- Must work at the sizes the existing dashboard supports.
- Hosts differ wildly in size: one has 85 containers, one has none. The layout has to
  survive both without one looking broken.

## What to hand back

A handoff in the same shape as the existing `handoffs/V2.*.md` files — the states, the
components, the tokens used, and the reference render — so it can be implemented directly
against the Cockpit system.
