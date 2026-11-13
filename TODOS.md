# TODOS

## Fleet

### PE knows every Docker host, not only its own (design first)

**What:** Today Leela, Hermes, the container count, Network, repairs and updates see only the local Docker socket; the other hosts are
read-only metrics and container lists from Beszel on the Hosts tab, and nothing else in PE knows they exist (a crashed container on
another host is never a finding). Design in `docs/designs/multi-host.md`: how PE reaches each host, one inventory, one health picture,
and where acting on another host would draw its lines.

**Effort:** L (design first, then one level per release)
**Priority:** P1

### `lan_only_domain` defaults to the author's domain

**What:** `config_schema.lan_only_domain` defaults to `casalan.com` for configs written before the field existed (the
author's host relies on it). A new install that does not set it inherits the author's domain (`/install`'s LAN-only check,
the topbar since rc14.12). Make new installs set it (the wizard asks; `config.example.yaml` shows it), then move the default
off the author's domain once the author's config sets it explicitly.

**Effort:** S
**Priority:** P2

### Reconcile the Missions and Memory tabs (pre-Console and post-Console overlap)

**What:** Missions and Memory grew in two eras: before the Console (missions created from incidents and the Work log, memory
cards and candidates) and after it (conversations, plans, runs, lessons offered from a conversation). The two tabs now overlap:
a Console run is both a conversation and a Repair mission; lessons and memory cards are two shapes of one idea; NEEDS YOU and
the Console's "Needs you" group answer the same question. Map what each tab shows, where each object lives, and decide one
home per concept (operator, live 2026-11-13: "the bulk of work left").

**Effort:** M (design first)
**Priority:** P1

### A log check that cannot read its whole window holds a good run

**What:** `log_absent` gives up when the window is over 64 KB ("absence cannot be shown") and the run is held for the operator,
though nothing was found (live M-39: Immich 3.3.6 to 3.4.1, migrations clean, 79 KB of first-start logs). Read the window in
bounded chunks (streaming the matcher) so a chatty upgrade can still be proven clean; keep "unknown" only when the logs truly
cannot be read.

**Effort:** S
**Priority:** P1

### A plan's "will not touch" must account for dependents

**What:** M-39's plan said it would not touch `immich-power-tools`, but stopping the database restarted it (a Compose
dependent). When an option stops or recreates a service, the planner (and PE's own check of the plan) should name the services
that depend on it, so "will not touch" is true or the dependent is listed under changes.

**Effort:** S
**Priority:** P2

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

### Surface real usage from the provider (done, rc14.7 to rc14.8.2)

Account credit and spend in the Console and on Missions with Telegram alerts (rc14.7); PE's own model calls through the broker, one
key and one record (rc14.8, live on the host); cost per conversation and per mission from the broker's records (rc14.8.2). Left out on
purpose: an Anthropic price-table estimate for an install on the Anthropic API without an Admin key (the host uses OpenRouter, which
prices every call). Operator step: remove `OPENAI_API_KEY` from `/etc/planetexpress.env`; nothing has used it since rc14.8.

### State machines made explicit (done, rc14.7.4 to rc14.7.10)

`docs/designs/state-machines.md`: 35 machines mapped, the ones that mattered given one table each. Two items need a schema change and
were left: a "dismissed" incident kind (V9) and the Console's consumed-turn marker (C4).

### Obsolete

- Chat spend ceiling: the dashboard Chat is gone (rc14.7.3).
- Collapse the provider setup in casa_amy: Amy and Fry share one research call (`casa_llm.research`, rc14.8).

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
