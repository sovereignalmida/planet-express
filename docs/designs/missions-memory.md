# Missions and Memory after the Console: one home per concept (design, 2026-11-13)

Status: proposal for the operator's review. Release 1 (NEEDS YOU counts only what needs you) ships as 4.0.1 alongside this
doc; nothing else here is built. No database schema change anywhere: every change is a view over the tables that exist.

## 1. The problem

Missions and Memory grew in two eras. Before the Console, a mission was the unit of work: created from an incident's
INVESTIGATE or the Work log, decided on its own page or over Telegram, its lessons reviewed in Memory. Since the Console,
the unit of work is a conversation, and each run it starts is a Repair mission underneath. Both eras still have their
surfaces, so the same thing appears in two or three places with different words and different buttons.

Operator, live: "the bulk of work left is reconciling the two tabs in Missions and Memory as there are overlaps from before
we had the console and after", and "I have things [in] Needs You that don't [need me]".

Live counts (2026-11-13): 40 missions: 23 Console conversations (observe, runtime `console`), 16 runs and Work-log repairs
(repair, runtime `command`), 1 pre-Console propose mission. 12 memory cards (4 candidates, 8 confirmed), 4 of them Console
lessons. 0 standing grants. 0 open incidents.

## 2. Inventory: where each thing is shown and acted on today

(S = `casa_scruffy.py`, CA = `planet_express/console/api.py`, MS = `planet_express/application/mission_service.py`,
CT = `planet_express/console/telegram.py`, F = `casa_farnsworth.py`.)

| Object | Shown at | Acted at | Overlap |
|---|---|---|---|
| **Console conversation** (an observe mission, runtime `console`) | Console rail and thread (CA:113, 123); **Missions board** as an ordinary card; `/missions/<id>` (facts and events); Telegram `/threads`, `/missions`, `/mission` | Console: post, stop, archive, scrap plan; **Missions: CANCEL** (S:1263); Telegram `/reply`, `/plan`, `/close`, **`/cancel_mission`** | Two boards, two ways to close it; the Console then says "closed from the Work log" (CA:69) |
| **Run** of a confirmed plan (a Repair mission + `console_executions` row) | Console execution card (CA:483); **Missions board**; **`/missions/<id>` with its own approval card**; NEEDS YOU; Telegram `/missions`, `/mission`, `/mission_card` | Console: authorise, withdraw, roll back, check again, keep (CA:299-411); **Missions page: APPROVE, DENY, CHECK AGAIN, KEEP, ROLL BACK, CANCEL** (S:1192-1214); Telegram `/authorise`, `/withdraw`, `/run` **and** `/approve`, `/deny`, `/rollback M-<n>` | Every action has two buttons on two pages and two Telegram commands; the Console has code to adopt a decision made "in the Work log" (CA:385, 459) |
| **Pre-Console mission** (`mission.create`, `mission.from_incident`) | Missions board, `/missions/<id>` | Created only by INVESTIGATE on an incident, and only while the Console is off (with it on, the button is ASK THE CONSOLE, incidents.js:70); `mission.create` has no UI caller | A path the Console replaced |
| **v3 approval** (Telegram `/restart`, `/update`, `/install`, incident PROPOSE RESTART, dashboard container actions) and its **execution** | NEEDS YOU › FLIGHT AUTHORISATION; `/executions/<id>`; Telegram cards; Overview HULL "PLANS" | Approve/deny (S:767; F:2082); abort, roll back (S:836) | A second approval system beside the Console's; outside the record entirely |
| **Canary rollback window** | NEEDS YOU › OPEN ROLLBACK CANDIDATES | SETTLE (elevated, S:844) | Counted in NEEDS YOU (correct: a decision) |
| **Update history** (Zoidberg) | **Memory › DEPLOYMENTS** (reads `state/update_history.json`) | none (read-only) | Changes to the host, filed under memory |
| **Incident** | NEEDS YOU › INCIDENT CONSOLE only | PROPOSE RESTART (makes a v3 approval), DISMISS, INVESTIGATE / ASK THE CONSOLE | Fine; not a decision, rightly not counted |
| **Memory card** (candidate, confirmed, review due) and **Console lesson** | Memory tab (three lists); Console thread lessons block (CA:232) | Memory: CONFIRM, RE-VERIFY, DEPRECATE (S:1310); **Console: keep / discard the same candidate** (CA:247) | One card decided in two places; whichever acts second gets "already decided" |
| **Standing grant** | Missions › STANDING GRANTS | CREATE (elevated), REVOKE | **No production caller attaches one to a mission** (the Console never passes one; `start_under_standing` has no caller): a feature with no effect |
| **"Needs you" counts** | NEEDS YOU badge (S:1090); Console rail group (console.js:92); Overview HULL (findings + v3 plans only) | — | Three counts from three rules; the badge counted answered conversations (fixed in 4.0.1) |

## 3. Principles

1. **One home per concept**, and every other surface links to it instead of repeating its buttons.
2. **NEEDS YOU means you must act.** Not "an agent finished talking", not "something happened". Every item names the
   decision and links to where you make it. One rule computes it, and every count (badge, Console rail, Overview) reads that
   rule.
3. **The record is everything that changed or was looked into**, whatever started it: a conversation, Telegram, an incident,
   Zoidberg, a safe prune, a v3 runbook. One row per piece of work, newest first.
4. **Memory is what PE knows**, nothing else.
5. **The Console is where you work.** It does not change in this design, except that it stops being contradicted.

## 4. The target

| Tab | Holds | Changes from today |
|---|---|---|
| **Console** | Conversations, plans, runs, Undo, lessons offered | None. Its rail's "Needs you" group stays, from the same rule as NEEDS YOU |
| **Missions › NEEDS YOU** | The inbox: a plan to choose or authorise (Console), a v3 plan to approve, a run held for you, a recovery to choose, a held rollback window. Below it, not counted: what is wrong (incidents, hull findings) | Already the right place (operator's choice). 4.0.1 makes its count real. Release 3 makes it the only source for every count |
| **Missions › RECORD** (replaces the status board) | A time-ordered list: one row per conversation (its runs nested under it, with outcome and cost), per v3 execution (approved runbooks, canary updates, safe prunes), per remaining pre-Console mission. Filters: outcome (completed, rolled back, held, cancelled, failed) and origin (Console, Telegram, incident, automatic). Each row links to where the work lives: the conversation, the execution page, or the mission page | Board columns go; Deployments move here as rows; v3 executions appear for the first time |
| **Mission page** `/missions/<id>` | A detail view reached from the record: facts, events, artifacts, cost | For a mission a conversation owns, its action buttons go: the page says "decided in the Console" and links there. Missions with no conversation keep their buttons |
| **Memory** | Confirmed cards, cards due for review, and candidates as one queue; a candidate from a conversation says so and links to it | DEPLOYMENTS sub-tab goes to the record. A candidate is decided in one place per origin (see 6) |
| **Telegram** | Mirrors the above | For a Console run, `/authorise`, `/withdraw`, `/run` are the commands; `/approve`, `/deny`, `/rollback M-<n>` answer "decide it in the conversation" for a run a conversation owns, and keep working for other missions |

**Retired:**

- **Standing grants** (no effect today: nothing attaches one). Removed: the page, the RPCs' UI, the unused attach path. The
  table stays (no schema change).
- **INVESTIGATE making a pre-Console mission.** With the Console on, it already opens a conversation; with it off, the button
  is hidden, as today. `mission.from_incident` stays as an RPC for Telegram-less installs only if something still calls it;
  otherwise removed.
- **The status board.** Replaced by the record.

## 5. NEEDS YOU, the rule (release 1, 4.0.1, built)

A mission is a decision when its status is `awaiting_decision`, `needs_operator`, `recovery_required` or `paused_tripwire`,
with two exceptions. A conversation's own mission is judged by the Console when the Console lists it (a pending plan to
choose or authorise, a run held for you, a turn that did not finish), and in `awaiting_decision` it is never a decision (it
means "the agent answered; your turn to reply"). A run a conversation owns is dropped only when the Console lists that
conversation under "Needs you", so it is counted once, as the conversation. Otherwise a run is judged as a mission: a held run
whose conversation shows "Working" (a follow-up is running) or cannot be shown (Console off or unreadable, conversation
archived) never drops out of sight. Core's mission list now says which conversation owns each mission (`conversation_id`, from `console_conversations`
and `console_executions`; no schema change). Plus, as before: pending v3 plans and held rollback windows.

Release 3 moves this rule into Core (one RPC, `needs.list`) so the badge, the Console rail and the Overview HULL tile all read
it instead of computing their own.

## 6. Lessons and candidates

A Console lesson is a candidate card. Today it can be kept in the Console or confirmed in Memory, and the loser of the race
gets "already decided". Target: the Memory queue shows every candidate; one from a conversation reads "offered in <title>"
and links there; both buttons stay (they call the same store, and the conflict is already handled), but the wording matches:
**Keep** / **Discard** in both places (Memory's CONFIRM and DEPRECATE are renamed for candidates; DEPRECATE stays for
confirmed cards). A lesson from an archived conversation is reachable from Memory, as today.

## 7. With missions off

v3-only installs keep what works today: NEEDS YOU (flight authorisation, rollback windows, incidents, hull), the execution
pages, and now the record with v3 executions and deployments (which needs no mission tables). The Console, mission rows and
memory cards are absent, as today.

## 8. Releases

| Release | Content | Size |
|---|---|---|
| 1 (4.0.1, built) | NEEDS YOU counts only what needs you; Core's mission list names the owning conversation | S |
| 2 | **The record**: time-ordered list replacing the board; conversations with nested runs; v3 executions, canary updates and safe prunes as rows; deployments move in; filters; cost per row | M |
| 3 | **One place to act**: `needs.list` in Core read by every count; the mission page drops buttons for conversation-owned missions; Telegram duplicates answer "decide it in the conversation"; candidate wording and "offered in" links | M |
| 4 | **Retire**: standing grants UI, the pre-Console creation path, dead RPCs; INSTALL and README updated | S |

Each: characterization tests first, the second review, a live check on the host.

## 9. Decisions (operator, 2026-11-13)

1. **Standing grants: removed** (release 4): the page, its RPCs' UI and the dead attach path go; the table stays (no schema
   change).
2. **Questions are record rows**, filterable: "questions" (a conversation with no run) vs "changes" (anything that ran or
   could have changed the host).
3. **Telegram `/approve` and `/deny` stay** for missions no conversation owns. For a run a conversation owns they answer
   "decide it in the conversation" (release 3).
