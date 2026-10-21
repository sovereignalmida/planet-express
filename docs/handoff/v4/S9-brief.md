# S9 — Release proof, rehearsal harness, docs (v4 slice 9)

Read first: `docs/designs/v4-mission-control.md` §11.3 (release proof items 1-10), `docs/designs/v4-plan.md` slice 9,
`docs/handoff/v4/STATE.md`, the merged `planet_express/missions/` (assembly.py, supervisor.py, approval_flow.py,
verify/, checkpoint/, tripwire/, dockerproxy/), `tests/repair_fakes.py` and `tests/test_mission_repair_flow.py` (existing
end-to-end fakes), `scripts/mission_containment_rehearsal.py`, INSTALL.md, CHANGELOG.md.
`git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer. Never run privileged tests on this machine.

## Goal
Every release-proof item has a NAMED test or a rehearsal script with a recorded result, and the docs cover everything v4
adds. You do not change safety behaviour; if a proof item exposes a defect, write the failing test, mark it xfail with the
reason, and report it (do not fix safety code in this slice unless trivial and covered).

## Deliver
1. `tests/release_proof/` package: one module per item with the item number in the file name and a docstring quoting
   the item. Reuse the existing fakes; use fixtures under `tests/fixtures/v4/` (synthetic, anonymised; never real
   hostnames/IPs/secrets). Items 1-6 and 8 as automated tests (item 1: missions off by default, v3 suite untouched;
   2: checkpoint exact restore, stale-backup refusal, each side door fails closed; 3: planted ingress/DNS/credential/
   firewall/data-shrink change pauses within 30 s fake-clock bound and offers rollback; 4: no protected credentials in
   env/files/paths for an ungranted session; 5: planted secret absent from context/transcript/artifacts/events/memory,
   hostile log line leaves the grant hash unchanged; 6: crash at investigation, execution, verification classified,
   no replay; 8: three historical incident shapes as fixtures -> memory retrieved, fact vs unknown separated, valid
   structured output, no mutation under Observe/Propose).
2. Item 7 rehearsal: `scripts/rehearse_repair.py` driving the REAL stack (checkpoint, proxy, tripwire, verify) against
   real Docker on a disposable host with a synthetic compose project: (a) a VPN-gateway-style recreate that orphans
   containers joining its network namespace; the mission recreates dependents in dependency order, passes an
   in-namespace check and a compose-project completeness check; (b) a failed canary rolled back, then an approved
   bounded config correction recreating only that service. The agent is a deterministic scripted command (no LLM).
   It prints a JSON record (steps, timings, check results) and exits non-zero on any failure. It refuses to run
   unless `PE_REHEARSAL_OK=1` and the host is a disposable one (same discipline as run_privileged_tests.sh).
3. Item 9: a section in `docs/handoff/v4/rehearsals.md` with the exact commands for Ubuntu/systemd and MOS, and a
   `--vm HOST` mode in `scripts/run_privileged_tests.sh` style for the rehearsal (rsync, run as root over ssh).
4. Item 10 docs: INSTALL.md sections for enabling missions (config keys, setup answers, launcher steps, MOS chown
   requirement, boot hook), accounts/sockets/secrets/grants/boot components v4 adds, upgrade, v4->v3 downgrade (snapshot
   restore), uninstall; CHANGELOG entry; `docs/handoff/v4/release-proof.md` mapping each item 1-10 to its test or
   rehearsal record (with PENDING for those needing a human/host run).
5. Do not edit `docs/private/`.

## Acceptance
- All automated release-proof tests pass or are xfail with a written reason; full suite green; missions off => no change.
- The map in release-proof.md is complete and every referenced test exists (a test checks that).

## Report
Files changed, test counts, xfails with reasons, defects found, items left PENDING.
