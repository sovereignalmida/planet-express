# S3c — Integrate supervisor, containment, worker RPC, adapter; wire into the daemon

Context: S3a (runtime/supervisor/sessions/brief) and S3b (cgroup containment, worker RPC, command
adapter) were built in parallel against `S3-common.md` and are merged but do NOT fit together
(`tests/test_missions_command_adapter.py` fails: `RunHandle(mission_id=...)` vs S3a's `RunHandle`).
Run `git merge --ff-only v4-next` first. Read `S3-common.md`, `S3a-brief.md`, `S3b-brief.md`, then the
merged code. Test first, `codex review --uncommitted` before final commit (if Codex reports a usage
limit, say so in your report and continue), no push, Co-Authored-By trailer.

## Deliver
1. Reconcile interfaces, S3a's definitions win unless impossible: `CommandRuntime` uses S3a's
   `AgentRuntime`/`RunHandle`/`RunContext` (`context.spawn(argv, env, cwd, extra_fds)`, `context.token_fd`,
   `context.brief`), not its own spawn; `worker_rpc` calls `resolve_session(store, token_hash,
   peer_cgroup=, peer_uid=)`; delete `tests/s3b_stubs.py` and make S3b's tests use the real modules;
   `FakeContainment` gains `output_fd`/`release_output` (or the adapter degrades to no streaming and that
   is tested). Keep every existing S3a/S3b test passing or consciously updated.
2. `Supervisor` implements S3b's `WorkerBackend` (get_context, post_event, heartbeat, submit_result,
   memory_search) so worker calls land on the right mission/session. `memory.search` returns CONFIRMED
   cards only (reuse `memory.retrieval`).
3. `AgentResult.memory_candidates` are persisted as `candidate` cards with actor `runtime:<mission_id>`
   (source `inferred`, never `operator-stated`) via `memory.store.upsert_candidate`; a bad/secret-shaped
   candidate is dropped and recorded in an event, never failing the mission. They stay quarantined (the
   store already refuses to retrieve candidates).
4. Daemon wiring (minimal edits to `casa_farnsworth.py`; list them exactly): when `config.MISSIONS_ENABLED`,
   build `CgroupContainment` (from `missions.cgroup_root`/`worker_user`), the `CommandRuntime`
   (`missions.runtime.argv`), the `Supervisor`, a worker `RpcServer` on `missions.worker_socket`, a poll
   thread that calls `supervisor.poll_all()` and starts queued Observe/Propose missions, and `recover()`
   at startup; clean shutdown stops all of it and kills sessions. Nothing changes when disabled.
   Add `missions.enabled` to the supervisor from config (S3a used a constructor flag).
5. A fake-runtime end-to-end test: create a mission through the S4 service, let the poll thread run it with
   `FakeContainment` and an echo runtime, see it reach `awaiting_decision` with an artifact, and see a
   killed/budget-breached one end in `needs_operator` with the session provably empty.

## Known issue to document, not solve here
Core runs as an unprivileged user; spawning as the worker uid and joining a cgroup needs privilege (a
non-root writer cannot move another uid's process into a cgroup). The real answer is a root-owned
launcher invoked through an exact sudoers rule (slice 3d, spec first). Make `CgroupContainment` take a
`launcher` strategy so 3d can plug in; the default strategy may require Core to have the capabilities and
must fail with a clear message otherwise.

## Acceptance
Full suite green; no behaviour change with missions disabled; the e2e test above; Codex findings fixed.
