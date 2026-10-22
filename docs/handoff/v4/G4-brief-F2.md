# G4-F2 — safety fixes from the G4 /code-review (execution-safety code: second review required)

Source: `docs/handoff/v4/G4-review.md` rows 1-8. Each fix: failing test first, then the fix; a finding you decline needs a written reason.
Branch from `v4-next` (it now includes S9 and G4-F1). Do not push. Never run privileged tests here. Trailer
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Synthetic fixtures only.

1. `missions/approval_flow.py:~95` `_plan_view` clips each verification-check param to 200 chars while approval binds the hash of
   the full plan: the operator approves a tail they cannot see. Show params in full (bounded by the schema maxima: url 512,
   arguments_json 2048) or refuse to render an approvable card when anything would be clipped. Test: a >200-char http URL / exec
   argv / query arguments appear complete on the card.
2. `missions/approvals.py:~190` the one execution predicate must require the `grant` approval row to be `elevated` for a critical
   mission, not only `ApprovalFlow.approve`. Test: a non-elevated grant row recorded directly does not satisfy a critical mission.
3. Grant `exclusions` (card `approval_flow.py:~133`, standing-grant "exclusions required"): nothing enforces them. Enforce them as
   protected-like in the tripwire classification, the tool broker and the Docker proxy scope (an excluded resource is outside scope
   even if inside the grant scope), with tests at each of the three. If a layer genuinely cannot, say so and make the card stop
   implying a hard carve-out for it.
4. `missions/tripwire/signals.py:~185` and `tripwire/diff.py`: a container's resource identity is taken from its own compose
   labels, so a session-created container labelled with an in-scope project/service is judged in-scope and never pauses. A
   container ADDED during a session must not be classified in-scope from its own labels; classify by what Core knows (the
   baseline's set, the proxy's recorded creations) and treat an unknown new container as out-of-scope (pause). Test: spoofed
   label => pause; a container the proxy recorded as created by this session => allowed.
5. `missions/launcher/side_doors.py:~240` `if "--tlsverify" in value` treats `--tlsverify=false` as safe, and does not require a CA.
   Parse the flag properly (true/1/absent-value only) and require `--tlscacert`/equivalent for a TCP listener to count as
   protected; otherwise report exposure. Tests for `=false`, `=0`, no CA, and the genuinely safe form.
6. `missions/runner.py:~113` `MissionRunner.stop()` stops the tripwire before sessions are killed. Order: stop accepting starts,
   `supervisor.shutdown()` (sessions killed and proven gone), only then stop the tripwire monitor. Test the order with fakes.
7. `missions/supervisor.py:~403` session `expires_at` is fixed at `wall_s+300` and not extended when a tripwire pause is resumed
   (wall budget is shifted, expiry is not): resume must extend the session expiry by the paused duration (and refresh the
   fence/lease as needed). Test: pause 15 min of a 30 min mission, resume, the session still resolves.
8. `config_schema.py:~460` default session uid range 61000-61999 overlaps systemd DynamicUser (61184-65519).
   `launcher/config.py` has `DEFAULT_UID_RANGE` (200000-200999) that nothing uses: use it as the schema default, keep one source
   of truth, add a validator that rejects any range overlapping 61184-65519 and any range containing existing passwd uids it
   can see; migrate existing configs deliberately (do not silently change a range that already has live sessions; document).

After green: `codex review --base v4-next`; fix or decline each finding in writing. Then run the full non-privileged suite.
Report: files changed, tests, Codex findings and dispositions, anything declined with reason.
