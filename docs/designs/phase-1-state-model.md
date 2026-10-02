# v3 Phase 1 — normalized state model and dependency graph

Status: **DRAFT — Revision 0.1** (2026-10-02). Not yet reviewed by Chris. Parent docs:
`/home/chris/Projects/casalab/code/Planet Express v3 Architecture Brief.md` (§7-9, §20 Phase
1/4/5) and its `... - Addendum.md` (phase-order revision, agreed 2026-10-02). Sits alongside, not
inside, `docs/designs/planet-express-2-0-slices.md` — v3 is the architectural layer underneath the
2.0 slices, not a slice of them.

## 1. Why this exists

The Addendum's revised Phase 1 is: build the state model and dependency graph before the host-
provider interface, because designing the interface first risks shaping it around what `systemctl`
happens to expose rather than what dependency-aware reasoning actually needs. This doc is that
state model and graph.

## 2. Evidence: what's actually there today

Confirmed by direct codebase search (2026-10-02), not assumption:

- **No normalized entity model exists on `main`.** State is collected imperatively, per-agent, as
  loosely-typed dicts: `casa_leela.py` writes `state/latest_monitor.json`, validated only loosely
  by `state_models.MonitorSnapshot` (`containers: list[dict] = []`, by its own docstring "not an
  exhaustive schema of every nested dict"). `config.active_stack_dirs()` is the entire notion of
  "what stacks exist" — a glob over `STACKS_ROOT/*/docker-compose.yml`, returning `Path`s, not a
  `Stack` type.
- **No dependency graph, no `depends_on` parsing, anywhere.** `grep -rn "depends_on"` across the
  repo returns only prose (Fry and Farnsworth both *mention* `depends_on` as a compose field they
  don't carry through) and one unrelated test name about rollback-action ordering. No `DiGraph`,
  no topological sort, no `class Dependency`.
- **The only existing ordering logic is one hardcoded string match.** `casa_boot.py:38`:
  `stacks.sort(key=lambda d: (d.name != "network", d.name))` — the stack literally named
  `"network"` goes first, because Traefik/DNS/Gluetun live there. It doesn't know *why*, or which
  other stacks depend on specific containers inside it. This is the entire dependency model PE's
  boot sequence has today.
- **The namespace-reference edge type is already known to the system — as English, not data.**
  `casa_farnsworth.py`'s `PLAN_SYSTEM_PROMPT` hardcodes, in prose fed to an LLM: *"CASA_GSP and
  CASA_QBIT both run with `network_mode: container:CASA_GLUETON`... two different compose
  files... Restarting CASA_GLUETON gives it a brand new namespace; CASA_GSP and CASA_QBIT are NOT
  automatically attached to the new one and stay silently orphaned"* — followed by a manual
  4-step remediation recipe. **This is the exact incident this doc exists to prevent**, already
  diagnosed and written down by a previous session, but only reachable by an LLM planner *after*
  something has broken — `casa_boot.py`'s boot-time ordering never reads this prompt and has no
  structural awareness of it.
- **The one place `network_mode: container:x` / `service:x` is parsed at all** is
  `actions.py`'s `parse_router_owners()` and `read_widget_target()` — both read-only, ephemeral,
  built from a live `docker inspect`, used only to attribute a dashboard launch-link/widget, and
  explicitly give up (`UnresolvableNamespace`) rather than guess when the referenced container
  can't be matched by full ID. Nothing persists this as a dependency edge.
- **The `HostProvider` collision is live on `main`, not pending work — resolved 2026-10-02.**
  Checked twice: `beszel.py`'s `HostProvider` Protocol (meaning "remote host *data* source") is
  already merged to `main` via PR #3 ("T48: other hosts... through the beszel hub"). It is
  shipped, stable code with its own tests — renaming it now for a collision with something that
  doesn't exist yet would be present-day churn for a future problem. **Decision: v3 simply never
  uses the name `HostProvider`.** The brief's local-host-control adapter (Phase 3, not yet built)
  is named **`HostControlProvider`** going forward. `beszel.py` is untouched.
- **Severity/health is represented at least three incompatible ways**, which Phase 1's
  `HealthState` needs to reconcile rather than add a fourth: `HealthReading` (actions.py: raw
  docker state, `healthy|unhealthy|starting|none`), `incidents.py` observations
  (`HIGH|MEDIUM|CRITICAL` per resource-kind), and Hermes's own prompt-level scheme
  (`CRITICAL|HIGH|MEDIUM|LOW`, flattened further to two booleans by the time `state_models.Findings`
  persists it). A fourth, orthogonal axis — `Liveness` on the in-progress multi-host branch (is
  this *reading* fresh, not is the *resource* healthy) — also needs a defined relationship to
  `HealthState`, not a merge into it.

## 3. Requirement: the graph is discovered, not declared

Added 2026-10-02, non-negotiable for Phase 1's shape: PE's fleet is not static. Containers and
services get added, hardware changes, and new kinds of coupling between services will keep
showing up that nobody anticipated today (the Gluetun namespace edge is evidence of exactly this
happening once already). The model must not be a hand-authored schema someone edits when a new
stack is added. Concretely:

1. **The graph is rebuilt from live observation every cycle** — compose files, `docker inspect`,
   host state — the same way `build_dashboard_context()` is rebuilt today. It is never hand-
   maintained.
2. **Edge detection is a registry of detectors, not a closed enum.** Each known relationship type
   (compose `depends_on`, shared network, shared volume/mount, namespace reference, `links:`,
   whatever comes next) is one detector that inspects live state and emits edges of its type.
   Adding a new edge type is adding a detector, not redesigning the graph.
3. **Unrecognized relationships are a visible state, not silently dropped.** If a detector pass
   finds two resources that are clearly coupled somehow (e.g. a container references another by
   ID in a field no existing detector understands) but no detector claims the edge, that must
   surface as `unresolved dependency — no detector matched` rather than vanishing, the way the
   Gluetun edge vanished until someone hand-wrote it into a prompt. This is the actual "learning"
   mechanism: PE's own model exposes its blind spots so a detector gets added deliberately,
   on purpose, reviewed like any other code change — not an LLM inventing new relationship
   semantics at runtime.

This is also why Phase 1 is being built before the host-provider interface at all: a detector
registry that runs against "live state" needs the state model to exist first, and needs to be
provider-agnostic from day one (the same registry runs whether live state came from `docker
inspect` on this host or a future MOS API).

## 4. Goals and non-goals

Goals
1. Define the entity set: `Host, Service, Stack, Container, StoragePool, Mount, Disk, Network,
   BackupJob, Dependency, HealthState` (per the Architecture Brief §8), as real types, not dicts.
2. Define `Dependency` as a typed edge with a `kind` (registry-extensible, §3 above) connecting
   two entities, plus provenance (which detector found it, when).
3. Ship detectors for the edge types already known to exist in the real fleet today: compose
   `depends_on`, shared `network_mode` (same-project and **cross-project**, closing the Gluetun
   gap), shared bind mount/volume, Traefik router → service.
4. Reconcile the three-plus existing severity/health representations into one `HealthState`, with
   `Liveness` (reading freshness) kept as an explicit, separate field — not merged in.
5. Make the whole thing queryable in a way `casa_boot.py` can use directly: "what must be healthy
   before I start X" — this is the hook that turns Phase 1 into Phase 5's dependency-aware
   remediation later.

Non-goals (later phases per the Addendum)
- No capability/policy layer, no SOVEREIGN `execute_shell` work (Phase 2).
- No `HostProvider`/`MosHostProvider` interface design (Phase 3 — deliberately deferred so it's
  shaped by this model instead of by `systemctl`).
- No change to `casa_boot.py`'s actual boot behavior yet — Phase 1 produces the graph; using it to
  reorder boot is a following change, reviewed separately once the graph is proven against the
  real fleet.
- No MOS integration (Phase 6).

## 5. Decisions made without round-tripping to Chris

Per standing instruction (2026-10-02: "orchestrate, only consult me when you can't decide
yourself"), these were resolved directly rather than queued as open questions:

- **`HostProvider` naming — settled, see §2 above.** `beszel.py` keeps its name; v3's local-host-
  control adapter is `HostControlProvider`.
- **Detector registry placement: `planet_express/core/`.** Landed as `core/state.py` (entity
  types) and `core/dependencies.py` (graph + registry), matching `core/hosts.py` and
  `core/incidents.py`'s existing precedent (pure types/reasoning live in `core/`; the 2.0 slices
  migration is already moving logic out of the character-named scripts into exactly this layer).
  Not a new `casa_*.py` — there's nothing Leela/Hermes/etc.-shaped about a pure graph.
- **Cross-project fixture — built.** `tests/fixtures/compose/{network,media}/docker-compose.yml`,
  using the real container names from `casa_farnsworth.py`'s prompt (`CASA_GSP`, `CASA_QBIT`,
  `CASA_GLUETON`).

## 6. Open questions (not yet resolved, need Chris)

(none outstanding as of this revision — see `docs/designs/phase-1-state-model.md`'s companion
commits for what shipped against goals 1–3)
