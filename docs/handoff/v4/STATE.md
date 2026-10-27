# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | DONE rev 0.4 (final G1 dispositioned); operator signed off 2026-10-24 |
| 1 | Mission store | MERGED; Codex G3 + Claude review fixed |
| 2 | Ship's Memory | MERGED; Codex G3 x2 fixed |
| 3 | Supervisor, adapter, worker | MERGED (3a-3d); Codex ~9 rounds + Claude /code-review fixed. Privileged green docker + MOS + Ubuntu VM. Launcher steps wired into setup (missions answer, default off) |
| 4 | Surfaces | MERGED (rounds 1+2). Codex G3 on round 2 still pending from the 2026-10-13 usage limit; G4 whole-branch review covers it |
| 5 | Checkpoint | MERGED (3 Codex rounds + code-review). MOS parity not claimed until a real-compose conformance run |
| 6 | Tripwires and grants | MERGED (code-review 10 fixed, Codex 2 fixed) |
| 7a | Approval flow, daemon wiring, hooks | MERGED |
| 7b | Docker proxy, sudo generation | MERGED; review rounds + audit fixes (labels, host-root-equivalent mounts); privileged green on docker + both VMs |
| 8 | Verification, recovery | MERGED (Codex 2 rounds + code-review) |
| 9 | Release proof, rehearsal harness, docs | Ubuntu item 7 repair rehearsal passed 2026-10-23; MOS repair parity and open-side-door refusal remain unclaimed |
| 10 | Typed steps from `mission` origin | BUILT 2026-10-23 (`typed_steps.py`, `run_mission_runbook`); Claude `/code-review high` 9 findings fixed; Codex skipped (usage limit). NOT exposed to the worker (needs its own decision + review); see `S10-notes.md` |

## Next action
**Handoff 2026-10-22 (token budget).** All workers finished and merged into `v4-next` (62 commits ahead of origin, NOT pushed). Non-privileged suite 6262 passed. No background agents running.
Merged: S9 (release proof), S9b (rehearsal agent through the real Docker proxy, scenarios end cleanly), G4-F1 (protected-set discovery, fail closed), G4-F2 (8 safety fixes + 13 follow-up findings, Codex clean), G4-F3 (rows 11-21, Codex clean), G4-F4 (`container:<in-scope>` network mode + `GET /version` through the proxy; Codex 2 P1s fixed), plus the grant-budget default fix. G4 record: `G4-review.md` (F2 follow-up dispositions are in the F2 worker reports: all 13 fixed, none declined; F3 and F4 likewise).
**Do next, in order:**
1. Item 7 passed on PE_UBUNTU_TEST on 2026-10-23: namespace repair completed through the proxy (15 writes/3 creates), the failed canary rolled back, the service-scoped correction completed (5 writes/1 create), no proxy refusals, missions released and teardown empty. Record: `/tmp/planet-express-rehearsal-20261023-final.json` on the operator host. The run exposed and fixed Compose inspect-shaped create defaults, checkpoint comparison of generated labels, an in-flight Docker/firewall tripwire race, service-scoped project-network reattachment, and a stale harness baseline. `GET /info` was not needed.
2. Privileged suite re-run 2026-10-23 after the item 7 fixes: 259 passed, 5 skipped; the earlier fd-leak flake in `test_exactly_fds_0_1_2_3...[sudo]` did not recur.
3. Claude `/code-review high` on F3 and F4 ran 2026-10-23 (9 findings, all dispositioned; Codex had already reviewed them). Fixed: scans are serialised (`begin()` on the supervisor thread and the monitor thread shared one Scanner and could report each other as timeouts); `container:<target>` now also requires each network the target is attached to to pass the same scope rule as a direct attach; the start-deferral timer resets on a failed start and when a mission settles; `/version` clamps `Components[].Details.ApiVersion`; the brief's data-handling text has one source; a stale `baseline.prev.json` is cleared when there is nothing to set aside. Left, with reasons: start check on host/`container:` network mode applies only to containers carrying the proxy's CREATED_LABEL, so existing in-scope containers (Plex, gluetun-attached apps) are unaffected (finding was a false positive); `worker:decision` actor is deliberate (a worker's free-text notes must not forge cards) and v4 has no pre-upgrade cards; `_mission_locks` (one Lock per mission) and the 256-entry usage cache re-read from the log on a miss, so neither affects correctness; the launcher preflight on the poll thread is the existing design, re-proved at most every 600 s.
   Item 7 fixes got `/code-review high` on 2026-10-23 (Codex skipped, usage limit). Dispositions: tripwire deferral no longer skips the scan, it excuses only `firewall_changed` while a proxy write is unresolved or just finished, capped at 120 s of continuous cover (deliberate: a container stop takes ~10 s and Docker rewrites its rules throughout, and a 5 s cap failed the rehearsal); `docker.result` now closes on upstream failure (status 0); in-flight query bounded to the event tail; label filter shared (`comparable_labels`) and narrowed to Compose's per-recreate labels, `config-hash` included because it is measured unstable under `network_mode: service:` (rehearsal re-run proves it); snapshot failure is logged; stale/corrupt deferral state tolerated. Left as designed: project-network reattachment (needs the service in scope, the same project label, and prior membership at session start). Rehearsal passed again after the fixes (record `/tmp/planet-express-rehearsal-20261023-review-fixes.json`); privileged suite 259 passed, 5 skipped (the fd-leak flake showed once on [direct], 3 clean reruns).
4. S10 done (see table). **Decided 2026-10-24: `run_mission_runbook` stays Core-only for v4.0; exposing it to the worker via an RPC method (allowlist change, own second review, abuse/fairness design for T24 limits) is the first v4.1 item.** S10's gate got its Codex pass 2026-10-24 (`--commit 7fcd1f1` plus two rounds on the fixes). Fixed: every step of a runbook is charged to `budgets.tool_calls` (was one per runbook); a service-only grant now works (derived stack and compose-file targets need only be unprotected, the compose path must be the stack's exact canonical file; leases are required per mutating step, not per runbook).
5. Final G4 re-run on the finished branch (whole-branch Codex, `/code-review high`, fuller `/cso` with dependency CVE scan), then update `G4-review.md`.
   **Final G4 re-run, in progress 2026-10-23** (Claude `/code-review high` whole branch + `/cso`; Codex skipped, usage limit). `/cso`: nothing at the 8/10 bar in the v4 surface; report at the session scratchpad `cso-2026-10-23.json`. Inherited-from-v3 items: certifi stale CA bundle (bump `certifi>=2026.10.19`, check the live host venv), `deploy.sh:90` installs unhashed `>=` floors, CI actions tag-pinned and no CODEOWNERS. Below the bar: exec/attach/archive on an in-scope container do not apply `protected_mounts` (`policy.py:433`, design call), `_NAME` regexes use `$` with `.match` (`resources.py:18`, `policy.py:67`: use `fullmatch`), inspect redaction misses split Cmd args, PidsLimit/Memory/ShmSize uncapped.
   Code-review: FIXED (f505be5): scheme-less Docker TCP hosts, non-normalised grant paths, compose.dry_run fixed text (note: the broker already blanked non-2xx output, so the stderr claim was partly wrong; pass/fail stays a one-bit oracle). **FIXED 2026-10-23 (Claude `/code-review high` + Codex x4 on the fixes; privileged suite 259 passed, Ubuntu repair rehearsal passed):**
   a. Leases: a stack, a service's stack, and a scoped path or mount also lease the stack directory and every resolved form of it (configured and resolved root, symlinked stack directory, symlink aliases); `Supervisor` passes the checkpoint's resolved root and the tripwire's configured one; a relative root is refused.
   b. Hook policy over 3500 chars goes to a Core-owned `policy.json` (`PE_HOOK_POLICY_FILE`), hash checked at every poll, capped at 1 MiB; a policy file that was named but cannot be loaded BLOCKS (never the empty default); the post hook still records.
   c. `roll_back`: a restore that raises hands the mission to `needs_operator` (message says nothing was written); a post-capture failure refuses and changes nothing.
   d. Tripwire: a named volume is exempt from its host-path check only when the proxy recorded authorising that exact volume for a container it recorded creating, and only when nothing besides discovery's blanket volume listing protects the path (operator `missions.protected` entries, parents, grant exclusions still win). `ProtectedSet.explicit` records the operator's own entries.
   e. Checkpoint: `retire_unused` forgets a checkpoint no session ever ran under so a retried start takes a fresh one; eligible but unremovable defers the start (`checkpoint_retire_failed`) rather than launching under the stale picture.
   f. Wording of the brief, the recovery classification and `cancel.held` now says what the checkpoint covers (scoped files and container settings), not "the host".
   **Left, with reasons:** (1) `capture_post` overwrites the post-change manifest on every `roll_back` retry from `needs_operator`/`rollback_pending`; that is the S5/S8 design and unchanged here, but it weakens drift detection on retries: worth a look in the next slice. (2) A `container.create` with enough long mounts can exceed the event body cap, so its vetted-volume record is truncated and the tripwire pauses on those volumes: a false pause, fail-safe, resume to continue. (3) A grant-level `protected.resources` entry that equals a discovered volume path cannot be told from the site's copy of it; use `missions.protected` or a grant exclusion to protect a volume against vetted creates. (4) A launcher failure after the session row exists is not covered by `retire_unused`.
   **Whole-branch Codex review (`codex review --base d861838`, 2026-10-23) FIXED, re-reviewed clean (`--uncommitted` x2; Claude review of the same diff by the author; full suite 6579 passed):** (1) checkpoint `_current` re-resolves a symlinked entry's given path and every other in-scope name that deduplicated to the same file (`aliases` in the manifest); a retargeted link is a problem and a restore refuses even with drift acknowledged. (2) Setup answers apply the Core UID-range rule (`uid_range_problem`, DynamicUser overlap) when missions are enabled. (3) `memory.store.confirm` reads the card and checks actor/type/status inside the write transaction, and the UPDATE is guarded on `status = 'candidate'`.
   **v3-production merged into v4-next 2026-10-24** (incident dismissal, automount reporting, Leela journal format, setup-server hotfix). Conflicts resolved by keeping both sides: `casa_scruffy.py` (investigate + dismiss routes) and `scripts/mos-boot/post-start.sh` (background subshell plus the launcher-boot cgroup step before Core starts). Two plan-id pins re-pinned in `tests/test_setup_missions.py` (MOS: hook body; full tier: `casa-stacks.service.template`). Full suite green after the re-pin except that file's final re-run (120 passed). `incident.dismiss` is a new operator RPC; the worker server refuses any key outside `WORKER_METHODS` (`missions/worker_rpc.py`), so it is unreachable from a session. Not Codex-reviewed on v4.
   **Claude `/code-review high` on `d676f5b..9a1ff1e` (2026-10-24), 9 findings.** FIXED: authorize reads the call budget and records the authorization under one lock (concurrent authorizations could overrun `tool_calls`); the mounts check survives `unarmed_automounts()` raising (the reachability report still goes out); Leela reads journalctl's colon-less `+0100` offset on Python < 3.11. LEFT, with reasons: (a) an unreadable `systemctl show` counts as "not armed": deliberate in the v3 PR and pinned by `test_unarmed_automounts_counts_an_unreadable_state_as_unarmed`; (b) provider detection by class name matches the existing pattern in `casa_stackctl.py`; (c) `dismiss_incident` treats "not in the latest full scan" as retired, and a degraded scan can also fail to observe: v3 behaviour from PR #15, worth a look in v3 follow-up (a dismissal needs an operator click and the next scan re-opens a still-failing incident); (d) `/api/undo` refused while setup is exposed: the same gate as apply and retry, and the CLI `undo --answers` is the fallback; (e) a journal root frozen at the first real apply: a failed first attempt then a changed install dir keeps the first root, which is where anything partial was written; (f) digest truncation keeps whole lines and could drop everything if one line exceeds the limit: the header is short and the limit is 3500; (g) the typed-steps scope exemption relies on `stacks_root` being wired into the mission store, which `missions/assembly.py` does.
   **Rehearsal on the tagged commit `516e20b` (2026-10-24):** privileged suite 259 passed, 5 skipped. The Ubuntu repair rehearsal passed 10 of 11 runs; one run failed (`canary-fix` ended `needs_operator` instead of `completed`, so `canary:only-web-recreated` and `canary:released` failed). The failing run had no record of why. The rehearsal now records the mission's last 12 events when a mission ends `needs_operator`/`failed`/`recovery_required`. 8 further runs were clean. Cause NOT identified; treated as a rare intermittent (timing suspected, e.g. the in-flight tripwire/firewall window), not proven. Re-run until it reproduces with the new diagnostics if it appears on the live host.
   **Live host upgraded to `v4.0.0-rc3` 2026-10-24** (from v3-production 7136e22, missions OFF). `/check` and the dashboard confirmed good. Rollback: restore snapshot `pre-v4` (taken with the v3 script) then check out 7136e22. First attempt to start was refused by the migration guard (no snapshot marker: the v3 snapshot script does not write one): a second snapshot with the v4 script fixed it, INSTALL.md updated (8b914d3). Next: watch for a few days; the next reboot is the first real test of boot settle; missions enablement is a separate step (see the private checklist).
   **Model broker built 2026-10-24 (v4.1 item, off by default): `missions/model_broker.py`, `missions.model_broker`, wiring in `runner.py`/`supervisor.py`/`assembly.py`.** A per-session loopback proxy: Core holds the model API key, a session gets a per-session access code as `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL`; only `/v1/messages` and `/count_tokens`, listed models, no `mcp_servers`/`container`/vendor-side tools, request and token budgets (promise reserved up front, charged from the vendor's `usage`, cut-short relays charged in full), pause/revoke/expiry cut in-flight relays and vendor connections, bounded connections and deadlines, capped refusal audit, key stripped from any session env. Verified: 70 unit tests, privileged test on the VM (a real session uid reaches its listener, key not in its env, both launcher strategies), and the real `claude` 2.1.310 CLI against a scripted stand-in vendor (plain request, and a tool call that writes `result.json`). Review: Codex 6 passes (every finding fixed with a test; usage limit hit on the 7th), then Claude `/code-review high` (10 findings: 9 fixed; left: none declined except the launcher env-size note, 13 variables are added on top of a 64-item cap). NOT yet run: a real mission with the real model and key; the Claude CLI binary and the hook interpreter must be installed where session uids can execute them.
   **Model broker, gateway mode (2026-10-24): the operator uses OpenRouter, not an Anthropic key and not a subscription login.** Anthropic's terms forbid intermediating subscription (OAuth) credentials, so the broker holds an API key only (documented in INSTALL.md). Added `upstream_path_prefix` (`/api`), `auth_scheme` (`bearer`), `session_env` (model-id mapping, values must be in `models`), `allow_body_keys`, and a body ALLOWLIST: only the standard Messages fields pass (a gateway's own fields such as `plugins`, `models`, `route` are refused), a response that echoes the key is cut, tighter model-id syntax, an env-size cap in config. Review: Claude `/code-review high` (8 findings, all fixed with tests); Codex SKIPPED for this change (usage limit until 19:44 on 2026-10-24, per the CLAUDE.md rule; it reviewed the broker's first six rounds). `scripts/model_broker_smoke.py` runs the real CLI through the broker to the real upstream: NOT yet run against OpenRouter (needs the operator's key). Live host: on rc3; rc5 is the first tag with gateway mode.
   **Deploy plan (operator, 2026-10-24):** the live Ubuntu host (v3-production today) is the only real-world test host, so v4 goes there once release-ready. Missions default off; enabling them on the live host is a separate step after the plain upgrade has run clean.
6. Operator: slice 0 signed off 2026-10-24. The two worst v3 carry-overs (`setup/server.py` serve loop exiting mid-install; `/api/undo` without `exposed()`) are fixed on branch `v3-hotfix-setup-server` (off `v3-next`, local, not pushed or merged); remaining v3 carry-overs in `G4-review.md` (the setup-server exit-mid-install one is worst); push and deploy are yours. MOS repair rehearsal not claimed (no compose plugin). Existing installs with an explicit uid range 61000-61999 must follow INSTALL.md "Moving an existing uid range".
Shut the VM down when finished: `virsh shutdown PE_UBUNTU_TEST`. Estimate to ship: roughly 2-3 working days (S10 is the largest item).

## Decisions since the brief
- 2026-10-13: brief §14 decisions 1-8 recorded.

## Open
- Pushed `v4-next` to origin (public repo: keep infra detail out of tracked files; private inputs go in gitignored `docs/private/`).
- Operator-side findings from the export, separate from v4: legacy sudoers wildcard line, open Docker TCP socket, broken VPN port-forward cycle, leftover timers, AdGuard in paused_containers, two credentials to rotate.
- MOS test VM was unreachable on 2026-10-13; needed before slice 6.

## Usage / account
Primary orchestrator: Claude Code (account A until the 2026-10-22 usage limit; continued on a second account). Budget notes: (none yet).

## Resume commands
- Tests: `pytest tests -x -q` (targeted first).
- Review: `codex review --base v4-next` / `--uncommitted`; Claude: `/code-review`.

## Deferred findings (owned by a later slice)
- Slice 7: standing-grant `window_json` is stored but not evaluated; grant `protected`/`exclusions` not cross-checked against standing scope or plan (spec §5.2 needs a window format).
- Slice 3: supervisor must confirm session dead BEFORE requesting a terminal transition (terminal transition now frees leases/session in one txn).
- Existing v3 code review findings (not v4): quiet re-scan dropped when refused in a maintenance window; MOS `docker-compose config --format json` may fail on Compose v1; severity wobble re-planning. Unverified.

## Review debt
- Slice 4 round 2 Codex review (missed at the 2026-10-13 usage limit): folded into the G4 whole-branch review.
- 2026-10-23: Codex usage limit hit; item 7 rehearsal fixes got Claude `/code-review` only. Codex skipped per the CLAUDE.md rule (no re-queue).

## Live host status (2026-10-24, end of day)
- Live host is on `v4.0.0-rc6` (tip `de22e2f`). Missions are ON: `missions:` block in the live config (OpenRouter broker, `/usr/local/bin/claude`, hooks at `/usr/local/lib/planetexpress/pe_hook.py`). Docker TCP 2375 closed by the reboot; boot settle and `/prepare_reboot` worked for real. Launcher setup applied (all six steps ok), launcher preflight PASS at startup.
- Smoke test (real `claude` through the broker to OpenRouter) passed. First real mission `M-1` (Propose tier, incident investigation) ran end to end: 4 broker requests, ~47.8k tokens, `result.json` written, 2 memory candidates quarantined, mission in `awaiting_decision`, nothing on the host changed.
- Fixed today: sessions cgroup parent was 0750 under the unit's `UMask=0027` (preflight failed; `de22e2f`). Boot-settle cap 30 -> 15 min plus a dashboard banner (`f15f01b`). Codex skipped for both (usage limit until 19:44, not re-queued); Claude review only.

## Open for tomorrow (in this order; user go still needed for 3 and 4)
1. **Mission UX.** The Missions tab and mission page exist, but for Observe/Propose the status label `awaiting_decision` is misleading (it means "result ready", there is no approval card), there is no button to close a finished mission, no Telegram message when a mission finishes, and no button to turn a proposal into a Repair mission (core has `mission.propose_repair`). Plan: relabel to RESULT READY + CLOSE (-> completed), Telegram summary with link, then the "turn into repair mission" button (approval-flow surface: full second review).
2. **Workspace cleanup bug (real, blocks uid reuse).** The Claude CLI creates `$HOME/.cache` as the session uid with explicit mode 0700, so Core cannot `rmtree` the workspace; `_reap_session` logs "workspace was not removed: uid N stays held" and `sweep_uids` can never free it (one of 1000 uids leaked per mission). The helper's `umask(0o007)` does not help against explicit modes. Operator workaround: `sudo rm -rf /var/lib/planetexpress/mission-workspaces/S-*` (done for S-70f02131cae015ce). Proposed fix: when the normal removal fails, run a short cleanup session as the same uid through the existing launcher (`rm -rf` the workspace contents), no new root code or sudo rule; needs a privileged VM test and second review (launcher/supervisor).
3. Containment rehearsal on the live host was skipped (run in a login shell it fails for two environmental reasons: cached sudo credentials defeat the "sudoers refuses arguments" check, and the shell is not in the service's delegated cgroup). Faithful way: `sudo bash -c 'echo $$ > <service cgroup>/core/cgroup.procs && exec runuser -u casaroot -- bash -c "sudo -K; ... mission_containment_rehearsal.py --workspace-root ..."'`. The process-tree escape check (double-fork/setsid) is still unproven on this host.
4. Smaller: Telegram HTML parse failure on an unescaped `<execution_id>` (retries as plain text); CLI warns it does not recognise `anthropic/claude-sonnet-latest` and assumes a 200k window (harmless; the docs describe a setting); INSTALL.md argv example says `/usr/bin/claude` (host has `/usr/local/bin/claude`); rotate the Subwave controller admin password (printed in chat earlier); config backup `config.yaml.bak-pre-missions-20261024` on the host; snapshots `pre-v4`, `pre-v4-marker`, `pre-rc5` kept.

## Direction change (2026-10-25): the Operator Console
Operator decision: the experience wanted is a PE-native conversation with an agent (investigate, options, confirm/scrap/modify, watch, verify), not the Missions board or one-shot Chat. Plan: `docs/designs/v4-operator-console-plan.md` (decisions 1-5 recorded, all as recommended). Design handoff for Claude Design: `docs/designs/v4-operator-console-design-brief.md`. **No feature code until the design is in and P0 (multi-turn agent under containment) is scoped.** The "open for tomorrow" list above is superseded: the workspace-cleanup bug folds into P1; the mission relabel/CLOSE/Telegram-notice/proposal-to-repair items are replaced by the Console. Still separate and small: rehearsal on the live host, INSTALL.md claude path, Subwave password rotation.

### Console status (2026-10-25, end of day)
- **P0 spike: done, GO for P1.** Findings and decisions: `docs/designs/v4-console-p0-findings.md` (budget 150,000 tokens per conversation; 10 open
  conversations; archive after 3 days idle; option (c) approved). Design package is in `docs/designs/planet_express_design_v23/`.
- **P1 slice 1: done on `v4-next`, NOT deployed** (the live host is still on `v4.0.0-rc6`). A: cleanup session frees a dead session's uncleanable
  workspace and uid. B: sessions launch with `--settings <Core-owned> --setting-sources ""`; config refuses a wrapper argv when
  `missions.hooks.install_claude_code` is on. C: launcher `state_dir` plumbing (helper change, root code). Each reviewed by Codex and Claude; VM suite
  green (269 passed, 5 skipped). Because A and C change the root helper and B changes the launch argv, **a deploy needs a new tag, the helper
  reinstalled (setup repair), and a mission run to confirm**; wait for the operator's go.
- **Next: slice 2, the turn runner** (no UI): conversation store, one contained process per message with `--resume`, event parsing, interjection, state
  directory creation and archive order, the planted-config test list, budget headroom fix. See the findings doc for the gates.
- Housekeeping still open: shut down the VM when finished (`virsh shutdown PE_UBUNTU_TEST`); delete the spike key file (`rm -rf ~/.config/pe-spike`);
  M-1 still in `awaiting_decision` on the live host; Subwave password rotation; INSTALL.md claude path.

### Console slice 2 (2026-10-25, night): the engine, no UI. On `v4-next`, NOT deployed, NOT tagged.
Live host is on `v4.0.0-rc7` (slice 1). Slice 2 is code only; nothing about it runs until the operator deploys it AND sets `missions.console.enabled: true`.

**What exists** (`planet_express/console/`, plus changes under `planet_express/missions/`):
- A conversation is a mission of `runtime: console`, tier observe, kept open across turns. Every operator message is one contained session of that mission
  (`Supervisor.start_followup`), run by `ConsoleRuntime` (`--session-id` then `--resume` of the id the stream confirmed; read-only tools; the message is the last
  argument after `--`). The conversation's ONE uid is reserved for its whole life (`reserved_uids`, `MissionStore.reserve_uid`; the allocator never gives it to
  another mission). Its agent state lives in `<workspace_root>/C-<id>` (helper `state_dir`, slice 1C). PE keeps its own redacted copy of every message
  (`console_messages`). Plans are validated by `console/plan.py` (generated from the check catalogue) and stored (`console_plans`).
- New edge `awaiting_decision -> investigating` (operator only, observe/propose only); anyone may now cancel out of `needs_operator` (resuming stays the operator's).
- `ConsoleService` (create/post/interrupt/archive + `tick`): follow-ups start at the supervisor tick; idle conversations archive after `archive_after_days`; an
  archive stops a running turn, closes the mission, empties the state directory with a cleanup session as the same uid, THEN frees the uid.
- `ModelBroker.set_mission_cap`: a turn may spend only what is left of its conversation's budget (set before the broker opens the turn's endpoint).
- Config `missions.console` (off by default; needs the broker, the forced-settings hooks, `console.model` in `model_broker.models`).
- Tables are additive (`console_*`, `reserved_uids`): no schema bump, an older Core ignores them.

**Verified:** 6,931 unit tests; the standard VM run (now 283, it includes `tests/test_console_privileged.py`) under both Core strategies, including the Console end to end
with the REAL supervisor, helper, worker sockets, model broker (stand-in upstream), the grant's env policy and the real claude CLI: two turns, one uid, resumed context, archive
that frees everything. Review: Claude `/code-review xhigh` (15 findings, all fixed with regression tests, commit e974507); a `high` re-review of that fix commit was run (see the
commit log). **Codex was SKIPPED for slice 2** (usage limit until 21:12 on 2026-10-25), per the CLAUDE.md rule; it is not queued.

**Not built (next):**
1. Slice 3, the UI and its API: nothing exposes `ConsoleService` over HTTP/RPC yet (dashboard routes, SSE/polling of messages and events, the Console page from the design package).
2. Telegram parity, plan cards and Confirm (P2: confirming mints the hash-bound Repair grant; `console_plans.status` already models pending/superseded/scrapped/expired/confirmed).
3. A resume that finds no conversation does not auto-restart from PE's stored thread (the turn fails and says so; the operator sends again). The first turn's id is only stored once confirmed, which removes the common cause.
4. Not yet tested: workspace-resident `.mcp.json`, `.claude/agents|skills|commands`, `CLAUDE.md` planted by a turn (see the findings doc); E6 with the installed `pe_hook.py`; 20 random kill points; one live E1 on the host.
5. The conversation's tool budget is counted by the installed hooks only (stream events are notes, to avoid counting each call twice).

**Before enabling on the live host:** deploy a new tag (code only; the rc7 helper already has `state_dir`), add `missions.console.enabled: true` and confirm `console.model` is
one of your OpenRouter `model_broker.models`; run one conversation, watch `journalctl -u casa-planetexpress`, check the uid is released after an archive.

### Console slice 3 (2026-10-26): the API and the page. On `v4-next`, NOT deployed, NOT tagged.
Live host is still on `v4.0.0-rc7`. Nothing below runs until deployed AND `missions.console.enabled: true`.

**What exists:** `planet_express/console/api.py` (`ConsoleApi`: list, live feed, plain-language status and verdict written by PE, open/post/interrupt/archive/scrap-plan) behind
`console.*` RPC methods in `application/mission_service.py` (the daemon attaches it in `casa_farnsworth.py`); dashboard routes `/console`, `/console/<id>`, `/api/console/*` in
`casa_scruffy.py` (CSRF on every POST, operator = the signed-in one, strict ids, Core's operator-written refusals shown); `templates/console.html`, `static/console.js`, `static/console.css`.
The page draws everything with text nodes only (a node test under a fake DOM that throws on markup APIs proves hostile text stays text). Plan cards are READ-ONLY: Modify pre-fills the composer,
Scrap sets the plan aside; there is no Confirm until P2. Checked in a real browser (desktop 1440x900, phone 390x844) with `scripts/console_preview.py` (canned conversations, no host).

**Verified:** node tests (`tests/js/console.test.js`, run by `tests/test_console_js.py`), API/RPC/dashboard tests, the full unit suite. Review: Claude `/code-review high` (11 findings fixed with
tests, commit 69c44c5); Codex pass on slice 3 see the commit log.

**Not built (next):** P2 (Confirm sheet: mints the hash-bound Repair grant from a plan option, live execution view, tripwire banner, PE verification in the thread, rollback); Telegram
parity (replies, plan-card buttons carrying the grant fingerprint); memory candidates at close; the "Work log" link still goes to the Missions page (P4 folds it in); the context panel
(Evidence/Scope/Limits/Record) is not drawn yet; incident -> conversation (`console.create` already takes `incident_id`; no button yet).
Known and left: the per-poll query count (bounded by `max_open` and the poll rate).

**Before enabling on the live host:** deploy a new tag (code only), set `missions.console.enabled: true` with `console.model` inside your broker's `models`, restart core, open `/console`.

### Console P2 (2026-10-26): plans, Confirm, the run in the thread. On `v4-next`, NOT deployed, NOT tagged.
Live host is still on `v4.0.0-rc7`.

**What exists:** a plan option carries a structured scope and exclusions validated against the host inventory (fails closed when no inventory); Confirm = two steps
(`console.confirm_prepare` proposes the Repair through the real `ApprovalFlow`, `confirm_authorise` approves bound to the grant fingerprint the operator saw, `confirm_withdraw`);
the run is followed in the conversation (`console/execution.py`: status, timeline, tripwire card, PE's verification with could-not-check distinct, controls pause/resume/rollback/take over);
progress lines are posted once each by `ConsoleService._report_executions`; the page has the confirm sheet, run blocks and a no-backup ask (`code: no_backup`; accepting it adds the elevated `no_backup` phase).

**Reviews:** Codex 7 findings and Claude `/code-review xhigh` 14 findings, all fixed with tests; a Codex pass on those fixes found 2 more, fixed. Notable fixes on the safety surface:
operator pause now scans while frozen and shows what changed since the last scan (a resume re-baselines, so it must not hide it); approval is all-or-nothing (elevation checked before any phase is approved);
withdraw never marks a run withdrawn that was approved elsewhere; a typed `backup_floor` flow code replaces string matching; rollback reports a refused restore and offers an explicit, two-click "roll back anyway" (`ack_drift`).
Verified: unit suite 7073 passed, VM privileged suite 283 passed, node tests.

**Known and left:** the per-poll query count (one `executions` query per idle conversation on list polls; bounded by `max_open`); take-over leaves nothing verifying (the thread says so);
the live host cannot run Repair missions until protected-set discovery is readable by the service (Confirm says "needs Repair missions" there).

**Not built (next):** Telegram parity; context panel; close-and-learn memory candidates; P4 retire old Chat/Missions (Work log link still goes to /missions). The first live Repair from the Console is the P2 acceptance test.

### Console Telegram parity (2026-10-26): On `v4-next`, NOT deployed, NOT tagged.
`planet_express/console/telegram.py` (`ConsoleTelegram`) over the same `ConsoleApi` as the page, dispatched from `MissionService.telegram` (`/ask /reply /threads /plan /confirm /authorise (/authorize) /withdraw /run /close`).
`/confirm <id> <A|B|C> <plan8>` binds to the plan code on the plan card read (a revised plan is refused); the card puts the `/authorise <id> <hash8>` line first and is bounded; Telegram never gives an
elevated approval (a no-backup option says to use the dashboard) and never acknowledges drift on a rollback. `/ask` `/reply` `/confirm` `/authorise` follow the conversation: a push (`push_async`, own thread, never in the
supervisor tick) sends the agent's answers, plan cards and PE's progress lines in batches under Telegram's limit, advancing the cursor (`console_telegram` table) only past what was sent. Untrusted text goes out as one
inert line (no line breaks, `/commands` defused). Logs carry the exception type only (the bot token is in Telegram errors' URLs).
Reviews: Codex (4) and Claude `/code-review xhigh` (15) fixed with tests. **Not verified live**: no real Telegram send has been made from this code.

### Console memory candidates (2026-10-26): On `v4-next`, NOT deployed, NOT tagged.
The agent may write `lessons.json` (<=5 a turn, <=20 a conversation; strict, bounded, scope names checked against the host inventory, refused if it names a scope and no inventory exists; instructions in `LESSONS_FORMAT.md`,
which says Ship's Memory is unavailable where its tables are missing). `console/lessons.py` + `ConsoleRuntime._lessons` turn them into CANDIDATE cards (source `inferred`, actor `runtime:console-<id>`, evidence
`conversation:<id>`) linked in `console_lessons`; secret-shaped ones are refused by the memory store and reported without echo; an already-offered lesson is never rewritten. The thread shows "Worth remembering" with Keep / Edit /
Discard (`ConsoleApi.lesson_decide`, RPC `console.lesson`, route `/api/console/<id>/lesson`): keep = the memory store's operator-only confirm (an edit is stored as operator-stated, still secret-checked, never truncated).
Quarantine is unchanged: nothing is retrievable by any session until kept (tested).
Reviews: Codex (3) and Claude `/code-review xhigh` (14) fixed with tests; left on purpose: the decision logic lives in `ConsoleApi` rather than `MissionService._card_change` (two places confirm a card), and the feed
reads each lesson's card per poll (<=20, bounded). Telegram has no lesson commands: its pushed message says to open the Console.
Unit suite 7118 passed. Not verified live (needs a real conversation where the agent offers a lesson).

### Console context panel (2026-10-27): On `v4-next`, NOT deployed, NOT tagged.
The feed carries `context` (`ConsoleApi._context`), read from PE's own records and never the agent's account: Evidence (incident id, turns, budget used), Scope (stacks, services, paths and the count of ENFORCED
protection only: free-form `will_not_touch` notes are not counted; `read_only` / `run` / `last_run` so an ended run is never shown as actionable), Limits (model budget, minutes, steps of the run's grant), Record (events across the
conversation's mission and every run's, runs, checkpoint taken). `static/console.js` `renderCtx` draws four tabs with text nodes only; wide screens show it as the third column, under 900px it is a modal bottom sheet behind a
Context button (focus in, Tab kept inside, Escape returns focus, reset when the operator changes conversation).
Codex (6, then 3 more on the fixes) and Claude `/code-review high` (8) fixed with tests: focus survives poll redraws, `[hidden]` CSS override, the sheet is inert behind it and reverts on widening, Limits says whether it is the conversation's or the run's, `MissionStore.activity_count` replaces a private-connection query. Left on purpose: `feed()` reads executions and missions a few times per poll (bounded, same known cost as before). Unit suite 7120 passed.
Left: Evidence has no per-container levels or "context used %" from the design (needs inventory/levels in the feed); Record shows counts only, no links.
**Not built (next):** P4 retire old Chat/Missions pages (Work log folded in); incident -> conversation button.


### v4.0.0-rc8 (2026-10-27): tagged on `v4-next`, NOT deployed. Live host is still on `v4.0.0-rc7`.
Adds, since rc7: Console P2 (plans, Confirm, the run in the thread), Telegram parity, memory candidates, the context panel. Verified: unit suite 7120 passed; privileged suite 271 passed / 17 skipped in Docker and
283 passed / 5 skipped on the Ubuntu test VM (`PE_PYTHON=/opt/pe-venv/bin/python PE_VM_DIR=/opt/pe-privileged-tests scripts/run_privileged_tests.sh --vm 192.168.122.142`; the MOS VM was off and was not run).
**Not verified live:** a real Telegram send from the console code; a real conversation where the agent offers a lesson; the context panel in a real browser; the first live Repair from the Console (the P2 acceptance test).
**Not built (next):** P4 retire old Chat/Missions pages (Work log folded in); incident -> conversation button.

### v4.0.0-rc9 (2026-10-27): replaces rc8 (do not deploy rc8). On `v4-next`, NOT deployed until the operator restarts the services.
rc8 did not import on the live host's Python 3.12: `ConsoleApi.list()` shadows the builtin `list` inside the class body, so `-> list[dict]` in `_lessons` raised `TypeError` at load. The dev venv is **Python 3.14**
(annotations are lazy), so the whole unit suite passed. Fix: `from __future__ import annotations` in `console/api.py`. **Lesson:** before any deploy, import every module and run the suite under the host's interpreter
(Ubuntu test VM: `/opt/pe-venv/bin/python`, 3.12), and load the live config from a throwaway `git worktree` of the tag before touching the running checkout.
3.12 results on the VM as root: 67 failures, 66 identical on rc7 (the tests assume a non-root Core), 1 the same kind (`test_console_service` archive/cleanup-session expects Core unable to remove a directory; root can). Non-root: 0 expected.

### Investigation access (after rc9; becomes rc10): "broad read, narrow consequences". On `v4-next`, NOT tagged or deployed.
The first live Console conversation ("why is radarr failing?") had nothing to investigate with: no Docker, no logs, no inventory. The design rule from that: an investigation session may READ the host broadly, and nothing it can reach may change anything. Needs are never enumerated in advance.
- **Docker, read-only** (a2621ce): Observe/Propose sessions get a per-session read-only Docker proxy (list, inspect, events, stats, logs). Built without Repair wiring, so it exists on this host. Logs are redacted whole-line across frames before they reach the session (egress rule). `archive` (file reads out of containers) and `top` (process args/env) are left out on purpose.
- **Host logs**: the launcher gives those sessions the supplementary groups `adm` and `systemd-journal` (so `journalctl` and `/var/log` read as the operator reads them), and nothing else. Opt-in: `missions.read_log_groups: [adm, systemd-journal]` (an ALLOWLIST of exactly those two; config, launch.conf and the helper each hold it, a test keeps them equal). Core sends the spec flag `read_logs` only for observe/propose and only when configured; the helper takes group NAMES from the root-owned launch.conf (optional key `log_groups`), never from the spec. Preflight launches its probe with the flag and fails startup unless the probe holds exactly the configured groups, so an old helper or a stale launch.conf is caught before the first session.
  **Deploy consequence: the launcher helper and launch.conf must be reinstalled (sudo) BEFORE `read_log_groups` is turned on**; without the setting nothing changes, and an older helper still works. `python3 -m planet_express.missions.launcher.setup_steps repair --core-user USER --workspace-root DIR --log-group adm --log-group systemd-journal` (or the wizard with `missions.read_log_groups` in the answers).
- **Discovery tolerance**: a directory below the stacks root that Core cannot read (a container's data dir) no longer sinks the whole scan: it is recorded as a blind spot and protected whole (`DiscoveredInputs.blind`). An unreadable stacks root is still a failure. This is what lets Repair wiring, the inventory, plans and Confirm exist on a real host.
- Review: Codex 2 rounds on the Docker work, 2 on this (4 findings fixed: allowlist not denylist, setup-plan plumbing, preflight binding). Unit suite 7150 passed before the last two fixes. Claude `/code-review` (9 findings: fixed the weakened post-drop group check, the setup warning, the docstring; left as-is: the unredacted journal/`/var/log` is the accepted policy (the operator asked for broad read; those logs reach the model unredacted, unlike Docker logs), group names resolve through NSS in the root helper (root-owned conf, names allowlisted), a failed helper step after the conf step would leave an old helper refusing the new conf (recovery: snapshot restore), `blind` is reported state with no other consumer yet). VM privileged suite: 285 passed / 5 skipped on Ubuntu 3.12 (one fd-map test failed once, passed on rerun: unexplained flake). Unit suite 7153 passed.
- Still deliberately blocked: secret files (hook), `inspect` env values are still readable (documented, not redacted).
