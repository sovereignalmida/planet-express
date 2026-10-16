# S8 — Verification and recovery (v4 slice 8) — SAFETY SURFACE (G1 + G3, one reviewer not Claude)

Read first: `docs/designs/v4-slice-0-contracts.md` §3 (state machine), §10 (catalogue), §11 (lifecycle and crash
classification), §2 (mission schema: `verification_plan_json`, outcome fields); `docs/designs/v4-mission-control.md`
§6.5; `docs/designs/v4-plan.md` slice 8; merged `planet_express/missions/` (`state.py`, `supervisor.py`
`_settle`/crash paths, `models.Grant`/`canonical.py`, `checkpoint/` for `manifest()`, `tools.py` for brokered
`query`), `casa_leela.py` (the scan: find how to run a fresh scan and read findings with resource ids) and
`planet_express/execution/dependency_dryrun.py` (existing probe patterns to reuse, not duplicate).
`git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer; missions stay OFF by default.

## Goal
A mission completes ONLY when Core's own pre-agreed checks plus a fresh scan pass. After any crash Core reads host
state, classifies the mission, and never replays an unknown mutation. This slice builds the verifier and the
classifier; slice 7 will call them from the approval flow; slice 9 rehearses end to end.

## Deliver
1. `planet_express/missions/verify/`:
   - `plan.py`: `VerificationPlan` strict model `{catalogue_v, checks:[{id, check, params}]}`, canonical (RFC 8785)
     hash; `grant.verification.plan_sha256` binds it (extend `Grant` validation: plan must exist, hash must match,
     for any tier above Propose; a plan cannot be changed after approval: a changed plan changes the hash and so the
     grant hash). Unknown check kinds or extra params REFUSE (no silent drop).
   - `checks.py`: the eight checks from §10 (`http`, `exec_probe`, `container_state`, `mount_readable`,
     `project_complete`, `log_absent`, `file_state`, `query`), each a strict params model, timeout <= 60 s, bounded
     retries and output, argv only, no shell. `http`: route probe with a resolve override (connect to a given
     address while sending the real Host/SNI), N consecutive passes. `exec_probe`: binaries only from the operator
     allowlist in Core config (`missions.verify.exec_allowlist`, default empty = check unavailable, fail closed).
     `container_state`: running AND (health, if defined) AND not restarting; a Docker `healthy` string alone never
     satisfies any check. `project_complete`: every service the compose project defines exists and runs (reuse the
     S5 provider's compose-config reading; unknown/unreadable project = FAIL, not pass). `log_absent`: bounded window,
     bounded bytes, pattern is a literal or a safe-bounded regex (reject catastrophic patterns by length and a
     timeout). `query`: only through the brokered tool registry, read-only tools.
   - `runner.py`: `Verifier.run(mission) -> VerificationResult` runs every check, records each result as a mission
     event (redacted, bounded), takes a FRESH scan and compares with the baseline captured at checkpoint: "no new
     finding in scope" = no finding absent from the baseline whose resource id is contained in the grant scope
     (§2.3 containment, `missions/resources.py`). Result is `passed` only if every check passes AND the scan is
     clean; anything else (check error, timeout, scan failure, missing plan) is `failed`, never `passed`.
   - Results are stored on the mission (existing verification result field) and bound to the grant hash and
     checkpoint id.
2. `planet_express/missions/recovery.py`: `classify(mission, host_state, checkpoint, events) -> Classification` per
   §11: state == checkpoint -> `needs_operator` offering resume (automatic restart only for Observe/Propose);
   state matches the end expectation -> `verifying`; otherwise `recovery_required` -> `needs_operator` or
   `rollback_pending`. Host state comes from Core (S5 provider facts + tripwire collectors), NEVER from the runtime
   or the agent's own events. Intent is journaled before a session starts and at each phase change; recovery reads
   the journal and refuses to replay any intent whose effect is unknown (it becomes an operator decision).
3. Supervisor integration points (small, behind interfaces; slice 7 completes the wiring): the supervisor calls
   `Verifier.run` when a session ends (state `verifying`), and the transition to `completed` is allowed ONLY by a
   `passed` result for the current grant hash (enforce in `state.py`/store, not just in the caller: a direct store
   call to complete without a passed result must fail). `failed`, `rolled_back` and `needs_operator` outcomes are
   produced from real failure cases and carry the result summary.
4. Config: `missions.verify.*` (exec allowlist, per-check timeout cap 60 s, scan timeout, retry caps) in
   `config_schema.py`, validated, defaults safe.

## Acceptance tests (write first)
- Each check: pass, fail, timeout, malformed params, and a "green-but-broken" case (container `healthy` but the
  in-container probe fails; route up but wrong body; project missing a service; mount present but unreadable by the
  consumer) that MUST fail.
- Verification result `passed` requires all checks and a clean fresh scan: tests with one failing check, a scan
  failure, a new in-scope finding, a finding outside scope (ignored), and a baseline finding (ignored).
- Completion gate: completing a mission without a passed result, with a result for an older grant hash, or with an
  agent-asserted success fails at the store/state layer. A changed plan changes the grant hash and invalidates approval.
- Crash classification at investigation, execution and verification (the three phases), each with fake host state:
  unchanged-from-checkpoint, matches-expectation, and diverged; no unknown mutation is ever replayed; a hostile
  event stream (agent claims success) cannot influence the classification.
- Resource bounds: oversize output, huge log window, catastrophic regex, slow probe all bounded and fail closed.
- No secrets in events/results; `exec_probe` binary outside the allowlist refused; shell metacharacters inert.
- Missions disabled => nothing runs. Full suite green.

## Report
Files changed, test counts, deviations (list each), open questions.
