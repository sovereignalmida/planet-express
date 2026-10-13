# S3b — Containment, worker RPC, command adapter (v4 slice 3, part b)

Read first: `S3-common.md`; `docs/designs/v4-slice-0-contracts.md` §1, §2.1, §6, §6.1 (intro only), §11, §15,
§20 (MOS cgroup facts); `planet_express/integrations/rpc.py` (RpcServer, SO_PEERCRED, framing),
`planet_express/setup/` (how accounts/sockets are created and journaled). Depends on S3a's types in
`containment.py`/`runtime.py`/`sessions.py`; until S3a merges, code against the interface in S3-common and
stub what you need locally (do not edit S3a's files).

## Deliver
1. `planet_express/missions/containment_cgroup.py`: `CgroupContainment` implementing `SessionContainment`
   on cgroup v2 (`mkdir`, `cgroup.procs`, `cgroup.freeze`, `cgroup.kill`, `cgroup.events populated`). The
   cgroup root is configurable (a delegated subtree). Spawn drops to the worker uid/gid via
   `os.setgroups/setgid/setuid` in a pre-exec step that joins the cgroup BEFORE exec, closes inherited fds
   except `pass_fds`, sets `no_new_privs`, resets env to exactly the given dict, new session. `kill()` =
   freeze → `cgroup.kill` → poll `populated 0`. `is_empty()` reads `cgroup.events`.
2. `planet_express/missions/worker_rpc.py`: a second `RpcServer` (reuse the class; extend it minimally and
   backwards-compatibly so handlers can receive `(params, session)` and so a per-connection authenticator
   hook exists) on `worker.sock` with ONLY the five worker methods in S3-common. Authentication per call:
   SO_PEERCRED pid → read `/proc/<pid>/cgroup` → must equal the session's `cgroup_path` (or uid equals
   `session_uid` in the uid kind), AND the session token (sha256 matches). The token arrives on an inherited
   fd (pass_fds) read by the child, then presented in the request. Refuse otherwise, uniformly.
3. `planet_express/missions/adapters/command.py`: `CommandRuntime(AgentRuntime)` runs a configured argv
   (e.g. an agent CLI) inside the containment with env = minimal + `PE_WORKER_SOCKET` + `PE_SESSION_TOKEN_FD`,
   cwd = a per-mission workspace owned by the worker, feeds the brief (file in workspace), parses the
   child's final JSON into `AgentResult`; stdout/stderr streamed as bounded, redacted `AgentUpdate`s. A
   `StubRuntime`-compatible echo adapter for tests. No vendor-specific code.
4. Config (`config_schema.py` MissionsConfig): `worker_user`, `cgroup_root`, `worker_socket`, `runtime.argv`,
   `runtime.env_allow`, budgets defaults.
5. `scripts/mission_containment_rehearsal.py`: a self-contained script (needs root) that creates a
   cgroup, spawns a process tree as a throwaway uid, freezes, kills with a double-fork/setsid escaper,
   proves `is_empty()`, and cleans up; prints JSON. Used by the orchestrator on Ubuntu and the MOS VM.

## Acceptance tests (first)
- Unit tests with a fake cgroup tree on tmpfs: cgroup file protocol correct (order: freeze→kill→poll).
- RPC: wrong uid, wrong cgroup, right cgroup + wrong token, revoked session, a method outside the allowlist
  (`approval.decide`, `execution.abort`, `config.get`) all refused; right everything accepted; the worker
  socket server cannot be pointed at the operator handler table (assert handler dict keys exactly).
- Token never in argv/env (only an fd number); fd closed in the parent after spawn.
- Command adapter with an echo child: result parsed and validated; malformed/oversize result rejected;
  output bounded and redacted (planted secret absent).
- Root-only integration tests are marked and skipped when not root; the rehearsal script is documented.
- Full suite green.
