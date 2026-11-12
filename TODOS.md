# TODOS

## Dashboard

### "Load earlier" log paging in the container panel

**What:** Let the dashboard's container log panel page backward past the "earlier lines skipped" marker.

**Why:** A noisy container that writes more than 500 lines (or 256 KiB) between the panel's 3s polls hits the cap; today you see the gap marker but must SSH in and run `docker logs` to read what was skipped, which is exactly what you'd want from your phone mid-incident.

**Context:** Deferred during `/plan-eng-review` (2026-09-13, decision D1) to keep landing 1c small; the skipped-lines marker keeps gaps visible so nothing is silently lost. Spec already worked out in the spec review of `docs/designs/planet-express-2-0-slices.md`: fetch `docker logs --timestamps --since <pre-gap cursor> --until <first returned ts> --tail 500`, dedup both edges by content hash, repeat until `skipped` is false. Needs an `until` parameter on the RPC `logs.tail` method (`planet_express/integrations/rpc.py`), a "load earlier" control in `static/dashboard.js`, and tests in `test_actions.py` for edge dedup and multi-page gaps. Start by reproducing a gap on the test homelab's `crash-loop` fixture.

**Effort:** M
**Priority:** P3
**Depends on:** Landing 1c (dashboard RPC + container panel)

### Retire Core's chat service (P3)

**What:** rc14.7.3 removed the dashboard Chat tab and its `/api/chat*` routes, so Core's `ChatService` and its `chat.ask`,
`chat.get` and `chat.quota` RPC methods have no caller left. Remove them, the chat tables' writers, and the chat-only
investigation path, after checking nothing in Telegram reaches them. `casa_bender.diagnostic_label()` (it named the Chat tab's evidence
cards) has no caller left either; it goes with them, under the second-review gate. The chat spend-ceiling item below goes with it.

**Why:** dead code on the RPC surface is surface a reviewer still has to check.

### Chat spend ceiling: count cost, not calls

**What:** Record per-call token usage and make the daily chat ceiling a cost budget instead of a
call count.

**Why:** Calls are a poor proxy for money. One long investigation with five diagnostic rounds and a
large context can cost more than twenty short questions, so a call ceiling that feels safe can still
produce a surprising bill, while a cheap-but-chatty session gets throttled for no financial reason.

**Context:** Deferred during `/plan-eng-review` (2026-09-16, decision D26). D22 chose calls
deliberately — it is what the existing `MAX_DIAGNOSTIC_ROUNDS` budget counts and it needs no price
table — but required slice 2 to record enough per call to switch later, so the groundwork ships
either way and this item is only the switch. Note the shape changes: the ceiling reserves quota
transactionally in the same `BEGIN IMMEDIATE` that records the call, so a spend ceiling means
reserving an *estimated* cost and reconciling against actual usage afterwards. Needs per-provider
usage parsing (Anthropic `usage`, OpenAI `response.usage`) and a price table that goes stale on every
repricing. Revisit once there is real usage data showing whether the call ceiling was ever the
binding constraint.

**Effort:** M
**Priority:** P3
**Depends on:** T21 (slice 2 chat with the call-based ceiling)

### Collapse the provider client-setup duplication in casa_amy

**What:** After the shared tool loop is extracted, `casa_amy.py` still branches per provider in
`_ask_anthropic` (`:84`) and `_ask_openai` (`:104`) — two functions that build a client, set model and
effort, attach web-search tools, and pull text out of the response.

**Why:** It is the last provider fork once the loop extraction lands. A new model tier, a key
rotation, an SDK bump, or slice 6's openai-compatible endpoint has to be made in both, and they
already differ in shape: Anthropic streams with adaptive thinking plus `web_fetch`, OpenAI does one
non-streaming call with `web_search` only.

**Context:** Accepted residue of `/plan-eng-review` decision D10 (2026-09-16), not an oversight. D10
deliberately excluded Amy from the loop extraction because her search runs provider-side with no
client-side tool loop, so this is a different abstraction (client construction) and merging the two
was rejected. ~30 lines that change rarely. The trigger is slice 6's openai-compatible provider:
that is when two branches become three and the fork starts costing something. Do not fold this into
the loop extraction.

**Effort:** S
**Priority:** P3
**Depends on:** T20 (loop extraction); realistically slice 6

## Core

### Redaction: recover innocent same-line content and close the key-shape gaps

**What:** Make `planet_express/core/redact.py` keep non-secret content after a redacted key on the same
line, and recognise the key shapes it currently misses.

**Why:** T18 shipped a deliberately conservative filter. After the first sensitive key it withholds the
rest of the line, so `HOST=h API_KEY=x PORT=80` loses `PORT=80` from logs and planner evidence. It also
misses run-together keys outside its compound list (`MYKEY=`), keys longer than 128 characters or more
than 32 spaces from their value, and it over-redacts camelCase names such as `keyId` and `tokenCount`.

**Context:** Accepted residue of T18 (2026-09-16), not an oversight. Every earlier attempt to find where a
value ends leaked, and 14 review rounds are recorded in the T18 plan entry. Any change here must keep
passing `tests/test_redact.py` in full, including the regression test for every review finding and the
`test_no_superlinear_path` guards. Only start if log/evidence context loss is actually hurting
diagnosis. Configured literal secrets are already redacted wherever they appear, whatever the key.

**Effort:** M
**Priority:** P3
**Depends on:** T18

### redact() costs ~4µs per character, which bounds every log read

**What:** Speed up `planet_express/core/redact.py`'s key scan so redacting a few MB of log text is not
seconds of CPU.

**Why:** `_ASSIGNMENT_RE` is `([A-Za-z0-9_]{1,128})["']{0,2}[ \t]{0,32}[:=]`, so on text with no
separators the engine matches up to 128 identifier characters at **every** position and backtracks —
measured 30ms per 8 KiB record, 15.1s for 500 such records (T29). T29 works around it by redacting
newest-first under the caller's deadline and marking the gap, so a container writing long records
returns fewer lines than it could.

**Context:** The bounds are what made redact linear at all (T18 review rounds 6-7 found three
quadratic paths), so this is not "remove the bounds". The likely shape is scanning for separators
(`[:=]`) and walking back a bounded identifier, which is O(separators × 128) instead of
O(characters × 128) — the T18 gate rejected an UNBOUNDED backward walk, not a bounded one. Any change
must keep `tests/test_redact.py` green in full: every leak class from 14 review rounds plus the seven
`test_no_superlinear_path` guards. Add a perf test asserting a 4 MB body redacts in well under a
second before changing the matcher.

**Effort:** M
**Priority:** P3
**Depends on:** nothing (T29 ships the workaround)

## Install (`/install`, Fry and Farnsworth)

### Map an upstream project's relative bind mounts under `~/apps/<stack>`

**What:** Farnsworth refuses any install whose volumes are not named volumes (live rc14.8.1: Uptime Kuma, `./data:/app/data`),
so in practice `/install` refuses nearly every real project. Map a relative host path (`./x`, `x`) to `~/apps/<stack>/x` (the
installer already refuses when that directory exists) and show the mapped path on the proposal card. Keep refusing absolute,
`~`, `$`/`${...}` and `..` paths (the docker socket, `/opt/...`, anything outside the stack's own folder). Also escape the literal
`` `~/apps/<stack>` `` in the installer's refusal messages: Telegram reads them as HTML and the send falls back to plain text.
Second review (Codex + `/code-review`): this is Farnsworth's planning logic.

**Effort:** S
**Priority:** P3

## Missions (trust mode, from live use after rc14.4)

### Say why a mission action was refused on the mission page

**What:** The mission page answers every supervisor refusal with "That could not be done now. The event stream says why.", but most
refusals are not in the event stream (live: a second Check again while the first was running). Show the refusal's own words.

**Effort:** S
**Priority:** P2

### Surface real usage from the provider so the operator decides

**Status (rc14.7):** account level done: credit and spend in the Console and on Missions, Telegram alerts, per-call `cost_usd` on the
broker's events when OpenRouter reports it (confirm on a live request). Still open: per-conversation and per-mission cost shown from
those events and the Anthropic price-table estimate without an Admin key. v3's model calls go through the broker whenever it runs (rc14.8); retire the OpenAI key once that is confirmed on the host.

**What:** Replace guessed limits with the provider's own figures, shown where the operator works (Console header, Missions page,
Telegram on request), so they decide when to stop, not a cap:
- **API key through a gateway (OpenRouter, the host today):** `GET /api/v1/key` with the key the broker already holds: `limit`,
  `limit_remaining`, `limit_reset`, `usage` (all-time) and daily/weekly/monthly usage. Show "credit left" and "spent today / this
  month". The broker can also ask OpenRouter for per-request cost (usage accounting) and record it on its per-call event, so each
  conversation and mission shows what it cost in money, not only tokens.
- **Anthropic API directly:** spend from the Usage and Cost API, which needs an Admin API key (a separate, optional credential held
  by Core like the model key; never in a session). Without one, show tokens and an estimate from a configured price table, labelled
  as an estimate.
- **Subscription:** no public API for usage against plan limits was found, and the broker refuses a subscription login by design.
  Out of scope until there is a source of truth; say so on the page rather than guess.
- Per conversation and per mission: tokens and cost (from the broker's records), next to the account-level figures.
- Optional alerting the operator configures (for example "tell me on Telegram when credit left drops under $5"): information, never
  a stop.

**Why:** Fixed per-conversation budgets did not match real use (live M-31/M-32). The operator should see what is being spent and
what is left at the provider, and decide.

**Context:** `missions/model_broker.py` (it holds the key and sees every call: the place to fetch and record; any change gets the
second review), `console/api.py` and `static/console.js` (the header), the Missions page, `config_schema.py` (`missions.model_broker`).
Verify OpenRouter's `/api/v1/key` fields and usage accounting against the live key before building.

**Includes:** route v3's model calls (Hermes and Farnsworth through `casa_llm.py`, Amy through `casa_amy.py`) through the model
broker too. Today they call the provider directly with their own key, so v3's spend is in no record and any usage figure would leave
it out. One key, one record, one honest number.

**Effort:** M
**Priority:** P1

### State machines: find the implicit ones and make the ones that matter explicit (after rc14.7)

**What:** Inventory every implicit state machine (status strings compared in several places, timestamps doubling as state, flag
combinations, in-memory registries, step sequences) into `docs/designs/state-machines.md`: states, transitions, writers, readers,
storage, risk. Then per machine: make it explicit (the `missions/state.py` pattern: a transition table, one guard, one
state-to-label/actions table every surface reads), fold it into an existing machine, or leave it with a recorded reason. Stored values
and schemas do not change; legacy values map onto the table. One machine per release, characterization tests first, a replay of the
live host's events to prove nothing changed, the agent's brief told its state and allowed next moves from the same table. Order:
Console (conversation, plan, execution), checkpoint and rollback/undo, leases and approvals; the v3 `PipelineState` lock is mapped,
not changed, unless something is wrong.

**Why:** One word and one set of actions per state on every surface (UX), one file that says what can happen next (DX), and an
agent that is told its state instead of guessing (AX).

**Effort:** L (about half a day to map, then one or two days per machine)
**Priority:** P2

### One planner and one investigator: v3's Farnsworth planner and Amy hand off to the Console

**What:** A finding with a typed runbook keeps the v3 fast path. A finding without one, or a runbook that failed, opens a Console
conversation (the incident attached) instead of asking Farnsworth's LLM planner or Amy. The Console plans with PE's check catalogue,
the checkpoint and Undo; Amy's web search becomes a tool the Console agent has.

**Why:** Two planners and two diagnosticians, each with its own prompts and output shape, to keep correct. One way to reason about a
problem is simpler for the operator, the code and the agent.

**Context:** `casa_farnsworth.py` (`_devise_typed_plans`, `_propose_compose_runbook`), `casa_amy.py`, `console/service.py` (incident
origin). Part of the state-machine work: it is where the two flows meet.

**Effort:** M
**Priority:** P2

### One way approval and undo look, whichever path did the work

**What:** v3 runbooks (approval card, engine step rollback) and v4 missions (hash-bound grant, checkpoint restore) keep their
mechanisms, but present the same: the same words for approve, running, done, undo; the same Undo button; the same record view, on the
dashboard and Telegram.

**Why:** The operator, and the agent, learn one model of "approve, watch, undo" instead of two.

**Context:** The state-machine label/actions tables are the place to do it. Approval and rollback code stays under the second-review
gate.

**Effort:** M
**Priority:** P2

### Accept plain text for `known` and `unsure`

**What:** `lenient()` turns a string `known` / `unsure` into a one-item list instead of refusing the plan.

**Why:** A live plan was refused for exactly this and needed an automatic retry.

**Effort:** S
**Priority:** P2

### "Close anyway" when PE holds a cancel

**Status (rc14.7):** partly done: a run held after PE's checks failed has Check again and Keep the changes and close. A held CANCEL
(the run's changes differ from the checkpoint, no failed check) still has only Roll back.

**What:** When a cancel is held (the run's changes still differ from the checkpoint), the Missions page and Console say why and
offer an explicit "Close anyway" (operator-only, recorded), instead of a disabled "CANCEL REQUESTED" button.

**Why:** A held cancel left the operator with no way to close the mission without a hand edit to the database.

**Effort:** S
**Priority:** P2

### Hide closed missions by default

**What:** The Missions page hides terminal missions behind a toggle (on by default). Decide separately whether test history is
ever deleted.

**Effort:** S
**Priority:** P3

### An agent's main reply is lost when it replies twice in one turn

**What:** When the agent writes its reply, then finishes a background task and replies again, only the last reply is stored.
Keep both (or the longer one) in the conversation.

**Why:** A live investigation's full diagnosis was missing from the Console; only its short follow-up was shown.

**Context:** `console/stream.py` (assistant messages vs closing words), `console/service.py` turn end.

**Effort:** S
**Priority:** P2

### Cancel and crash checks ignore run state

**What:** `recovery.matches_checkpoint` compares facts but not running/stopped, while restore does. A session that only stopped
a service can be classified "nothing changed".

**Context:** Low finding from the rc14.1 review; unchanged since.

**Effort:** S
**Priority:** P3

### Finished conversations hold a session uid until archived

**What:** Since rc14.4 a finished conversation no longer counts towards `max_open`, but it still holds its reserved uid and
state directory. Auto-archive after N days, or release the uid when the mission ends.

**Effort:** S
**Priority:** P3

### Ops: NFS subdirectory binds go stale

**What:** Containers that bind an NFS *subdirectory* pin its file handle at start and get ESTALE when the server stops honouring
it; binding the share root avoids it. Move the remaining subdirectory binds on the host to the share root (one Console request
each).

**Effort:** S
**Priority:** P2

## Completed

### Undo covers every file the plan changes (done, rc14.5)

A confirmed option lists its `files`; they are hash-bound in the grant, shown on the Authorise sheet, and captured by the checkpoint
wherever they are on the host, so Undo puts them back. A file the checkpoint cannot cover (absent, unwritable directory, PE's own)
does not stop the run. The result card names it, along with every file the hook saw the run write outside the checkpoint and the
number of calls it could not read for writes.

### A conversation's token budget must not stop the work (done, rc14.6)

The per-conversation `token_budget` is a usage mark: the page shows how big the conversation has grown and flags it past the mark,
and nothing refuses a message, a turn or PE's plan retry because of it; no per-turn cap is set from it. The broker's own per-session
ceilings (requests, tokens) stay the runaway-cost backstop. The default mark is 500k. `min_turn_tokens` is accepted but unused.
Context compaction for long conversations stays with the CLI (`--resume` auto-compacts); revisit if cost per turn becomes a problem.
