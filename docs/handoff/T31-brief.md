# Task T31 — approval cards and the execution screen (last piece of landing 1c)

Branch `v2`. Python 3.11+, vanilla JS, no new dependencies. Web side only: do NOT change
`casa_farnsworth.py`, `casa_bender.py`, `casa_leela.py` or `planet_express/**` unless a finding forces
it (then say so explicitly).

## Why
Landing 1c was marked complete but its dashboard half was never built (see "Finish landing 1c" in
`docs/designs/planet-express-2-0-slices.md`). T29 landed the core reads, T30 the container page and
restart sheet. T31 is the remaining two screens from
`docs/designs/planet_express_design_v20/system/ACTION-SCREENS.md`: **C · Flight authorisation
(approval card)** and **D · Flight recorder (execution)**. After this, an operator can authorise a
proposal and watch it run without Telegram.

## Read first
- `ACTION-SCREENS.md` sections C and D (states and treatments) and the cross-cutting rules at the top.
- `casa_scruffy.py`: `authenticate()` (`g.operator`, CSRF from `request.form`, `/api/*` → JSON 401),
  `core(method, params)`, the chat routes, and T30's container routes + `/executions/<id>` placeholder.
- `static/container.js` — the house style: one state object, `textContent` only, visibility-gated
  polling with in-flight guards, CSRF from `<meta name="csrf-token">`, POST bodies for anything large.
- `templates/container.html` — page shell conventions (head assets, cockpit classes).
- `static/cockpit.css` §16 classes: `.pe-verdict*`, `.pe-sheet*`, `.pe-steps`, `.pe-step*`,
  `.pe-logwell*`, plus T30's additions at the end of the file.
- RPC shapes in `planet_express/integrations/rpc.py`:
  - `proposal.list_pending {}` → list of pending approvals (`target` decoded, no `message_id`)
  - `approval.get {approval_id}` → approval + `executions` (newest first) + `capabilities`
  - `approval.list_recent {limit}` → resolved approvals (1-20) each with its latest `execution`
  - `approval.decide {approval_id, approve, decided_by}` → `DecideResult(outcome, message, execution_id)`
    where outcome ∈ `started|denied|refused|busy|already_decided|expired|unknown`
  - `execution.get_status {execution_id}` → execution row + `capabilities` + `approval` summary
- `planet_express/application/command_service.py` `decide()` — especially the T24 re-check that
  refuses (`outcome == "refused"`) when current policy forbids the action, and the busy path.
- `tests/test_scruffy_routes.py` — fixtures, `csrf(client)` (it issues its own request: clear
  `rpc.calls` after calling it), `MultiDict` for repeated form fields.

## Required behaviour

### 1. Approvals on the Actions tab
- Replace the "Actions currently execute through Telegram" banner with live content; keep the
  deployment manifest and rollback-candidate panels as they are.
- Pending approvals render as cards (`pending` state): status row + plan id, title
  (`Restart <stack>/<service>`), who proposed it (`requested_via` / `requested_by`), risk readout
  (`BLAST RADIUS` = the target, `REVERSIBLE` = from `capabilities.rollbackable`), a countdown to
  `expires_at` (`mm:ss`, **paused while a decision is submitting**), and `DENY` + `AUTHORISE`.
- Resolved cards from `approval.list_recent` keep attribution: who decided, when, and for denials the
  reason verbatim. `expired` renders dashed with `RE-PROPOSE` (which just links to the container page —
  do not invent a re-propose RPC).
- `empty` state: no pending approvals → name the last thing that happened (newest recent approval) and
  link to it, per the design's rule 6.
- The Actions tab lives inside `#dashboard-live`, which is replaced every 60s. Either mount the
  approvals UI outside it (like the chat panel) or make it re-bind after a refresh — state (a running
  countdown, a submit in flight) must not be lost or duplicated. Say which you chose and why.

### 2. Decide endpoint
- `POST /api/approvals/<approval_id>/decide` — form-encoded `csrf_token` and `approve` (`"1"`/`"0"`),
  `approval_id` matching `[0-9a-f]{12}` else 404 JSON. Calls `approval.decide` with
  `decided_by=g.operator` — **never** an operator from the form.
- Map outcomes to JSON the UI renders in place: `started` → include `execution_id` so the card can
  offer `WATCH EXECUTION` (and the page may navigate straight to `/executions/<id>`); `denied` → the
  resolved card; `busy` → the message, card stays pending (this is the mutation lock; it is normal);
  `refused` → the message (policy changed since the card was created — T24); `already_decided`,
  `expired`, `unknown` → refresh that card from `approval.get`.
- `GET /api/approvals` (pending + recent, one call) and `GET /api/approvals/<id>` for a single card.

### 3. Execution screen `/executions/<execution_id>`
- Replace T30's placeholder template with the real screen, polling
  `GET /api/executions/<execution_id>` (which calls `execution.get_status`) every 2s while visible,
  stopping once the status is terminal.
- States: `running` (amber, elapsed, the step rail — a restart is one step, so render one step plus the
  verification phase), `verifying` (cyan; "Commands finished. The run isn't done until the service
  answers."), `passed` (green orb, `VERIFIED GOOD`, the verifier's `reason` e.g. "healthy for 15s",
  run summary with `AUTHORISED BY` from the `approval` summary, `DONE`), `failed` (red, the failure
  `reason` verbatim), `interrupted` (orange; `WHAT WE KNOW`; primary `RE-SCAN THE HOST` → link to the
  dashboard, plus `RE-PROPOSE` → the container page), `empty`/unknown id → 404 JSON / a dashed card.
- **Render only the controls the action declares** (`capabilities`): restart declares
  `abortable=False`, `rollbackable=False`, `resumable=False`, so ABORT / ROLL BACK / RESUME must never
  appear. `DONE` links back to the container page.

## Tests
`tests/test_scruffy_routes.py` (fake `rpc_call`, authenticated client):
- `/api/approvals` returns pending + recent; `/api/approvals/<id>` maps `not_found` → 404 JSON,
  `RpcError` → 503 JSON (never the airlock HTML).
- decide: without CSRF → 400; `decided_by` always from the device token even when the form sends an
  operator; `approve=0` sends `approve: False`; every outcome mapped, including `busy` and `refused`.
- `/api/executions/<id>`: passthrough, `not_found` → 404, capabilities present.
- the execution page never renders abort/rollback/resume markup for a restart (assert on the HTML).
- the approvals UI survives a simulated 60s refresh (assert the chosen mounting: either the markup is
  outside `#dashboard-live`, or the JS re-binds — test whichever you implemented).
Keep `tests/test_dashboard_data.py` green.

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (full suite; **1085 tests pass today** — a
  sandbox that blocks AF_UNIX fails ~38 RPC socket tests: report that, never skip or fake them)
- `.venv/bin/ruff check .` and `node --check static/<new>.js`
- Then the VM rehearsal below.

## VM rehearsal (required before landing)
The test homelab is a throwaway KVM guest: `tests/homelab/vm.sh up|provision|push|ssh`. A worked
end-to-end pattern — provision a temporary operator, log in with passphrase + TOTP over curl, drive the
endpoints, then restore the VM's own operator list — is in the T30 entry of the plan and was scripted
as `t30-rehearsal.sh`; rebuild the same shape for T31:
1. Create a proposal that needs approval: on the VM, `venv/bin/python -c` calling `CommandService.propose`
   is awkward; easiest is Telegram-free — use the RPC directly as the web user
   (`planet_express.integrations.rpc.call(config.RPC_SOCKET, "proposal.create", {...})`) or run
   `/restart` through the test bot.
2. Confirm the card renders pending, then `AUTHORISE` it from the dashboard and watch
   `/executions/<id>` reach `passed`, with the container's `StartedAt` actually moving.
3. Confirm a `DENY` records the reason and the card shows attribution.
4. Confirm core refuses cleanly while a scan holds the lock ("Busy right now (scan running)") — that is
   correct behaviour, not a bug.

## Must NOT change
Auth/login behaviour, chat, the container page's polling, core mutating paths, `SCHEMA_VERSION`.
Legacy Farnsworth LLM shell plans are NOT part of this screen — they keep approving in Telegram until
slice 5b retires them. Do not add an approval path for them here.
