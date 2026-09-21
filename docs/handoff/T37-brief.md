# Task T37 — slice 5a: `/up` and `/down` as typed actions

Branch `v2`. Python 3.11+, vanilla JS, no new dependencies. This touches the typed-action execution
path and Farnsworth's command handling, so the second-review gate (CLAUDE.md) applies in full.

## Why
`/up` and `/down` are the last Telegram mutations that bypass the typed-action model: `_run_stack_op`
(`casa_farnsworth.py` ~1546) takes the mutation lock and calls `casa_stackctl.stack_up/down`, which
run `docker compose` via `subprocess.run` with no approval object, no audit row, no verification and
no dashboard visibility. Slice 5a (plan D25) moves them onto typed actions "as they are". Multi-step
execution, `/install`, canary updates, `/rollback` and retiring legacy shell plans are **5b — not here**.

## Decision D33 (operator, 2026-09-21) — risk classes
| Telegram | Action | Risk | With default `direct_request_risks: [R1]` |
| --- | --- | --- | --- |
| `/up <stack>` | `compose.up_stack` | R1 | runs immediately (operator-direct), verified, audited |
| `/down <stack>` | `compose.down_stack` | R2 | approval card first |
| `/up all` | `compose.up_all` | R2 | approval card first |
| `/down all` | `compose.down_all` | R3 | approval card first |
| `/down <ingress stack>` | `compose.down_ingress` | R3 | approval card first |

"Ingress stack" = the stack named `network` or any stack whose name contains a
`NETWORK_GUARDED_SUBSTRINGS` token (traefik/adguard). Forbidden stacks: **up refused, down allowed**
(unchanged — the 2026-07-07 `ai` incident is why down must work on them). All five declare
`abortable=False, rollbackable=False, resumable=False`. The operator can loosen friction later by adding
R2/R3 to `autonomy.direct_request_risks` (a D29-sensitive field). Policy's existing rules then apply
unchanged: forbidden risks refused, non-R0 never automatic (D23/D31), T24 limits only for non-operator
origins.

## Read first
- `docs/designs/planet-express-2-0-slices.md`: slice 5 (~line 457, D25), T14 (operator-direct
  requests), T24 (policy), T31/T33 entries.
- `planet_express/execution/actions.py`: `Target`, `resolve_target`, `ActionSpec`, `REGISTRY`,
  `restart_argv`, `verify_after_restart`, `read_health`, `NETWORK_GUARDED_SUBSTRINGS`, `_NAME_RE`.
- `planet_express/execution/policy.py` (unchanged unless forced; say so if you touch it).
- `planet_express/application/command_service.py`: `propose`, `request_action`, `decide`,
  `_run_execution`, `_finish`, `reconcile_on_startup`, `_card_text`, `get_status`,
  `_execution_handoff`. Today every one of these assumes a restart of one `stack/service`.
- `planet_express/core/store.py`: `propose`, `create_direct_execution` (hardcodes
  `requested_via='dashboard-direct'`), `recent_attempts`, `interrupt_unfinished`.
- `casa_farnsworth.py`: `/up`, `/down`, `/restart` handling (~1195-1250), `_run_stack_op`, `/help` text.
- `casa_stackctl.py`: `stack_up`/`stack_down`/`all_stack_dirs`/`config.active_stack_dirs()` ordering
  (up all: `network` first; down all: `network` last). The CLI stays as it is (with its core-active
  guard); Farnsworth stops calling `stack_up/stack_down`.
- `static/approvals.js`, `static/execution.js`: titles, blast radius and links assume
  `Restart <stack>/<service>`.

## Required behaviour

### 1. Actions and targets (`actions.py`)
- Register the five actions above. Keep `docker.restart_service` exactly as it is.
- A stack target: `{"stack": name}` with key `stack:<name>`; the all-stacks target: `{"scope": "all",
  "stacks": [...]}` with key `all`. Resolution validates the name with `_NAME_RE` and no `..`, requires
  `<stacks_root>/<stack>/docker-compose.yml` to exist, refuses **up** of a forbidden stack, and maps
  down of an ingress stack to `compose.down_ingress` (a request for `compose.down_stack` on an ingress
  stack is refused with a message naming the right action — never silently downgraded to R2).
  For `all`, snapshot the ordered stack list at proposal time (up all = `config.active_stack_dirs()`,
  i.e. excluding forbidden, network first; down all = every stack dir, network last) and store it in
  the target; at execution time re-resolve and **fail without running anything** if the set changed.
- argv builders only (execution goes through `bender.run_argv`, argv, no shell):
  `["docker","compose","-f",<file>,"up","-d"]` and `[... ,"down"]`, per stack.

### 2. Verification (from container state, never the command's exit code)
- up: after `up -d`, list the project's containers (`docker compose -f <file> ps -a --format json`,
  or `ps -a -q` + `read_health`), and pass once every container is running with health
  healthy/none continuously for `VERIFY_STABLE_SECONDS`, failing fast on exited/unhealthy/restart
  count increase, bounded by a timeout (≥ 180s for a stack). A stack with zero containers after up
  fails ("no containers started").
- down: pass when `docker compose ps -a -q` for the project is empty; bounded.
- all: run stacks sequentially in the snapshotted order, verifying each before the next; stop at the
  first failure and report which stacks passed, which failed, and which were not attempted.
  Timeouts: per stack, as today's 180s for the compose command plus the verification bound.

### 3. CommandService generalisation
- Dispatch by action for: target resolution (propose/request/execute), the Telegram card text, the
  execution steps, verification, `_finish` texts ("Verified good: media up", "Down failed for …"),
  interrupted-at-startup texts, and `get_status` (return the stored target dict; add `action` and a
  human `summary`, e.g. "Bring media up", so front ends need no action-specific guessing).
- The restart path's behaviour, texts, tests and incident proposals must be byte-for-byte unchanged
  in outcome (keep existing tests green; do not rewrite them).
- Operator-direct from Telegram: add an origin `telegram-direct` (in `OPERATOR_ORIGINS` — the only
  policy edit allowed; say so) and let `request_action` / `Store.create_direct_execution` take the
  origin instead of hardcoding `dashboard-direct`. `/up <stack>` → direct request when
  `policy.allows_direct_request(R1)`; otherwise it falls back to a proposal card. R2/R3 actions →
  `propose` with `requested_via="telegram"` → card → `decide` (the existing T24 policy re-check and
  mutation lock apply unchanged).
- The mutation lock: one execution holds it for the whole run (all stacks), exactly like a restart.
  Busy → the existing "Busy right now (<reason>)" reply; never queue.
- `action.request` RPC keeps accepting `{action, stack, service, operator}` for restart; add stack
  actions with `service` absent/None. Validate exact param sets per action. The dashboard gets **no
  new buttons** in this task (stack cards' non-goal stands); it must just render these approvals and
  executions correctly.

### 4. Farnsworth
- `/up <stack|all>` and `/down <stack|all>` route to CommandService as above; remove `_run_stack_op`
  and its `stackctl` calls. Keep the usage messages. Reply immediately with what happened
  ("🛠 Bringing media up…" with the execution id, or "Approval card sent", or the refusal reason).
- Update `/help` to say which ones need approval.

### 5. Dashboard rendering (JS only)
`approvals.js` / `execution.js`: use `get_status`'s/approval's `action` + `summary` (add `summary` to
approval reads in `rpc.py` if needed — read-only change) for titles, BLAST RADIUS (stack, or "every
stack (n)"), and links (a stack target links to the Overview, not a container page). Restart cards
must look exactly as today.

## Tests
- actions: name validation, forbidden up refused / down allowed, ingress mapping and the refusal of
  `down_stack` on an ingress stack, all-target snapshot + changed-set failure, argv builders.
- verification: up pass/fail (exited, unhealthy, restart increase, zero containers, timeout), down
  pass/timeout, all stops at first failure with the right passed/failed/not-attempted report.
- command service: `/up media` is operator-direct R1 (execution created, `requested_via
  telegram-direct`); with R1 removed from `direct_request_risks` it proposes a card; R2/R3 always
  propose; decide re-checks policy (forbid R2 after proposing → refused, no execution); busy lock; a
  forbidden-risk config refuses; restart behaviour unchanged (existing tests untouched and green).
- farnsworth: command parsing (`/up`, `/up all`, `/down network` → `down_ingress`, usage errors), no
  remaining calls to `stackctl.stack_up/stack_down` from Farnsworth.
- rpc/scruffy: action.request param validation for stack actions; approval/execution reads carry
  `summary`.

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1210 pass today; a sandbox blocking AF_UNIX
  fails the RPC socket tests: report, never skip or fake)
- `.venv/bin/ruff check .`, `node --check` on changed JS, `git diff --check`
- Report every file changed and every deviation from this brief with the reason.

## Must NOT change
`casa_bender.py` safety checks/sudo allowlist/`run_argv`, legacy LLM plan execution, the restart
action's semantics, auth, `SCHEMA_VERSION` (no schema change is needed: approvals store arbitrary
`target_json`; if you believe one is, stop and say why).
