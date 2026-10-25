# Operator Console, phase 0: findings (interim, 2026-10-25)

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

## Still to run before the P1 go/no-go

- **Under containment (VM, real launcher):** E1, E2, E4 (including cgroup freeze for 60 s and thaw, kill mid-tool-call), E6 with the installed
  `pe_hook.py`, and E8 (uid and workspace lifetime, the cleanup session that fixes the leaked-workspace bug).
- **With a real model (needs the operator's go; token cap applies):** E5 plan schema validity (at least 18 of 20), E7 cost curve over ten
  turns, and one live run of E1 on the host through the broker.
- More E4 samples (20, randomised) once under containment.

## Open questions for the operator
None yet. The tokens for E5 and E7 need an explicit go.
