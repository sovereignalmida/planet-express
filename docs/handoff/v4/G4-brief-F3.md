# G4-F3 — functional and robustness fixes from the G4 /code-review (rows 11-21 of `G4-review.md`)

Branch from `v4-next` (includes S9, G4-F1, G4-F2). Tests first; each finding fixed or declined in writing. No push, no privileged
tests here, trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`, synthetic fixtures only. This touches
execution-safety code: finish with `codex review --base v4-next` and fix or decline each finding.
11. supervisor.py ~1266: LeaseConflict/NoFreeUid/SessionConflict are transient: not counted as failed starts toward needs_operator;
    acquire leases BEFORE the expensive checkpoint/baseline; wait with a bounded, recorded reason.
12. supervisor.py ~1203: checkpoint.create + tripwire.begin + launch wait run on the single poll thread and starve other sessions'
    lease heartbeats: keep heartbeating live sessions during a start (or run the start off the poll thread with a bounded worker).
13. tripwire/signals.py ~196: `docker ps -aq` then one `docker inspect` of all ids fails if a container vanished in between, causing
    a false pause: tolerate missing ids / retry once; a genuine docker failure still fails the signal.
14. supervisor.py ~261: `evidence_provider` is never wired: investigate_incident stores evidence in a note but the brief says "none
    provided". Wire it in runner/assembly so incident evidence reaches the brief (redacted, bounded).
15. casa_farnsworth.py ~1614: `/rollback M-<n>` is intercepted by the v3 `/rollback <execution_id>` handler: dispatch mission ids to
    MissionService first; `/help` text must match.
16. brief.py ~36: the Observe/Propose INSTRUCTIONS text is used for Repair briefs; make instructions tier-specific (Repair: may change
    only what the grant scope names, plus its verification and rollback duties).
17. supervisor.py ~755 resume: re-baselining before transition/thaw loses the original trip evidence if the resume then fails: take the
    new baseline only after the transition and thaw succeed (or keep the old baseline file until then).
18. supervisor.py ~287: `launcher_ready()` caches a passing preflight forever: re-prove periodically and at every Repair start.
19. tools.py ~375 `ToolBroker._count`/budget and supervisor.py ~1413 `request_decision`: per-call full-event-log scans; make the budget
    check atomic with recording (one transaction) and use SQL COUNTs scoped to the actor/session; a worker note must not be able to
    consume its own decision quota (pin the event actor).
20. tripwire/signals.py ~521: `deadline = min(signal_timeout_s, deadline_s)` makes SCAN_DEADLINE_S dead config, and timed-out
    collector threads are abandoned and respawned each scan: honour both bounds and do not respawn a collector still running.
21. Remove dead/duplicated constants: verify/checks.py SHELLS (dup of config_schema _VERIFY_SHELLS), hooks._PUBLISH, any other unused.
Report: files changed, tests, Codex findings and dispositions, declines with reasons.
