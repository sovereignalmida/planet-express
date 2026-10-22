# v4 release proof map (design 11.3, items 1-10)

Every item below names its test or its rehearsal record. `tests/release_proof/test_proof_map.py` reads this file and
fails if an item is missing, a status is not one of the listed words, or a referenced test file or function does not
exist. Run the automated half with `pytest tests/release_proof -q`.

Statuses: **AUTOMATED** (a test in the normal suite), **AUTOMATED + HOST** (automated, plus a privileged or real-host run
that is recorded), **SCRIPT** (a rehearsal script exists and is unit-tested for arguments and refusals; a human must run it
on a disposable host), **PENDING** (needs a human or a host run that has not happened), **DONE** (documentation).

| Item | Status | Evidence |
| --- | --- | --- |
| 1 | AUTOMATED | `tests/release_proof/test_item01_defaults.py::test_the_schema_default_and_the_shipped_example_config_have_missions_off`, `tests/release_proof/test_item01_defaults.py::test_no_v3_test_file_has_been_deleted_or_renamed_since_v3_next`; the v3 suites run in the full suite |
| 2 | AUTOMATED + HOST | `tests/release_proof/test_item02_checkpoint_and_side_doors.py`; host: `tests/test_checkpoint_privileged.py`, `tests/test_launcher_privileged.py` (2026-10-20, see below) |
| 3 | AUTOMATED + HOST | `tests/release_proof/test_item03_tripwire.py::test_a_planted_change_pauses_within_the_published_bound_and_offers_rollback`; host: `tests/test_tripwire_privileged.py` |
| 4 | AUTOMATED | `tests/release_proof/test_item04_no_protected_credentials.py` |
| 5 | AUTOMATED | `tests/release_proof/test_item05_secrets_and_hostile_text.py` |
| 6 | AUTOMATED | `tests/release_proof/test_item06_crash_classification.py` |
| 7 | SCRIPT | `scripts/rehearse_repair.py` (run via `scripts/run_rehearsal.sh --vm HOST`), unit tests `tests/test_rehearse_repair.py`; run record PENDING |
| 8 | AUTOMATED | `tests/release_proof/test_item08_incident_shapes.py::test_an_incident_shape_is_evaluated_end_to_end`, fixtures `tests/fixtures/v4/incidents/` |
| 9 | PENDING | `docs/handoff/v4/rehearsals.md` (commands for Ubuntu/systemd and MOS), `scripts/run_rehearsal.sh`, `scripts/run_privileged_tests.sh`; the 2026-10-20 launcher/containment runs are recorded, the repair rehearsal and the refusal case are not |
| 10 | DONE | `tests/release_proof/test_item10_docs.py`, `INSTALL.md`, `CHANGELOG.md`, `docs/handoff/v4/rehearsals.md` |

## Notes per item

1. v3 regression suites are the existing tests; this slice changes none of them. Missions are off in the schema default and
   in `config.example.yaml`; every mission and memory RPC answers `disabled`; a disabled supervisor, checkpoint service and
   tripwire monitor start nothing. A test also fails if a v3 test file is deleted or renamed since `v3-next` (skipped
   where git or the ref is absent).
2. Checkpoint: byte, mode and container-fact restore; refusal when the host moved on after the session or the end state was
   not captured; backup floor refuses unverifiable, absent and stale. Side doors (docker group, session range containing
   Core, Docker TCP in config and as a live listener, unmanaged or hand-edited sudoers, unreadable inputs, a crashing step)
   each fail the gate. The privileged runs (real uids, cgroups, ACLs, sockets) were recorded on the MOS and Ubuntu test VMs
   on 2026-10-20 (`docs/handoff/v4/STATE.md`).
3. All five named domains (ingress, DNS file, credential file, firewall, data-volume shrink) are planted under a fake
   clock whose scan consumes its whole 10 s deadline; the measured latency equals interval plus deadline and must not
   exceed the 30 s bound; the pause event carries an available rollback only after a durable post-capture, and a rollback
   restores the checkpointed files. A real-time smoke test lives in `tests/test_tripwire_monitor.py`.
4. The operator's environment is full of DNS, ingress, router and backup secrets; none reaches an ungranted session's
   environment, readable paths, launch spec or persisted state; the DNS write tool is refused without the credential and
   even with it (write tools stay gated); a grant cannot name an unregistered credential.
5. A planted secret in four forms (key=value, header, bearer, URL userinfo) is planted in the evidence, the agent's events,
   its final result and its memory candidates; afterwards the brief and every persisted byte (all tables, artifacts) are
   searched. A hostile log line stays quoted data and the grant row is byte-identical before and after.
6. Crash at investigation (retried only inside the read-only budget), execution (needs_operator or recovery with rollback
   offered, never replayed, the unknown intent named) and verification (Core verifies again; completion only through Core).
7. `scripts/rehearse_repair.py` builds the daemon's own controls (`assembly.build_repair_controls`), a real supervisor and approval
   flow, and drives a synthetic compose project in real Docker: (a) a gateway recreate orphans two containers in its network
   namespace; the scripted agent rebinds them in dependency order; Core checks an in-namespace probe on both and project
   completeness; (b) a failed canary is rolled back by the operator through the checkpoint, then a bounded correction
   recreates only `web`. It refuses unless `PE_REHEARSAL_OK=1`, `--i-know-this-is-disposable`, root, a working docker and
   no foreign containers. The scripted agent reaches Docker only through a real per-mission Docker proxy (`DockerProxyService`: real
   upstream, grant-derived policy, recorded creates), and every call the proxy refuses is listed in the record. It does not exercise
   the cgroup launcher or session uids (see `docs/handoff/v4/rehearsals.md`).
8. Three synthetic incident shapes (VPN-gateway namespace, stale NFS handle, disk pressure after a canary) run end to end
   under Observe and Propose with the real memory retrieval: the expected cards are in the brief, the stored result is a
   valid `AgentResult` with evidence and unknowns in separate disjoint lists, no `executing` state, no checkpoint, only the
   agent was launched. Output that breaks the contract (an extra key, an unsupported confidence) is refused, not stored.
9. See `docs/handoff/v4/rehearsals.md`. Neither platform degrades to an unmanaged session: a failing side door or preflight keeps the host
   proposal-only (tests in item 2). PENDING until the repair rehearsal and the open-side-door refusal are run on the VMs;
   MOS repair rehearsal is not claimed (no compose plugin; see `docs/handoff/v4/rehearsals.md`).
10. INSTALL.md covers enabling, accounts, sockets, secrets, grants, boot components, upgrade, downgrade and uninstall;
    `tests/release_proof/test_item10_docs.py` checks it names every launcher step and the example config block validates.

## Open items at the time of writing

- Item 7 and the repair half of item 9 need a human to run `scripts/run_rehearsal.sh --vm HOST` on a disposable VM and paste
  the JSON record into `docs/handoff/v4/rehearsals.md`.
- Item 2's host half and item 3's host half are the 2026-10-20 runs; repeat them on the release candidate.
