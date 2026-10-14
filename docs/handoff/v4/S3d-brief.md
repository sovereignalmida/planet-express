# S3d — Session launcher (v4 slice 3d)

Spec: `docs/designs/v4-slice-0-contracts.md` §22 (operator-approved 2026-10-13), plus §2.1, §6, §15, §20. Start only
after slice 3c round 2 is merged into `v4-next`. `git merge --ff-only v4-next` first. Test first; `codex review
--uncommitted` before each commit; no push; trailer; never run privileged code outside a throwaway
container/VM (see below). This is a root-privileged component: keep it small and boring.

## Deliver
1. `planet_express/missions/launcher/` : `pe_launch.py` (the single-threaded exec helper, stdlib only, no
   imports of planet_express internals so it can be audited alone), `spec.py` (strict JSON spec, bounds),
   `config.py` (loads the root-owned `launch.conf` from a fixed path), and `strategies.py` with
   `SudoLauncher` and `DirectLauncher` (both spawn the SAME helper as a plain subprocess; no `preexec_fn`).
2. Change the containment protocol per §22.3: one `launch(session_id, spec) -> ContainedSession` replaces
   `create()+spawn()`; the launcher allocates the unique uid/gid; `ContainedSession` carries cgroup_path and
   session_uid; `output_fd` returned by the launcher. Update `Supervisor`, `FakeContainment`, `CommandRuntime`,
   `worker_rpc` (cgroup AND uid AND token) and `issue_session` accordingly, and the uid allocation in
   `start_session` (transactional, unique `session_uid`, range from config). Keep all tests green or update
   them deliberately.
3. Preflight (`planet_express/missions/launcher/preflight.py`): real create/move/freeze(+confirm frozen 1)/kill/
   populated 0/rmdir on a throwaway cgroup, `cgroup.kill` exists, helper+config ancestors root-owned and
   non-writable and non-symlink, sudoers probe (`sudo -n pe-launch` ok, `pe-launch --x` refused), uid range
   present, socket directory traversable by session uids. Output a structured report; any failure => the host is
   proposal-only (supervisor refuses mutating tiers AND refuses to start sessions that need containment).
4. Setup steps per §22.5: `session-uid-range`, `launcher-helper`, `launcher-sudoers` (Ubuntu only), `cgroup-tree`
   (systemd unit settings; MOS boot script on the data pool), `workspace-root`, plus the worker socket directory
   (Codex P1 from 3c: `/run/planetexpress` is 0750 PE-group; give sessions access via a dedicated socket dir).
   Each with probe, repair, undo and journal entries following `planet_express/setup/` patterns.
5. `scripts/mission_containment_rehearsal.py` updated to drive the real launcher and print the preflight report.

## Acceptance tests (first)
All of §22.3 step 5 and §22.2: spec fields cannot select another gid, root, Core uid, out-of-range uid, path outside
roots, symlinked cwd, relative argv[0], oversize spec; `sudo -n pe-launch --x` refused; token absent from
cmdline/environ; no stray fds; Core can freeze/kill, session uid gets EACCES on cgroup files; double-fork escapers
stay inside; cross-session ptrace and /proc/<pid>/fd denied (two real uids); uid reuse blocked until empty;
installation tests for writable and symlinked ancestors; preflight failure => proposal-only. Tests that need root
are marked and skipped otherwise; provide `scripts/run_privileged_tests.sh` that runs them inside a disposable
container or on the MOS VM (the orchestrator will run it; do NOT run privileged tests on the developer host).
Full suite green.

## Carried in from the Codex G3 on merged slice 3 (2026-10-13), must be fixed in this slice
1. [P1] `CgroupContainment.create()` gives every session the shared worker uid but `mission_sessions.session_uid`
   is UNIQUE: the second real session fails. Fixed by the unique-uid-per-session allocation in this slice
   (test: two sequential and two concurrent real-shaped sessions; a revoked session's uid is not reused until empty).
2. [P1] Remove `Popen(preexec_fn)` from the threaded launch path (§22.7, single exec helper).
3. [P1] `MissionBudgetsConfig` is parsed but never applied: `MissionService._create_queued()` builds grants with
   model defaults, so `missions.budgets.*` has no effect. Apply the configured budgets (clamped to the grant model
   maxima) when grants are created, with tests.
4. [P2] `tests/test_supervisor_integration.py::test_a_failed_relaunch_never_falls_through...` is flaky (fails ~50%
   in the full suite): `session_ids()` sorts by `issued_at, id` and both sessions share a fake-clock `issued_at`, so
   `[-1]` picks the cleaned first session. Select the active run/session id instead of lexical order.
