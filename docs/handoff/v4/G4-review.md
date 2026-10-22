# G4 release review record (2026-10-22)

Reviewers: Codex (`codex review --base v3-next`), Claude `/code-review high` on `v3-next...v4-next` (a first run on the wrong
range, v3-next vs v3-production, is logged under "v3 carry-overs"), `/cso` (focused v4 surface, daily mode).

## /cso
No finding at 8/10 or above. Hits were redaction regexes (memory/store.py) and a fake test stub `.env`. No shell=True, TLS-off,
pull_request_target. CI actions are tag-pinned not SHA-pinned (MEDIUM hardening, deferred). Worker socket allowlist is structural.
Not done: dependency CVE scan, full STRIDE, saved JSON report.

## Codex
| # | Finding | Disposition |
|---|---|---|
| C1 | P1 runner.py:249 protected-set discovery inputs never passed (data_volumes, ingress, dns, credentials, backup stay at defaults) | CONFIRMED. Fix: `G4-brief-protected-discovery.md` (worker running) |

## Claude /code-review (v4 range): 22 findings
Batch F2 = safety fixes (brief `G4-brief-F2.md`), F3 = functional/robustness, S10 = folded into slice 10, DECLINED/OPERATOR = reason given.

| # | File:line | Finding | Disposition |
|---|---|---|---|
| 1 | approval_flow.py:95 | card clips check params to 200 chars; approval binds the full-plan hash | F2 (show in full or refuse card) |
| 2 | approvals.py:190 | critical grant elevation not in the one predicate | F2 |
| 3 | approval_flow.py:133 | grant `exclusions` shown on card, never enforced (tripwire/broker/proxy) | F2 (enforce as protected-like, or remove from card; enforce preferred) |
| 4 | tripwire/signals.py:185 | container identity from its own compose labels: spoofed in-scope label hides a change | F2 |
| 5 | launcher/side_doors.py:240 | `--tlsverify` substring skips `--tlsverify=false` | F2 |
| 6 | runner.py:113 | stop() halts tripwire before sessions are killed | F2 (after G4-F1 merges: same file) |
| 7 | supervisor.py:403 | session expires_at not extended on resume | F2 |
| 8 | config_schema.py:460 | default uid range 61000-61999 overlaps systemd DynamicUser; launcher/config.py has safe DEFAULT_UID_RANGE unused | F2 |
| 9 | execution/policy.py:146 | mission-origin network-guard ignores empty-param steps (stack.up_all, unit.action, prune.safe) | S10 (latent: origin not wired yet); S10-notes updated |
| 10 | launcher/setup_steps.py:698 | worker sudoers names every session uid, so read-only tiers inherit the sudo rules | OPERATOR/DOCUMENT: opt-in (`worker_sudo_all_sessions`, acknowledgement required). S9 INSTALL warns; per-tier uids not knowable at generation time. Revisit if dynamic sudoers is wanted |
| 11 | supervisor.py:1266 | transient LeaseConflict/NoFreeUid counted as failed starts, mission to needs_operator in ~6 s; checkpoint already spent | F3 (acquire leases first; wait condition) |
| 12 | supervisor.py:1203 | blocking checkpoint/baseline on the poll thread starves other sessions' lease heartbeats | F3 |
| 13 | tripwire/signals.py:196 | docker ps/inspect race: removed container fails the scan, false pause | F3 (tolerate missing ids / retry once) |
| 14 | supervisor.py:261 | `evidence_provider` never wired: incident evidence never reaches the agent | F3 |
| 15 | farnsworth.py:1614 | `/rollback M-<n>` caught by the v3 handler | F3 |
| 16 | brief.py:36 | Observe/Propose instructions used for Repair briefs | F3 |
| 17 | supervisor.py:755 | resume re-baselines before transition/thaw; failed resume loses original trip evidence | F3 |
| 18 | supervisor.py:287 | launcher_ready caches a pass forever | F3 (re-prove periodically or at each start) |
| 19 | tools.py:375, supervisor.py:1413 | per-call full-event-log scans (json_extract / pydantic load); budget check not atomic; worker can self-exhaust decision quota via a note | F3 |
| 20 | tripwire/signals.py:521 | `deadline` min() makes SCAN_DEADLINE_S dead; abandoned collector threads accumulate | F3 |
| 21 | verify/checks.py:56 etc. | dead/duplicated constants (SHELLS, _PUBLISH, DEFAULT_UID_RANGE) | F3 (cleanup) |
| 22 | (F2 #1/#4 overlap) | none | - |

## v3 carry-overs (from the wrong-range run; NOT v4 code, `v3-next` vs `v3-production`)
Operator to triage, likely a v3-next hotfix branch: setup/server.py:477 serve loop exits mid-install when idle expiry lapses
(kills the daemon worker thread as root); setup/__main__.py:148 journal_root re-runs discover() per poll and moves with install_dir;
static/setup.js:91 Telegram token cleared on failed find-chat; casa_farnsworth.py:1315 rescan identity keyed on LLM text;
setup/server.py:354 /api/undo lacks the exposed() check; casa_zoidberg.py:428 digest truncation can split HTML; setup/discover.py:237
any global IPv6 = "public", blocks setup; command_service.py:1173 duplicate @staticmethod; farnsworth.py:1952 double target
resolution; scripts/mos-boot/post-start.sh:8 leaks vars and blocks 130 s.
