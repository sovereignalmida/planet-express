# Operator Console, phase 0: the spike (scope)

Status: scoped 2026-10-25, approved direction in `v4-operator-console-plan.md`. Runs while the Console UI is being designed.
Throwaway by design: scripts under `scripts/spikes/` and one findings file. No production code, no schema change, no
change to the safety surface. Nothing from the spike ships; what ships is built in P1 from what it proves.

## 1. The one question

Can one agent context persist across many operator messages while staying inside v4's containment, and can PE see what the
agent is doing as it happens? Everything else in the Console plan (conversations, live trail, plan cards, interjection)
depends on the answer, and on *which* of two shapes works:

- **Turn by turn.** Each operator message runs one contained agent process that resumes the same conversation context.
  Simple, crash-safe, fits the supervisor as built. Live output is per turn.
- **Streaming session.** One long-lived contained process that receives operator messages as input events while it runs.
  Lets an interjection reach the agent mid-turn. More moving parts.

The plan works with either. The spike decides.

## 2. What the installed CLI offers (2.1.310, checked 2026-10-25)

`--resume <session-id>`, `--session-id <uuid>`, `--continue`, `--fork-session`, `--no-session-persistence`;
`--input-format stream-json` and `--output-format stream-json` (print mode), `--include-partial-messages`,
`--replay-user-messages`; `--json-schema` for structured output; `--append-system-prompt`, `--settings`,
`--permission-mode`, `--add-dir`, `--bare`. No custom build is needed.

## 3. Experiments (each has a pass line; run on the VM first, then once on the live host)

All runs use the real launcher (session uid, cgroup, workspace outside home, hooks installed) and the real broker. VM runs
use a stand-in upstream so they cost nothing; one final run per experiment goes through the real upstream on the live
host with a hard token cap.

**E1. Resume across processes.** Turn 1 with `--session-id X` states a fact and reads a file. Turn 2 is a *new process*
with `--resume X` and asks about both. Do this with a different broker access code per turn (the broker issues a code per
session) and with the config and home directories inside the workspace.
Pass: the context is intact; no credential or path outside the workspace is touched; the second process works without
the first one's access code.

**E2. Live output to PE events.** Run a turn with `--output-format stream-json --include-partial-messages`. Parse it as it
arrives. Map each message, tool call and tool result to PE's existing event kinds, with redaction before persistence.
Pass: events appear within about 2 seconds of the agent doing something; a planted secret in tool output never reaches
the stored event; a 10 MB tool result is truncated, not stored; malformed or truncated lines don't crash the parser.

**E3. Interjection, both shapes.** (a) Turn by turn: pause (cgroup freeze) or stop the process mid-turn, then start a
new turn with the operator's message; check the agent recovers from a half-finished turn. (b) Streaming: one process with
`--input-format stream-json`; send a second user message while it is running; observe whether it queues, interrupts or
merges. Pass for (a): no corrupted context after a kill at any point, repeated 20 times at random points. Pass for (b):
the behaviour is deterministic and we can describe it in one sentence.

**E4. Pause and freeze safety.** Freeze the cgroup mid-stream for 60 seconds, thaw; kill mid-tool-call and resume.
Pass: the process either continues cleanly or fails in a way the supervisor already classifies; no hung socket on the
broker side, no leaked connection (the broker already bounds both).

**E5. Structured plan output.** From realistic incident fixtures (three historical shapes already in the test suite), ask
for a plan in the proposed schema: 1 to 3 options, each with changes, exclusions, risk, rollback, and verification checks
chosen *only* from PE's catalogue. Use `--json-schema` and, separately, a file the agent writes that PE validates.
Pass: at least 18 of 20 runs per method produce a plan PE accepts; every rejection is detected by PE, never silently
repaired; an agent asked to widen scope or invent a check is refused by validation.

**E6. Hooks and tool limits survive resume.** The pre/post hooks and the allowed/disallowed tool lists must apply on a
resumed turn exactly as on the first. Try to use a denied tool on turn 3 after allowing context on turn 1.
Pass: denied on every turn; the hook log shows every call.

**E7. Cost of a long conversation.** Ten turns with realistic tool use, measuring input and output tokens per turn through
the broker, with and without the upstream's prompt caching, and with the broker's `max_tokens` reservation.
Pass: we can state the cost curve and set a sane default conversation budget; the budget stops the conversation cleanly.

**E8. Lifetime, identity and cleanup.** A conversation needs a stable workspace owned by one session uid. Measure what a
conversation leaves behind (transcript and cache sizes), hold one uid across turns, and clean up with the mechanism
already proposed (a short cleanup session as the same uid). Confirm the leaked-workspace bug is fixed this way.
Pass: after close, the workspace and uid are released with no operator action; a held uid for an idle conversation is
bounded (decide an idle limit and what happens at it).

## 4. Questions the spike must answer in writing

1. Turn by turn, or streaming? One-sentence reason, with the evidence.
2. How long does a conversation hold a session uid, given 1000 uids and idle threads? (Candidate: hold while a turn runs
   or within N minutes, then park the thread and release the uid, with the workspace handed to a fresh uid by a cleanup
   and restore step. Evaluate whether that is needed at all.)
3. Does the broker's access-code-per-session model need to change for conversations, or is a code per turn enough?
4. What is the exact plan schema PE should demand, and which checks does the catalogue need that it lacks?
5. What does a conversation cost, and what default budget should it get?

## 5. Deliverables

- `scripts/spikes/console_p0_*.py`: runnable, documented, disposable; safe to delete.
- `docs/designs/v4-console-p0-findings.md`: the answers to section 4, the measured numbers, and a recommended P1 design
  (turn model, event mapping, schema, uid policy, budget default), with every failure or surprise listed.
- A go or no-go for P1, and, if no-go, the fallback (a custom thin runtime instead of the vendor CLI).

## 6. Rules for the spike

- Throwaway code is not held to the production bar, but it runs only on the disposable VM or inside the live host's
  contained launcher. It never gets a path around the launcher, the broker or the hooks.
- Any experiment that touches the real model key uses the broker only. Output is redacted by value before it is shown.
- Live-host runs: Propose tier, read-only, no changes to the host, hard token cap, after the VM passes. I ask before each.
- If an experiment shows the containment or broker model has a gap, it is recorded as a finding and fixed through the
  normal second-review path, not patched inside the spike.

## 7. Prerequisites and ordering

1. Start the test VM; confirm the privileged suite still passes on the current tag (baseline).
2. E1 and E2 first, because every other experiment depends on resume and live parsing.
3. E6 and E4 next (they can invalidate the approach), then E3, E5, E7, E8.
4. The leaked-workspace fix is part of E8 and is the first piece of production code the spike earns; it goes through the
   second review on its own commit.

Estimate: a small number of working sessions. It is cheap on purpose; its job is to stop us building P1 on a guess.
