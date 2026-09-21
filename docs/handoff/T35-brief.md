# Task T35 — config editing core: D29 enforcement, config RPCs, safe activation (slice 4, part 1)

Branch `v2`. Python 3.11+, no new dependencies. This is the security half of slice 4; the dashboard
editor screen is T36 and is NOT part of this task (add JSON routes only, no template/JS).

## Why
T26 built `ConfigService` (validate → atomic write → re-exec) but left it unwired, and D29 decided the
rules for exposing it. Slice 4 lets an operator edit config from the dashboard. Before any UI exists,
core must enforce what may change, who changed it, and that activation cannot strand core or the
dashboard's request.

## Read first
- `docs/designs/planet-express-2-0-slices.md`: premise **P3** (~line 58), slice 4 (~line 448), the
  **T24** note "For D29 (slice 4)" (~line 1159), and the **T26** entry incl. **D29** (~lines 1191-1225).
- `planet_express/application/config_service.py`, `config_io.py` (`validate_config_text`,
  `write_config_text` — keeps the operator's TEXT so comments survive; preserves mode/owner/ACL),
  `planet_express/core/reexec.py`, `casa_farnsworth.py` `_reexec_core` / `build_config_service` /
  `run_bot` / `_start_dashboard_rpc`, `PipelineState.try_begin_mutation(require_idle=True)`.
- `planet_express/integrations/rpc.py` — handler registration, param validation style
  (`auth_params`, exact key sets), `RpcError` codes, 1 MiB frame, worker pool + reserved decision
  worker, how a handler's reply is written.
- `config_schema.py` (`PlanetExpressConfig`, `extra="forbid"`), `config.py` (`CONFIG_FILE`).
- `scripts/setup_wizard.py` `main()` (~line 473-600): creates `/etc/planetexpress` and the config as root.
- `casa_scruffy.py`: `authenticate()`, `core()`, `is_json_request()`, CSRF from `request.form`,
  existing JSON routes (e.g. `/api/approvals/<id>/decide`) for style.

## Required behaviour

### 1. Field policy in `ConfigService` (D29 + P3)
Compare the parsed live config with the parsed draft **per top-level field** (`model_dump(mode="json")`
of each). Classify every changed field:
- `EDITABLE = {"paused_containers", "exclude_services", "backup_jobs"}` — always allowed.
- `SENSITIVE = {"sudo_allowlist", "forbidden_stacks", "autonomy"}` — allowed **only** when the
  root-only switch is on (below). Otherwise the apply is refused: `status: "locked"`, nothing written.
- anything else (`stacks_root`, `mounts`, `lan_only_domain`, …) — `status: "locked"` with reason
  "edit on the host": these change paths and host wiring, P3 does not expose them. Say so in the
  result so the UI can explain it.
A draft that differs only in comments/formatting has no changed fields and applies (it is the
operator's text). A draft with no text change at all → `status: "unchanged"`, nothing written, no
re-exec.

The switch: `PE_ALLOW_SENSITIVE_CONFIG_EDITS=1` read **once at core startup** from core's environment
(systemd loads `/etc/planetexpress.env`, root-owned 600) and passed into `ConfigService` as a
constructor argument. Any other value, or unset, is off. Never read it from `config.yaml`, never from
the request.

### 2. Optimistic concurrency and bounds
- `apply(text, *, base_sha256, operator)`: under the mutation lock, re-read the live file and compare
  its SHA-256 with `base_sha256`; mismatch → `status: "conflict"` (someone edited the file since the
  operator loaded it), nothing written. The field classification must also be computed against that
  re-read live text, not a cached copy.
- Reject drafts over 256 KiB (`status: "invalid"`, a clear error) before parsing; also reject
  non-UTF-8 / NUL-containing text.
- Result statuses become: `invalid | locked | conflict | busy | unchanged | write_failed | activating`,
  each with `errors`, `reason`, `changed_fields`, `locked_fields`.

### 3. Activation that the caller survives
Today `apply` calls `activate()` synchronously, which `execv`s inside the caller — over RPC the reply
would never be sent. Change it so:
- `apply` returns `activating` to the caller **after** the write succeeded, while the mutation lock
  stays held (no scan, action or other apply may start between the write and the exec).
- A dedicated activation thread waits for the RPC reply to be written (give the RPC layer a
  post-reply hook, or a short bounded delay if you can show it's sufficient — prefer the hook), then
  sends a best-effort Telegram notice with a bounded timeout (≤10s, never log exception text: it can
  carry the bot token): "⚙️ Config changed by `<operator>`: `<fields>`. Core is restarting to apply
  it." (comment-only edits: "formatting only"), then runs the existing `_reexec_core` path
  (`tg.confirm_updates()` then `reexec()`).
- If exec itself raises, release the mutation lock, record `config.activation_failed`, and keep
  running on the old in-memory config (the file on disk is already new: say so in the event).
- Make sure the RPC socket is rebound cleanly after re-exec (the old fd must not be inherited; the
  stale socket file must not make the new core refuse to start its RPC server). Test it.

### 4. Audit
Every apply outcome records an event in the store: `config.applied` (operator, changed_fields,
before_sha256, after_sha256) or `config.apply_refused` (operator, status, changed_fields,
locked_fields). Never record the config text itself.

### 5. RPC (core) — operator-attributed, exact param sets like the existing handlers
- `config.get {}` → `{text, sha256, path, sensitive_edits_enabled, editable_fields, sensitive_fields}`.
  (config.yaml holds no secrets — they live in the env files — but keep the 256 KiB bound on read.)
- `config.validate {text}` → `{ok, errors, changed_fields, locked_fields}` (no lock, no write; compares
  against the current live file).
- `config.apply {text, base_sha256, operator}` → the apply result above. Operator validated like
  `auth_params`. Must not run on the reserved decision worker.

### 6. Scruffy JSON routes (no UI yet)
`GET /api/config`, `POST /api/config/validate`, `POST /api/config/apply` — form-encoded `csrf_token`,
`text`, `base_sha256`; `operator=g.operator` always, never from the form. `RpcError` → 503 JSON
(never the airlock HTML); add `/api/config` to `is_json_request()`. For `apply`, a dropped connection
after `activating` is expected (core is restarting) — the route should still return the result it got.

### 7. Setup wizard (D29)
`scripts/setup_wizard.py` must leave `/etc/planetexpress` and `config.yaml` owned by the core's run
user (mode 0750 dir / 0640 file), so apply works on default installs exactly as on the live host
(whose config lives in the clone and is already casaroot-owned — do not touch that). The dashboard's
read ACL (`scripts/web_access.py`) must still apply. Unit-test the commands the wizard would run.

### 8. Wire it
`run_bot` builds the service with the switch value and passes it to the RPC handlers. Remove the
`# noqa: F841` placeholder.

## Tests (new `tests/test_config_service.py` cases + RPC/route tests)
- Each field class: editable change applies; sensitive change is `locked` with the switch off and
  applies with it on; `stacks_root`/`mounts`/`lan_only_domain` change is `locked` either way;
  mixed editable+locked draft is refused as a whole (nothing written).
- Comment-only edit applies with `changed_fields == []`; identical text → `unchanged`, no activation.
- `conflict` when the file changed after `base_sha256`; classification uses the re-read file.
- Oversize, NUL, non-UTF-8 drafts → `invalid`, file unchanged.
- `busy` when a scan/mutation holds the lock; the lock stays held after `activating` until the
  activation thread runs; exec failure releases it and records `config.activation_failed`.
- The reply is written before activation (RPC-level test with a fake activate).
- Events recorded for applied and refused, without config text.
- Switch parsing: only exactly `"1"` enables.
- RPC param validation (extra/missing keys, bad operator, oversize text) and Scruffy routes
  (CSRF required, operator from token, 503 JSON on RpcError, JSON 401 unauthenticated).
- Re-exec rebinds the RPC socket (stale socket file present → new server starts).

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1166 pass today; a sandbox that blocks
  AF_UNIX fails ~39 RPC socket tests: report that, never skip or fake them)
- `.venv/bin/ruff check .`, `git diff --check`
- Report every file changed and every deviation from this brief with the reason.

## Must NOT change
`casa_bender.py` safety checks and sudo allowlist enforcement, the approval/typed-action paths,
`SCHEMA_VERSION` (events use the existing table), auth/login, chat, templates/JS.
