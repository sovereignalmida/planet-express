# Planet Express 2.0 — handoff, 2026-09-18

Written for whoever picks this up next (Codex over the weekend). Everything below is verifiable from
the repo and the live host; where I'm unsure, I say so.

## Where things stand

- **Branch `v2`**, all work pushed, CI green on every commit. Latest: `ea65ed4` (T30).
- **1085 tests pass**, `ruff check .` clean. Run the suite with
  `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q`.
- **Live host** (`casaroot@192.168.1.94`, install `/home/casaroot/apps/planetexpress`, branch
  `live/deployed`) runs **`v2.0.0-2c`** + the host-only disk-monitoring commit. Healthy as of the last
  check: both units active, dashboard answering, no errors, 15 stacks complete.
- **Tags:** `v2.0.0-1a … 1c`, `2a`, `2b`, `2c`. T29/T30 are on `v2` but **not tagged or deployed**.

### Done since the /plan-eng-review (T15-T30)
Slice 2 (chat) and the slice 3 prerequisites are complete: shell-free diagnostics, secret redaction at
every egress, one shared provider tool loop, chat tickets + dashboard panel, events index + prune,
pre-upgrade snapshots + schema refusal, config validate/atomic-write/re-exec, autonomy limits,
the backup maintenance window, NFS mount auto-discovery, the `backup_jobs` setting, and (T29/T30) the
core reads plus the container detail page and restart sheet. Each has a `- [x]` entry in
`docs/designs/planet-express-2-0-slices.md` recording what landed, what deviated, and what review found.

## Next task: T31

**`docs/handoff/T31-brief.md`** — approval cards + the execution screen. It is the last piece of
landing 1c; after it, an operator can authorise a proposal and watch it run without Telegram.
The core RPCs it needs already exist (T29). Nothing is started.

After T31, in the plan's order: slice 3's incidents (fingerprints), slice 4's config UI (needs D29,
below), then slice 5a (`/up`, `/down` as typed actions) and 5b (multi-step execution; legacy shell
plans retire there). `v2.0.0` = slices 1-5 complete.

## How this project works (please keep to it)

1. **The second-review gate is mandatory** (`CLAUDE.md`): run `codex review --uncommitted` before
   landing anything, read every finding, and either fix it or say in the commit/plan why not. It has
   caught a real secret leak and a permanent UI freeze in the last two tasks alone.
2. **Rehearse on the test VM before landing anything user-visible.** `tests/homelab/vm.sh up |
   provision | push | ssh`. It is a throwaway guest shaped like the live host (same unit names, same
   user split, fixture stacks: `healthy`, `unhealthy`, `crash-loop`, `slow-start`).
3. **Record outcomes in the plan** (`docs/designs/planet-express-2-0-slices.md`): what landed, every
   deviation from the brief, and every review finding with its resolution. That file is the project's
   memory; it is also synced to `~/.gstack/projects/sovereignalmida-planet-express/`.
4. **Never deploy to the live host from an agent.** Sudo there needs Chris's password. Prepare a script,
   hand him the one command, and verify afterwards over read-only SSH.
5. **The deploy hazard:** checking out new code while `casa-dashboard` runs breaks it instantly (Jinja
   loads templates from disk per request). Stop both units, swap code, start both — one sitting.

## Open decisions (need Chris, not an agent)

- **D29 config ownership (blocks slice 4's config UI).** Decided in principle: core owns the config
  directory on every install; `sudo_allowlist` and `forbidden_stacks` (and now `autonomy`) are refused
  by default, behind a root-only switch `PE_ALLOW_SENSITIVE_CONFIG_EDITS=1` in
  `/etc/planetexpress.env`. Not implemented; `setup_wizard.py` still leaves `/etc/planetexpress`
  root-owned, so `ConfigService.apply` returns `write_failed` on a default install (safe, nothing
  written). Build it with slice 4.
- **T23's rehearsed downgrade is done**; no open item there.

## Known live-host items

- **`CASA_TA` (TubeArchivist) has a stale NFS handle** on `/youtube` (`/casamedia_nfs/media/youtube`),
  found by T28's new probe and confirmed by Leela on the live host. The fix is a restart of that
  container; Chris hasn't done it yet as far as I know. Until then Leela reports one HIGH finding.
- **Radarr's import errors are not NFS.** `/complete` isn't mapped into the container; its
  `/casamedia` mount probes healthy. That is a download-path mapping mismatch with qBittorrent.
- **Daily borg backup is off on purpose**; `backup_jobs: [weekly]` is set in the live config (T27).
  Don't "fix" the missing daily job.
- **The borg script was moved to root ownership** (`/usr/local/sbin/planetexpress-borg-backup.sh`) and
  `/home/casaroot/apps` is no longer world-writable — that combination let any local user, including
  the dashboard's unprivileged `planetexpress-web`, get root on the next backup run.

## Accepted debt, with the reasoning (don't undo these blind)

- **`redact()` costs ~4µs/char** (`TODOS.md`). It bounds log reads: 500 × 8 KiB records hit the
  caller's deadline, so the log view returns fewer lines than its caps allow. The fix is to scan for
  separators and walk back a *bounded* identifier instead of matching at every position. **Every bound
  in that file exists because a review found a quadratic path, and its matching rules exist because 14
  review rounds found leaks.** Keep `tests/test_redact.py` green in full, including the seven
  `test_no_superlinear_path` guards, and add a perf test before touching the matcher.
- **Redaction withholds the rest of a line after a sensitive key**, losing innocent content
  (`TODOS.md`). Deliberate: every attempt to find where a value ends leaked.
- **Chat's proposal label says "restart stack/service" literally** — tickets don't store the proposal
  target (T21b).
- **Legacy LLM shell plans** still approve only in Telegram. They are the last unenforced safety
  boundary and retire in slice 5b.

## Things that bit me (so they don't bite you)

- `git cherry-pick` has no `-q` flag; my first live deploy script died on it (the rollback worked).
- `findmnt` exits **1 with no output** when nothing matches — that is "no NFS mounts", not an error.
- Codex's sandbox can't bind AF_UNIX sockets, so it reports ~38 RPC test failures that pass locally.
  Always re-run the suite outside the sandbox before believing a red result — and before believing a
  green one, since socket tests it skipped have caught real breakage (a stale `unknown_method` test).
- `crypto.randomUUID()` is unavailable over plain HTTP, which is how the dashboard is served on the LAN.
- Werkzeug's test client needs a `MultiDict` for repeated form fields, and `csrf(client)` in
  `tests/test_scruffy_routes.py` issues its own request — clear `rpc.calls` after calling it.
