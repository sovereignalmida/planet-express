# Operator Console, phase 0: findings (interim 2, 2026-10-25)

Scope and pass lines: `v4-console-p0-spike.md`. Scripts: `scripts/spikes/console_p0_*.py` (throwaway). CLI under test: Claude Code
2.1.310. Everything below was measured against a stand-in Messages API (no model, no cost), outside the launcher, on the developer
machine, unless it says otherwise. **Not yet run under containment (uid, cgroup, hooks as installed) or with a real model.**

Baseline: the privileged suite on the VM passes (261 passed, 5 skipped, three clean runs; one run in four hit the known intermittent
fd-inheritance test, which predates this work).

## Results

| Exp | Result | Evidence |
|---|---|---|
| E1 resume across processes | **Pass.** A second process with `--resume <id>` replays the first turn's user and assistant messages. Each turn used only its own API key (the broker access code), so a code per turn is enough. Resume from a different working directory also kept context. | The second turn's request body contained the first turn's text and reply. |
| E2 live events | **Pass.** `--output-format stream-json --include-partial-messages --verbose` emits one JSON object per line as the model streams: `system:init`, `stream_event:*` (message_start, content_block_delta ...), `assistant`, `result:success`. Deltas arrived at the same times the stand-in sent them (0.66 s, 1.07 s). Every line parsed. The stream carries the session id. | Arrival times recorded per line. |
| E3 interjection (streaming input) | **A message sent mid-reply is queued, not applied.** The first reply ran to completion, then the second message was handled with full history. There is no implicit interrupt. | Result events at 5.29 s then 5.41 s; second request held both messages. |
| E3b interrupt | **A `control_request` with subtype `interrupt` stops a streaming reply within about 10 ms**, returns a `control_response`, ends the turn as `error_during_execution`, and the process then takes the next message with context kept. **The session id reported by the stream changed after the interrupt**, so PE must record the id the stream reports each turn, never assume it is stable. | Interrupt sent at 2.0 s; turn ended at 2.01 s. |
| E4 kill then resume | **Pass with one caveat.** Killed (SIGKILL, whole process group) at 0.9 s, 1.6 s and 2.4 s: resume worked and the context was intact, including the operator message. Killed at 0.3 s the message was not yet saved and resume failed cleanly: `No conversation found with session ID`. | 4 samples; more needed. |
| E6 hooks on resume | **Pass.** A PreToolUse hook that denies Bash denied it on the first turn and on both resumed turns, and the hook log recorded every call. (The third turn exited 1; that is the stand-in's endless tool loop hitting `--max-turns`, not a hook failure.) | Hook log: 5 Bash calls across 3 turns, all denied. |

## What this means for the design (preliminary)

1. **Resume works across separate processes, so turn by turn is viable.** One process per operator message, `--resume` with the stream's
   current session id, a fresh broker access code per turn. This is the simplest fit with the supervisor as built (one session per run,
   uid and cgroup released or held by policy, crash handling already classified).
2. **Live trail is straightforward.** Parse stream-json per turn into the existing event kinds; redaction and truncation stay in PE.
3. **Interjection needs an explicit stop.** Streaming mode queues an interjecting message; only the `interrupt` control stops a turn.
   Per-turn processes have no such control, so an interjection there means stop the process (SIGTERM, then SIGKILL after a short wait) and
   start the next turn with the operator's message. That matches decision 3 (pause and hand back control) and the context is preserved
   once the operator message has been saved (about one second in). PE keeps its own copy of every operator message, so the sub-second
   window loses nothing: if resume says no conversation, PE starts the turn fresh with the stored message and the thread summary.
4. **Streaming input is not needed for P1.** It gives a cleaner interrupt, but it keeps one process, one cgroup and one uid alive per
   conversation and changes the session id under us. Revisit only if turn-by-turn latency feels bad in use.

## Under containment (VM, real launcher, both strategies; `tests/test_p0_console_privileged.py`)

| Exp | Result |
|---|---|
| E1 resume across contained launches | **Pass.** Turn 2 in a *new* session (new cgroup, new fresh workspace, new access code, same uid) resumed the conversation; the stand-in saw turn 1's message; only turn 2's own code was used. The whole privileged suite (261 tests) still passes alongside. |
| E2 live stream-json as a session uid | **Pass.** Events were written and parsed as in the uncontained run. |
| E4 kill and freeze under containment | **Pass.** `cgroup.kill` mid-stream, then resume: context intact. A 8-second freeze mid-stream then thaw: the turn finished by itself with `result:success`. (More random-point samples still wanted.) |
| E8 workspace lifetime | **Two hard constraints found, one fix verified** (below). |

### E8, what it means for P1

1. **The helper refuses to launch into a workspace that contains a subdirectory** ("workspace entry"; it hands over regular files
   only, deliberately). A turn leaves `cfg/` (and `.cache`), so a *second* turn cannot reuse the first turn's workspace. Conversation
   state therefore cannot live in a workspace the helper vets each turn.
2. **The leaked-workspace bug reproduces** under the unprivileged-Core strategy: a `drwx--S---` directory created by the session defeats
   Core's group access, and Core's `rm -rf` leaves the workspace and holds the uid. (The stand-in run alone did not create it; the live
   mission's `.cache` came from a tool-using turn, so the test creates it explicitly as the session uid.)
3. **Verified fix:** a short cleanup session as the same uid, launched from an empty scratch directory, running
   `find <old workspace> -mindepth 1 -delete`; then Core removes the now-empty directory. No new root code, no sudo change. (My earlier
   idea of launching the cleanup *in* the old workspace would have been refused by point 1; the scratch directory is what makes it work.)
4. **State persistence needs a decision (P1 design):**
   - **(c, recommended)** a conversation state directory outside the vetted workspace: a fixed root, one directory per conversation,
     created by the helper (root) owned by the session uid with Core's group, handed to the session with `HOME` and `CLAUDE_CONFIG_DIR` pointing
     into it. The helper only creates it and opens it with no-follow; it never walks its contents. Needs a small, reviewable helper addition
     (second review).
   - **(e)** no persisted CLI state: PE stores the conversation (it already stores every event) and starts each turn fresh with the
     prior turns, plus condensed tool results, in the prompt. No helper change and trivial cleanup, but the agent loses exact tool-result
     memory unless PE re-supplies it, and each turn re-sends the history either way.
   My recommendation is (c); (e) is the fallback if the helper change is unwelcome.

### New security requirement found: settings sources

With persisted state, **a settings file left in the state directory takes effect on the next turn by default** (a hook planted in
`CLAUDE_CONFIG_DIR/settings.json` ran, and its permission allow-list applied). A turn could therefore weaken the next turn's hooks.
**Passing `--setting-sources ""` ignores it, and PE's own hooks passed with `--settings` still fire** (both verified). P1 must always
launch with `--setting-sources ""` and pass hooks and permissions only through `--settings`; a test must assert a planted settings
file is ignored. This applies to the existing single-run missions as well (low risk there, since no later run existed).

## Real-model results (E5, E7), 2026-10-25

Run through the real broker to OpenRouter (`~anthropic/claude-sonnet-latest`), outside the launcher (the model path is what is measured).
Total 63 requests, 366,180 tokens charged (258,306 in, 107,874 out), under the 600,000 cap.

**E5, plan schema reliability: pass.**

| Method | Runs | PE accepted |
|---|---|---|
| Agent writes `plan.json` (Write tool), PE validates | 20 | **20** |
| `--json-schema` with the full plan schema | 6 | **6** |

(The schema method stopped at 6 of 9 when the E5 half reached its share of the token cap; 6 of 6 is a thin sample, the file method is the
proven one.) Three fixtures, rotated. Every plan passed PE's own `parse_plan` on each option's checks.

What it took to get there, which is the real finding:
1. **The agent must be given the real schema with its bounds.** With only field names and types, every plan was rejected by PE
   (`timeout_s` and `observe_s` out of range). PE caught every one; none was repaired silently. With each check's full JSON Schema in the
   prompt, 26 of 26 passed. P1's plan prompt is generated from `PARAM_MODELS`, never hand-written.
2. **`--json-schema` needs a real schema.** With `{"type": "object"}` the model returned `options` as a JSON string. With the full schema it
   was clean.
3. **In print mode a tool the agent may use must also be in `--allowedTools`**, or Write is silently declined and no file appears.
4. PE-side validation stays mandatory. The agent was wrong 100% of the time when under-specified, so acceptance is PE's call, not a model
   property.

Not yet tested: an agent asked to widen scope or invent a check (validation refuses unknown kinds and extra fields by construction; a
direct test belongs in P1's suite).

**E7, cost curve: pass.** Ten turns resumed in one conversation with tool use at turn 1, 10 s typical per turn, all exit 0.

| Turn | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| Charged tokens | 4,106 | 2,937 | 3,757 | 4,499 | 5,143 | 1,756 | 1,939 | 1,707 | 2,068 | 2,627 |

About 30,500 tokens for ten turns (roughly 3,000 a turn), flat rather than climbing, which says the upstream is caching the shared history
and the broker is counting the uncached part. A planning turn that investigates and writes a plan costs about 13,000 (E5 average). So a
conversation of ten chat turns plus two plan turns is around 60,000 tokens. **Recommended default conversation budget: 150,000 tokens**
(warn at 80%), which leaves room for a long investigation. Per-turn cost should be shown to the operator in the Limits panel.

**Broker finding: the budget check reserves too much.** Before relaying a request the broker reserves its declared `max_tokens` plus the
body size (`model_broker.py`, `promise`). The CLI declares a large output allowance, so with a 50,000 budget the second request was refused
after only 5,259 tokens had been used. A conversation budget must carry that headroom on every request, or P1 must lower the declared
output cap for conversations (the CLI honours `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) so the displayed "budget left" is honest. This touches the
broker (safety surface), so it gets the second review.

**Other surprises:**
- The CLI warns that the gateway model id is unrecognised and assumes a 200k window. Cosmetic, but set the window for the gateway.
- A bad key made the CLI retry for about three minutes a turn. The broker's request cap bounded it, but a conversation should stop on the
  first 401/403 and say so; P1 treats upstream auth failure as a hard stop with a plain message.
- Spike lesson: the first full run burned about three hours on a double-pasted key. The runner now stops at the first 401/403.

## Still to run before the P1 go/no-go

- **Under containment:** E6 with the installed `pe_hook.py` (the stand-in run proved the mechanism; the real hook needs the mission store),
  a kill mid-*tool-call*, 20 randomised kill points, a 60 s freeze, and the uid-holding policy for idle conversations (open question 2).
- **With a real model:** one live run of E1 on the host through the broker (E5 and E7 are done, above).
- More E4 samples (20, randomised) once under containment.

## Open questions for the operator
None yet.
