# TODOS

## Dashboard

### "Load earlier" log paging in the container panel

**What:** Let the dashboard's container log panel page backward past the "earlier lines skipped" marker.

**Why:** A noisy container that writes more than 500 lines (or 256 KiB) between the panel's 3s polls hits the cap; today you see the gap marker but must SSH in and run `docker logs` to read what was skipped, which is exactly what you'd want from your phone mid-incident.

**Context:** Deferred during `/plan-eng-review` (2026-09-13, decision D1) to keep landing 1c small; the skipped-lines marker keeps gaps visible so nothing is silently lost. Spec already worked out in the spec review of `docs/designs/planet-express-2-0-slices.md`: fetch `docker logs --timestamps --since <pre-gap cursor> --until <first returned ts> --tail 500`, dedup both edges by content hash, repeat until `skipped` is false. Needs an `until` parameter on the RPC `logs.tail` method (`planet_express/integrations/rpc.py`), a "load earlier" control in `static/dashboard.js`, and tests in `test_actions.py` for edge dedup and multi-page gaps. Start by reproducing a gap on the test homelab's `crash-loop` fixture.

**Effort:** M
**Priority:** P3
**Depends on:** Landing 1c (dashboard RPC + container panel)

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

## Completed
