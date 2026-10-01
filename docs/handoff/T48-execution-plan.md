# T48 execution plan and live state

Spec: `T48-multi-host-spec.md`. Read it first; it is measured against the live hub
and its decisions are settled. This file is the execution state. **Update the status
table in the same commit as the work** — a session that picks this up cold must be
able to trust it.

Branch `t48/multi-host`, worktree `/home/chris/Projects/casalab/code/pe-t48`.
The main checkout `code/planet-express` belongs to another session; do not work there.

## Status

| slice | what | state |
|-------|------|-------|
| S0 | host prereq: beszel survives reboot + a read-only account | **handed to Chris** |
| S1 | core types, locality predicate, staleness — pure, no I/O | not started |
| S2 | `BeszelHubProvider` + a fixture provider built from real captured data | not started |
| S3 | config inventory: schema, validation, secrets | not started |
| S4 | wire into the dashboard data path, off the scan's critical path | not started |
| S5 | real templates from Chris's design | blocked: needs S4's rough shape first |
| S6 | ship prep: CHANGELOG, version, deploy script, INSTALL | not started |

## Division of labour, and why

The Anthropic weekly window is the binding constraint (76% used on 2026-10-01, resets
Oct 5 05:00Z, Pro plan, stop at 5% remaining). Codex runs on a separate provider
quota (`gpt-5.6-terra`, ChatGPT auth), and Chris has cloud credits that are separate
again. So:

- **Opus (this session):** slice boundaries, the trust boundary, reading findings,
  integration judgement. Never mechanical work.
- **Cloud agents:** bulk implementation per slice, against the slice contract below.
- **Codex:** `codex review --commit <sha>` before every landing. Standing project
  gate, not optional. Read every finding; fix it or record why not.

## Slice contracts

Each slice: its own commit, its own Codex review, tests that fail if the guard is
removed. Nothing lands with a known finding unrecorded.

### S1 — core, pure, no I/O
`planet_express/core/hosts.py`.

- Types PE owns, never PocketBase records: `Host` (id, name, link, status, updated,
  details), `HostDetails` (hostname, cores, threads, arch, kernel, cpu_model,
  memory_bytes, os_name), `HostMetrics` (cpu_pct, mem_pct, mem_used_gib,
  mem_total_gib, disk_pct, disk_used_gib, disk_total_gib, load, temps),
  `RemoteContainer` (name, image, status, health, cpu, memory, net, ports, updatable).
- `Liveness`: one of `current`, `stale`, `unknown`, with the reason and the age.
  `STALE_AFTER = 120` seconds, which is twice the measured 1m bucket interval.
- `locality(local_names, by_system) -> str | None`, exactly as the spec states:
  coverage over the **local** set, `>= 0.5` and `>= 3x` the runner-up, not attempted
  below 5 local names, ambiguity returns None. Pure function, no docker, no network.
- Absent is not zero. Every optional field is `None`, never `0` or `""`.

Tests must cover: the measured case (85/85 vs 1 vs 0 resolves), a tie, two hosts
inside the 3x margin, fewer than 5 local names, an empty remote set, and a host whose
only overlap is `beszel-agent`.

### S2 — the provider
`planet_express/integrations/beszel.py`.

- `HostProvider` protocol: `hosts()` and `containers(system_id)`. Returns S1 types.
- `BeszelHubProvider`: token auth via
  `POST /api/collections/users/auth-with-password`; the four queries exactly as the
  spec writes them, **with** `filter=(system='<id>')` and `sort=-created`. A query
  missing its filter is the bug that paints one host's numbers on another.
- Never reads `systems.info`. `system_details.memory` is bytes; `stats.m` is GiB.
- Every failure maps to `unknown` with a reason. Timeouts bounded. No retry storm.
- `FixtureHostProvider` reading captured JSON, used by the tests and by S4 until the
  read-only account exists.

Fixtures come from real measured data, committed under `tests/fixtures/beszel/`.
Capture them with the SQLite read recipe in the spec; scrub nothing except anything
secret (there is nothing secret in these four collections).

### S3 — config
`config_schema.py`.

- `multi_host.hosts`: list of `{system_id, name, link}`. No per-entry local flag.
- `multi_host.local_system_id`: optional pin. Pinned and disagreeing with derivation
  is a validation failure refused at apply time.
- Unique `system_id` across entries. Empty list means PE does not query the collector
  at all.
- **No rule may depend on `model_fields_set`.** Config is compared by dumped value,
  so presence-dependent meaning is invisible to the diff and changeable by deleting a
  line. T47's sharpest finding.
- Credentials from `/etc/planetexpress-dashboard.env`, root-only, never `config.yaml`.

### S4 — wiring
- Off the monitoring scan's critical path. The icon warmer taught this three times:
  budget it, bound it, then take it off the path entirely.
- Collector down renders every configured host unknown; the local host keeps
  rendering from the docker socket and must not gain a beszel dependency.
- The local system is excluded from remote entries by derived id. Unconfigured
  collector rows are withheld while locality is unknown.
- A rough template is in scope here, deliberately ugly: Chris designs against a real
  shape, not a prose brief.

### S5 — screens
Gated on Chris. The brief goes out when S4 renders real data. It needs: a host card,
a remote container list, a stale/unknown state, a zero-container host (Solar
Assistant is real and reports none), and an unconfigured-host state.

### S6 — ship
CHANGELOG, version bump, deploy script from `docs/handoff/deploy-template.sh`,
INSTALL notes for the beszel account and the env vars. Chris runs the deploy; an
agent never does.

## Hard rules

- Design seal: first 582 lines of `static/cockpit.css` stay byte-identical,
  sha256 `aa913168cde1ff5d`. Check it at every change.
- `scripts/check_design_system.py` must stay green. If it fails, change the design
  copy, never `static/cockpit.css`.
- Nothing added to `actions.REGISTRY`. Phase one is observe-only; a capability needs
  its own spec.
- No remote value reaches the action layer or `actions.resolve_stack_target()`.
- Never log the agent `TOKEN`, the hub's `id_ed25519`, or the account password.
- Host mutations are Chris's to run. Prepare a script, hand one command, verify
  read-only over SSH.

## S0, handed to Chris on 2026-10-01

1. `/tmp/t48-prereq.sh` on the host — uncomments `restart: unless-stopped` on
   `beszel` and `beszel-agent` only (wiki-go's commented copy at line 350 is left
   alone; the script aborts if it would change anything else), validates the compose
   file, recreates just those two containers, verifies the hub answers.
2. In beszel's UI: create a user, set role `readonly`, and add it to all four
   systems — the `listRule` scopes to systems the account is listed on, so creating
   it is not enough. Then put its credentials in
   `/etc/planetexpress-dashboard.env` as `BESZEL_USER` and `BESZEL_PASSWORD`.

Neither blocks S1–S4: the fixture provider carries the work until the account exists.
