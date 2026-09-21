# Task T36 — Config tab: edit, check, diff, confirm, watch it activate (slice 4, part 2)

Branch `v2`. Python 3.11+, vanilla JS, no new dependencies. Web side only: `templates/dashboard.html`,
a new `static/config.js`, `static/cockpit.css`, `static/dashboard.js` (tab list only), tests. Do NOT
change `planet_express/**`, `casa_farnsworth.py`, `config_io.py`, `config_service` semantics or the
T35 routes' contracts. If a finding forces a core change, stop and say so.

## Why
T35 landed the core: field policy (D29), audited apply, re-exec activation, `loaded_sha256`. There is
still no way to use it. P3 says config edits happen in the dashboard "with diff + confirm". This is
that screen.

## Read first
- `docs/designs/planet-express-2-0-slices.md` — premise P3, the D29 decision in the T26 entry, and
  the whole **T35** entry (statuses, `loaded_sha256`, the dashboard reload watch, accepted residues).
- `casa_scruffy.py` routes `GET /api/config`, `POST /api/config/validate`, `POST /api/config/apply`
  (form-encoded `csrf_token`, `text`, `base_sha256`; JSON 503 on core unavailable).
- `planet_express/application/config_service.py` — result shapes; `get()` returns `text`, `sha256`
  (file), `loaded_sha256` (running core), `sensitive_edits_enabled`, `editable_fields`,
  `sensitive_fields`, `path`. Apply statuses: `invalid | locked | conflict | busy | unchanged |
  write_failed | activation_failed | activating`, each with `errors` (`{loc, msg}`), `reason`,
  `changed_fields`, `locked_fields`.
- `static/chat.js` and `static/approvals.js` — house style: one state object, `textContent` only
  (never innerHTML from data), CSRF from `<meta name="csrf-token">`, in-flight guards,
  visibility-gated polling, UI mounted **outside `#dashboard-live`** so the 60s swap can't destroy a
  draft or an in-flight apply.
- `static/cockpit.css` §16 (`.pe-sheet*`, `.pe-verdict*`, `.pe-card` levels, `.pe-logwell`).
- `docs/designs/planet_express_design_v20/system/ACTION-SCREENS.md` cross-cutting rules (there is no
  config mockup; follow the cockpit vocabulary and the two-step confirm sheet used by restart).

## Required behaviour

### 1. A Config tab
Add `Config` to the tab bar and `TAB_NAMES` (hash `#config`). Mount the panel outside
`#dashboard-live`, like chat. Load `GET /api/config` when the tab is first shown.

### 2. Header: what is live and what may change
- Path, and the state of the running config: **ACTIVE** when `loaded_sha256 == sha256`; otherwise
  **FILE NEWER THAN RUNNING CONFIG** (someone edited on the host, or an activation is in flight) —
  say so plainly; do not hide it.
- Three field groups as chips: EDITABLE (`editable_fields`), SENSITIVE (`sensitive_fields`, marked
  LOCKED unless `sensitive_edits_enabled`), and "everything else: edit on the host".
- When sensitive edits are off, one line explaining how to enable them:
  `PE_ALLOW_SENSITIVE_CONFIG_EDITS=1` in `/etc/planetexpress.env` (root-only) and a core restart. It
  is deliberately not switchable from the dashboard — say that too.

### 3. Editor
- A monospace `<textarea>` holding the config text (`spellcheck="false"`, no autocorrect/capitalise),
  full width, usable on a phone. Keep the loaded `sha256` as the base for apply.
- `CHECK` → `POST /api/config/validate`: show `errors` as a list (`loc` + `msg`), and the changed
  fields as chips, locked ones marked with the reason category (sensitive vs host-only). No write.
- `REVERT` restores the loaded text (confirm if the draft differs).
- Warn before leaving the page/tab with unsaved changes (`beforeunload` only while dirty).

### 4. Diff + confirm (the two-step sheet)
`REVIEW CHANGES` (disabled until the draft differs from the loaded text and the last CHECK for this
exact draft was ok):
- A line diff of loaded vs draft computed client-side (a plain LCS over lines is fine; cap at 2,000
  lines per side and show "diff too large to display" beyond that — the 256 KiB bound makes this
  rare). Added/removed lines styled, unchanged context collapsed to 3 lines around each hunk.
- The changed fields, and: "Core restarts in place to apply this. Scans and actions are refused for
  a few seconds."
- `CANCEL` and `APPLY & RESTART CORE`. Apply posts `text` + the base sha.

### 5. Outcomes, rendered in place
- `activating` → an activation card that polls `GET /api/config` every 2s (visible-only) until
  `loaded_sha256` equals the sha-256 of the applied text (compute it client-side with
  `crypto.subtle.digest` **if available**; the dashboard is served over plain HTTP on the LAN, where
  `crypto.subtle` may be undefined — then fall back to "`loaded_sha256 == sha256` and
  `sha256 != base`"). Expect transient 503s while core re-execs; show "core restarting…", not an
  error. Success: **ACTIVE**, with the new sha prefix; reload the editor from the response. After
  60s without success: say core hasn't confirmed the new config and to check the host — never claim
  success.
- `conflict` → the file changed since you loaded it: keep the draft, offer `LOAD LATEST` (replaces
  the editor after confirming) and show that your draft is still in the textarea until you do.
- `locked` → list locked fields with the reason; nothing was written.
- `invalid` → errors as in CHECK. `busy` → "a scan or action is running; try again shortly".
  `unchanged` → "nothing to apply". `write_failed` / `activation_failed` → the reason verbatim; say
  whether anything changed (`activation_failed`'s reason says so).
- Network failure / 503 during *apply* (not activation): the outcome is unknown — say so, then
  re-fetch `GET /api/config` and report what the running core and the file now say.

### 6. Robustness
- One apply in flight at a time; buttons disabled while submitting.
- The dashboard's own workers reload after an apply (T35's reload watch), so a request may land on a
  fresh worker: nothing in this screen may depend on per-worker state.
- Session expiry → the existing JSON 401 handling (redirect to login, as chat does).

## Tests
- `tests/test_scruffy_routes.py`: the Config tab and panel render for an authenticated user, the panel
  is outside `#dashboard-live`, `static/config.js` is referenced, the tab button exists, and the
  markup contains no inline config text (loaded by JS). Existing config-route tests stay green.
- If you add pure helpers (line diff), put them where a test can reach them: a tiny Node check run
  from pytest is acceptable only if `node` is already used by the suite; otherwise keep the diff
  simple and cover it in the VM/browser rehearsal instead. Say which you did.

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1209 pass today; a sandbox blocking AF_UNIX
  fails the RPC socket tests: report that, never skip or fake them)
- `.venv/bin/ruff check .`, `node --check static/config.js static/dashboard.js`, `git diff --check`
- Report every file changed and any deviation from this brief.
