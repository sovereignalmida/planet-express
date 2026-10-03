# v3 migration story — how v2 becomes v3 on the one production host

Status: **DRAFT — Revision 0.1** (2026-10-03). Not yet reviewed by Chris. Parent docs: the
Architecture Brief and its Addendum (this closes the Addendum's "migration story" open item),
`docs/designs/planet-express-2-0-slices.md` §"Prerequisite — local test homelab" (the house
methodology this doc applies to v3, not reinvents), and the three merged v3 design docs
(`phase-1-state-model.md`, `phase-3-host-control-provider.md`).

## 1. The honest starting point: there already is a migration story. v3 didn't use it.

The 2.0 slices effort established, and proved, a specific discipline before writing any slice-1
code:

> A throwaway full VM... runs the live host's distro and systemd major version, with Docker and
> the compose plugin... Each [landing] is rehearsed on the test VM, then lands alone.

`tests/homelab/` is that VM — built, and per the slices doc's own status line, "verified"
2026-09-15. Every 2.0 slice (1a, 1b, 1r, 1c, and on through the numbered `tN-*` rehearsal
scripts still in that directory) was rehearsed there before touching the live host, landed in
small reversible steps, each with a stated rollback.

**v3 Phase 1 and Phase 3 did not do this.** Phase 1 was validated by: an isolated git worktree on
the live host itself (read-only against the real fleet), then a merge into `live/deployed`, then
an actual reboot of the production host. Phase 3 was merged with no live-host rehearsal at all
(justified at the time — it's unused, additive code with nothing calling it; see §3). Neither
step touched `tests/homelab/`.

This worked out — nothing broke, and Phase 1's own goal (prove the dependency graph against the
*real* fleet's *real* compose files) arguably needed real data a fixture VM can't supply anyway.
But it was closer to the edge than the house standard calls for, and it's not how Chris would
plan the *next* landing if asked today. This doc is that plan, written honestly about what already
happened rather than pretending v3 started from a clean slate.

## 2. What the existing homelab harness can't yet rehearse

Checked directly rather than assumed: `tests/homelab/stacks/{healthy,crash-loop,unhealthy,
slow-start}` are each a single-service, single-project fixture. **None models two compose
projects with a cross-project reference** — the exact shape Phase 1's whole reason for existing
(the Gluetun/qBittorrent namespace incident) depends on. Today, the only place that scenario has
ever been exercised is the real `stacks/network` + `stacks/media` on the live host, and the
synthetic fixtures in `tests/fixtures/compose/{network,media}/` used by the unit test suite
(fast, but no real Docker, no real systemd, no real boot).

**Gap to close, concretely:** add a fixture pair to `tests/homelab/stacks/` — e.g.
`cross-network/` (a `gluetun`-shaped container with a `healthcheck` and a `container_name`) and
`cross-media/` (a service using `network_mode: container:<that name>`) — mirroring the two-project
shape the real incident had, the same way the existing four fixtures each mirror one real failure
mode (crash loop, unhealthy, slow start). This turns "does the graph correctly reorder two real
compose projects on a real cold boot" from a live-host-only question into something rehearsable
in the disposable VM, before it ever needs to touch production again.

## 3. A landing-risk tier, so not everything needs the full ceremony

Not every change carries the same risk, and treating them identically either over-tests trivial
changes or under-tests risky ones. Three tiers, each with its own minimum bar:

| Tier | Shape | Example from this session | Minimum bar before landing |
| --- | --- | --- | --- |
| **A — inert** | New code, nothing calls it yet | Phase 3's `HostControlProvider` | Unit tests + codex review. No VM, no live host. |
| **B — read/idempotent** | Runs for real, but produces no behavior change today (dry-run, or a decision that happens to be a no-op on the current fleet) | Phase 1's dependency-graph dry run; `boot_order.py`'s reordering (a no-op today since no cross-project ordering edges exist) | Unit tests + codex review, **then** rehearsed in `tests/homelab` against a fixture that *does* exercise the behavior (closing §2's gap), **then** may be validated against the live host directly if the VM can't supply real-enough data — as Phase 1 was. |
| **C — changes real behavior** | Actually reorders a real boot, actually restarts a real service, actually mutates a real file | None yet in v3 (Phase 1's reordering has never had a real constraint to satisfy on this fleet) | Full house standard: rehearsed in `tests/homelab` first, landed as its own small reversible step, explicit rollback stated before landing, **then** validated on the live host at a deliberately chosen moment (not mid-backup, as this session learned). |

Phase 1 and Phase 3 are reclassified under this table for the record: Phase 3 was correctly
tier A (no VM needed, none used). Phase 1 was tier B leaning toward C — it *should* have had a
`tests/homelab` rehearsal step before the live-host worktree test, not instead of it. The live
reboot was still the right final validation (the whole point was proving it against the real
fleet), but skipping the disposable rehearsal first meant the *first* time any of this ran for
real was already on production.

## 4. A bigger gap than "needs a merge": v3 has also skipped the tagged-deploy discipline

Codex review caught this, and it matters more than the VM-rehearsal gap in §1. The project
maintains a real `CHANGELOG.md` ("Keep a Changelog" format, Semantic Versioning) and tags each
release (`v2.5.1` is current, 2026-10-01). There is also `docs/handoff/deploy-template.sh` — a
mature, incident-derived template (its own comments cite specific production failures that
shaped each rule: a Jinja-loads-from-disk 500, a stale-attribute check that crashed three
deploys running, verify steps that only printed instead of asserting) for deploying a **tag**
to `live/deployed`, with a backup branch, a pre-deploy state snapshot, a release-specific verify
block, and `trap rollback ERR` armed through the risky window — an actual automated rollback,
not just a stated plan.

**v3's landings have used neither.** The boot-repair PR, Phase 1, the test-isolation fix, Phase 3,
and the CI widening all merged straight to `main` with no `CHANGELOG.md` entry and no tag; Phase
1 and the test-isolation fix reached `live/deployed` via a plain `git merge origin/main`, not
`deploy-template.sh`. `main` is now 22 commits past `v2.5.1` with an empty `[Unreleased]`
section. This is pure process debt, not a correctness problem — every commit was individually
reviewed and tested — but it means the one deploy mechanism this project has actually hardened
against real incidents has not been exercised for any of v3 yet.

**The fix is mechanical, not a new design:** write the `[Unreleased]` entries, cut a tag (this
doc proposes `v2.6.0` — new capability, not just fixes, per semver), and use
`deploy-template.sh` (copied and filled in, per its own header) to bring `live/deployed` level
with that tag instead of another ad hoc merge. `live/deployed`'s one host-only commit
(`7d4b441`, a disk-monitoring pattern tweak) is exactly the `LOCAL_COMMIT` the template already
has a named slot for cherry-picking back on top — this is the scenario it was written for.

**One real constraint the template doesn't remove:** every `sudo systemctl stop/start` in it
needs an interactive password. This session confirmed directly that I don't have one — running
`deploy-template.sh` is Chris's step, same as the reboot was. What I can do is prepare the
filled-in script completely (tag, `LOCAL_COMMIT`, a release-specific verify block actually
written against what this release changed, per the template's own "checks written against
behaviour, not a name" rule) so running it is a single command, not an improvisation.

## 5. Rollback

Two distinct cases, not one:

- **A tagged deploy via `deploy-template.sh`:** already solved, automated, and incident-tested
  (§4) — `trap rollback ERR` resets to the pre-deploy backup branch and restarts both units on
  any failure up through the HTTP assertions.
- **An ad hoc `git merge origin/main`** (how `live/deployed` has actually been updated so far
  this session): no automation at all. `git revert -m 1 <merge commit>` (or reset to the
  pre-merge commit, if not yet pushed), then restart whichever of
  `casa-planetexpress`/`casa-dashboard` actually changed. Not yet rehearsed against this
  specific repo's merge history.

§4's recommendation — adopt `deploy-template.sh` for v3 landings going forward — makes this
second case the exception rather than the norm, which is the better fix: closing the gap by
using the mechanism that already has rollback built in, rather than building a second one.

## 6. What this doc does not solve, on purpose

The Addendum's other open item — **provider testability** ("same codebase runs correctly on
Ubuntu/systemd *and* a MOS-based test host") — is a different problem from everything above, and
this doc doesn't conflate the two. `tests/homelab` mirrors the live host's *same* distro and init
system; it is a safe rehearsal copy of the current architecture, not a second host *type*. Phase
3's portability claim (`HostControlProvider` as an abstraction over something other than
systemd) stays unfalsifiable until an actual non-systemd test host exists — that is a separate
infrastructure acquisition, not something this migration story creates as a side effect.

## 7. Open questions (need Chris)

- **Build the cross-project homelab fixture (§2) now, as its own small piece of work, or fold it
  into whichever future landing first needs it (likely Phase 5, dependency-aware remediation)?**
  It's cheap and independently useful regardless of when; the only question is sequencing.
- **Is a second, non-systemd host (§6) worth standing up now**, even cheaply (a VM running a
  different init system, short of a full MOS install), so Phase 3's portability claim and Phase
  6's eventual MOS work share the same piece of infrastructure rather than waiting for one to be
  built for the other?
- **Adopt `deploy-template.sh` for the next `live/deployed` update now (§4), catching main up to
  a real tagged release** — this doc proposes doing it as part of landing this very doc, not as
  a someday item, since the gap it closes (no CHANGELOG entry or tag for 22 commits) only grows
  the longer it's left.
