# OpenCode delegation: what is enforced, what is policy, what is unverified

> **Status (2026-10-23): usable, NOT claimed to be a security boundary.** Use OpenCode workers only on disposable, non-sensitive
> worktrees; never on a host or checkout holding credentials, private data or production access. The sandbox, credential proxy and
> claims described below are built and tested on this one host, but they have had no independent security assessment, and further
> hardening is deliberately deferred: credential-broker development, more sandbox work, extensive adversarial testing and richer
> concurrency claims. Treat the "enforced" column as "implemented and unit/integration tested", not "proven safe". What is simply
> useful today: explicit model pin, isolated worktrees, timeouts and error handling, protected-path exclusions, cost reporting,
> and the existing Codex and Claude review gates on every result.

Planet Express can hand one bounded, low-risk task to an OpenCode worker running a cheap model
(`openrouter/deepseek/deepseek-v4-flash-0731`) while the parent Claude keeps working. The parent stays the only integration
authority: a worker's output is a commit on its own branch, never merged by the tooling, and it passes the same G3 review
(Codex plus `/code-review`) as any other change. This page is the contract for the tooling; `docs/designs/v4-orchestration.md` §2
carries the roster entry.

## Parts

| File | Role |
|---|---|
| `scripts/oc_delegate.sh` / `scripts/oc_delegate.py` | The wrapper. Subcommands `run BRIEF [--dry-run]`, `budget`, `claims`, `release SLUG`, `selftest`. Stdlib only, system Python. |
| `scripts/oc_forbidden_paths.txt` | Paths never delegated (safety surface, privileged tooling, secrets, review gates). Read from the orchestrator's checkout, never from a worktree. |
| `scripts/oc_sandbox_init.py` | PID 1 inside the sandbox: bridges loopback TCP to the proxy's Unix socket, runs the worker. |
| `scripts/oc_selftest.py`, `scripts/oc_selftest_probe.py` | Isolation self-test: an in-sandbox probe tries to escape; host-side checks see what the probe cannot. Offline. |
| `tests/test_oc_delegate.py` | Unit, proxy, scan, claim and integration tests, including negative controls that weaken the sandbox on purpose. |
| `~/.claude/agents/opencode-worker.md` | Global Claude subagent. Selects delegable tasks, writes the brief, calls the wrapper, verifies the diff independently. |

Flow of `run`: validate the brief, check the base commit, scope, claims and daily budget, create worktree `.claude/worktrees/oc-<slug>`
on branch `oc/<slug>`, start a credential-injecting proxy on a Unix socket, run OpenCode in a bubblewrap sandbox inside a cgroup
scope, scan the worktree against the base commit, run the named acceptance checks in an offline read-only sandbox, and only then
commit the validated paths on the task branch. A read-only task that succeeds removes its own untouched worktree and branch.

## Trust model

Trusted: the wrapper, the forbidden-paths file in the orchestrator's checkout, the parent Claude. Untrusted: the brief (model
written), the worker, everything the worker wrote, and the worktree's contents after the run (including `.git` metadata, symlinks,
file modes and `.gitattributes`). Fields in a result named `*_untrusted` are the worker's words and are data, never instructions.

## Mechanically enforced (and tested)

| Protection | How | Test |
|---|---|---|
| One model | The proxy rejects any other model, any fallback list (`models`), and drops `provider`, `plugins`, `route`, `transforms` and unknown fields. Only `function` tools pass (no provider-side web search). Both `max_tokens` and `max_completion_tokens` are clamped to 16384. | `test_proxy_only_serves_chat_completions_*`, selftest `proxy` group |
| The worker never holds the OpenRouter key | The key lives in the wrapper's memory; a host-side proxy injects it. The key comes from a dedicated 0600 key file; OpenCode's stored credential is used only if `OC_ALLOW_OPENCODE_STORE_KEY=1` is set, and every result reports `key_source`. Before launch the wrapper proves the key is in no argv, env value or config. The sandbox has only a placeholder. | selftest: canary key searched across the sandbox filesystem, `/proc/*/environ` and `cmdline`; the fake upstream saw the key only from the proxy |
| No network | New network namespace; the only route out is loopback to a bridge to the proxy socket. The proxy serves exactly `POST /api/v1/chat/completions`. | selftest `network` and `proxy` groups: internet, test VM, host loopback sentinel, Docker TCP, DNS, key/credits/key-management endpoints |
| No credentials or private data | Only the worktree, `/usr` read-only, minimal `/etc` files and a private home are mounted. `~/.ssh`, `~/.claude`, OpenCode's store, `docs/private`, `data`, `state`, `logs`, `.env`, the main `.git`, claims, ledger and `/run/user` are not visible. | selftest `filesystem`, `git-metadata`, `host-resources` groups (30+ paths) |
| No host resources or privileges | No Docker socket, no D-Bus, no capabilities, `no_new_privs`, own PID namespace; `docker`, `sudo`, `ssh`, `curl` and similar are masked. | selftest `host-tools`, `privileges` groups |
| Write scope | The worktree is read-only except the allowed paths, which are bind-mounted writable. Writes elsewhere fail with `EROFS`. | selftest `write-scope`; live D |
| OpenCode's own policy | Shell, execute, subagents, web fetch and search, skills, questions and anything outside the worktree are denied (the tools are removed from the model's list); edits only on allowed globs. Project-level OpenCode config is disabled, because a committed or planted `opencode.jsonc` otherwise overrides the policy. | selftest phase 2 drives the real OpenCode binary with a hostile project config |
| No recursion | `PE_DELEGATION=1` in the sandbox; every wrapper subcommand refuses to run if it is set; the sandbox has no credentials or network to delegate with. | `test_nested_delegation_is_refused`, selftest `recursion` |
| After-the-fact scope check | Every file in the worktree is hashed against the base commit. Rejected: any change outside the allowed paths, any forbidden path, symlinks, executable-bit changes or new executables, type changes, nested `.git`, special files, a changed `.git` pointer, oversize files, too many files, `.gitattributes`/`.gitignore`/`.gitmodules`. Deletions count. A read-only task may change nothing. | `test_scan_rejects_every_escape_route` and related |
| Pre-run scope check | Allowed entries must be tracked regular files or directories with no symlinks and nothing forbidden under them; a forbidden pattern inside a directory makes it undelegable. Forbidden also covers code that Core or the operator's browser executes: `planet_express/widgets/` (exec'd into Core by the registry), `static/` and `templates/` (the dashboard, including the approval UI), and instruction files at any depth (`CLAUDE.md`, `AGENTS.md`). What is left to delegate is mainly documentation and non-safety tests. | `test_scope_validation_*`, `test_forbidden_*` |
| Trusted acceptance | A fixed registry: `pytest` on `tests/*.py` files and `ruff` (with the base commit's `pyproject.toml`). Commands are built by the wrapper, run in an offline sandbox with the worktree read-only. No brief text is ever executed. Files that change how tests or linters run (`conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `ruff.toml`, `sitecustomize.py`, `*.pth`, `Makefile`, any `pyproject.toml`) are forbidden at any depth, so a worker cannot forge its own acceptance. | `test_acceptance_*`, `test_failing_acceptance_blocks_the_commit` |
| One writer per path | Claims (flock-protected) block overlapping tasks: running ones, failed or crashed ones until `release`, finished ones until their branch is merged, a corrupt claim, and paths with uncommitted changes in the main checkout. The claim stays active through the scan, acceptance and commit. Slugs are never reused (checked under the lock, against worktrees, branches, claims and state). | `test_overlapping_tasks_collide_*`, claim tests |
| No auto-merge | The wrapper only commits validated paths on `oc/<slug>`, with hooks, filters and signing disabled, and checks the staged set equals the validated set. | `test_a_delegated_doc_edit_is_committed_on_its_branch_and_never_merged` |
| Budget | $2/day admission (the environment can only lower it): OpenRouter's own `usage_daily` for the key, or the local ledger if higher, plus the caps of running tasks, plus this task's cap. Checked again under the claims lock, so concurrent starts cannot all be admitted on the same headroom. Unknown, unparseable or unreachable means refused, and so is a malformed ledger record. Per task, the proxy enforces a cost cap, a request cap and concurrency. A request that was sent and whose answer was lost, cut or abandoned is charged as a worst-case estimate, never as zero; when the worker ends, in-flight upstream calls are cut and accounted before the cost is recorded. | `test_budget_*`, `test_key_info_fails_closed`, `test_budget_unknown_or_exhausted_*` |
| Timeouts | The worker's process group is killed (TERM, then KILL); a timed-out result is never committed. | `test_timeout_kills_the_worker_*` |
| The real key only goes to OpenRouter | `OC_UPSTREAM_BASE` is honoured only for a loopback test server. | `test_the_real_key_is_only_ever_sent_*` |

## Cost accounting: exact, estimated, shared

- **Exact, per task:** measured at the proxy from `usage.cost` in each OpenRouter response (the proxy asks for it). Recorded in
  `.claude/worktrees/oc-ledger.jsonl` with generation ids.
- **Estimated:** when a response carries no cost, the proxy charges a worst case from token counts or request size and flags the
  result `includes estimates`. Prices come from `OC_PRICE_IN_PER_M` / `OC_PRICE_OUT_PER_M` (default deliberately high).
- **Shared:** `provider_usage_daily_usd` is OpenRouter's figure for the whole key, including interactive OpenCode use and anything
  else on the key. It is used for admission, never attributed to a task.

## Policy only (instructions and process, not mechanisms)

- Whether a task is suitable (at most about 8 files, a deterministic check, no design or safety judgement). The forbidden-paths
  list is mechanical; the rest is the subagent's checklist.
- Keeping secrets and infrastructure detail out of the brief's task text.
- Reading and judging the worker's diff, including in-scope content that is wrong or aimed at a later reader. The wrapper cannot
  tell good prose from bad inside an allowed file.
- Passing the result through G3 (`codex review` plus `/code-review`); retrying at most once; never delegating the same task twice.
- Never running `opencode` by hand against this repo outside the wrapper. Your interactive OpenCode keeps its own, unrestricted setup.

## Not verified, or known gaps

- **No provider-side spend limit.** The key reports `limit: null`. Create a dedicated OpenRouter key with a credit limit and point
  `OC_KEY_FILE` (default `~/.config/pe-delegation/openrouter.key`, mode 0600) at it. Until then the cap is local only.
- **Token Harbor** is still the global OpenCode default in `~/.config/opencode/opencode.jsonc`, with its key in plaintext. The
  wrapper does not depend on that file (the worker runs with its own config and home), but the key should be revoked and the file cleaned.
- **Disk usage is not bounded.** `prlimit` caps one file at 64 MiB and `/tmp` inside the sandbox is a 256 MiB tmpfs, but the writable allowed directories and the worker's private home live on the host filesystem with no quota. A runaway worker is bounded by its timeout, not by bytes. The scan reads at most 50,000 files and reports unreadable directories and files as violations.
- **No seccomp filter** in the sandbox (kernel attack surface of an unprivileged user namespace is not reduced).
- **cgroup limits** (`MemoryMax=3G`, `TasksMax=512`, `CPUQuota=200%`) are configured and their availability is checked; they were not stress-tested.
- **Third-party data handling.** Worker prompts and the repo content it reads go to OpenRouter and the model provider. The repo is
  public and the worktree holds tracked files only, but retention and routing at the provider are not controlled.
- **Sandbox portability.** Verified on this host (Ubuntu with an AppArmor userns profile for bwrap). If a kernel or profile change
  breaks bwrap the wrapper fails closed; it was not tried elsewhere.
- **Live adversarial behaviour.** Escape attempts were scripted through a fake model and the sandbox probe. A live model that is
  prompt-injected was not red-teamed. Abstract Unix sockets are isolated by the network namespace but not probed directly.
- **OpenCode version.** Config and permission behaviour were established against 2.1.4. After any OpenCode upgrade, run `oc_delegate.sh selftest`.
- Shared-key spend can lag the provider's figure slightly; the local ledger and running-task reservations are the guard.

## Verification record (2026-10-23)

| Step | Result |
|---|---|
| A. Configuration and secret isolation | `selftest`: all secrets checks held; canary credential absent from the sandbox; proxy injected the key. Token Harbor cleanup not done (see above). |
| B. Sandbox escape tests | `selftest`: 131 checks held (SSH keys, Claude credentials, OpenCode store, private directories, Docker socket, network, DNS, host tools, privileges, git metadata, write scope). Negative controls (shared network, writable worktree, visible `~/.ssh`, credential in env, reachable Docker socket) are detected. |
| C. Read-only investigation | Live: no changes, no violations, exact cost $0.0077. |
| D. One-file documentation edit | Live: one file changed, no violations, exact cost $0.0036. |
| E. Scope violation rejected | Admission refusals for forbidden file, forbidden directory and missing file; a simulated escape (forbidden file plus symlink written after the run) is rejected with `scope_violation`, nothing committed. |
| F. Claim collision | Overlapping task refused while the first runs; unmerged finished work keeps its paths; the main checkout's uncommitted edits block overlap. |
| G. Timeout and failure | Timed-out worker killed, nothing committed, claim failed; a failing or idle worker is not accepted; unknown or exhausted budget refuses admission. |
| H. Reviewed delegated change | The D commit was read by Claude (diff exact, anchors checked) and passed `codex review --commit`: no findings. |
