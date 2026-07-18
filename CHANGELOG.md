# Changelog

All notable changes to Planet Express will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Repo scaffold: LICENSE (MIT), README, this CHANGELOG, and `CLAUDE.md` establishing a standing
  independent Codex-review gate for security/release-relevant work — same convention used on the
  author's other public project (Billarr), applied here from the very first commit rather than
  bolted on later.
- Initial commit of the existing pipeline (Leela/Hermes/Farnsworth/Bender/Zoidberg/Amy) as it runs
  today — hardcoded to the author's own host. This is the real starting baseline the generalization
  work happens against, not a rewritten-from-scratch v1.
- **Spec 1: config-driven topology.** `config.py`'s hardcoded `STACKS_ROOT`/`FORBIDDEN_STACKS`/
  `MOUNT_UNITS`/`PAUSED_CONTAINERS` are now loaded from a validated config file (`CASA_CONFIG` env
  var, default `/etc/planetexpress/config.yaml`) instead of being literal Python constants —
  `config.example.yaml` ships with generic placeholder values for anyone installing this for
  themselves. Bad or missing config now fails fast with a clear, field-level error. Unknown config
  keys are rejected outright (an independent Codex review caught that a misspelled key would
  otherwise silently vanish and quietly disable whatever safety list the operator thought they'd
  set) and `stacks_root` must be an absolute path. `casa_zoidberg.py`'s `EXCLUDE_SERVICES` moved
  into the same config, closing a drift gap where it lived as a local constant instead of the
  shared source of truth. The long-dead `casa-sysadmin-context.yaml` (a leftover design doc from
  before the real pipeline existed — confirmed via grep that nothing had called its loader in
  months, and its content had gone materially stale with nothing to catch it) is retired, not
  migrated.
- **Spec 2: Notifier interface abstraction.** `casa_farnsworth.py`'s 57 direct `TelegramClient`
  call sites are now split behind a `Notifier` interface (`notify`, `request_approval`,
  `interpret_decision`, `resolve`) — outbound notifications and the plan/diff approve-or-cancel
  flow are channel-agnostic in shape now, backed by `TelegramNotifier` for v1 and a `FakeNotifier`
  for tests. Deliberately does not cover the `/stacks`/`/up`/`/down`/`/mounts`/`/help` remote-control
  command parsing — a genuinely different interaction model, left on `TelegramClient` directly.
  Known gap: `casa_bender.py`/`casa_zoidberg.py` still talk to `TelegramClient` directly for their
  own step-status/rollback notifications — deferred rather than tripling this spec's size. Verified
  live against the real bot: a real approve tap ran a real (harmless, read-only) plan through Bender
  end to end, and a real cancel tap was exercised twice, including on one genuine organic finding.
- **Spec 3: state schema stabilization.** The 6 state files (`latest_monitor.json`,
  `latest_findings.json`, `pending_plan.json`, `run_status.json`, `rollback_candidates.json`,
  `update_history.json`) now carry a `schema_version` and are validated at the write boundary via
  new `state_models.py`, instead of each producer writing an ad hoc dict literal — so a future shape
  change trips a version check instead of a silent `KeyError` in a consumer (the planned read-only
  dashboard). Internally-produced files are strict; `Findings`/`PlanSet` (LLM-produced) tolerate
  extra fields rather than crash the pipeline over unexpected LLM output shape. Consolidated a
  duplicated `ROLLBACK_CANDIDATES_FILE` definition (independently defined in both
  `casa_farnsworth.py` and `casa_zoidberg.py`) into `config.py`, and fixed `casa_leela.run_full()`
  never setting a `mode` key unlike `run_status()`/`run_updates()`. `update_history.json`'s on-disk
  shape changes from a bare list to an enveloped `{"schema_version":1,"entries":[...]}` — not
  migrated, since that history is low-stakes. An independent Codex review caught that
  `UpdateHistoryEntry.old_id`/`new_id` were required strings when the real code legitimately
  produces `None` for a stopped service — fixed to `Optional[str]`.
- **Spec 4: sudo/execution allowlist enforcement.** `casa_bender.py`'s `_safety_check()` was a pure
  blocklist with **no allowlist check at all** for sudo-scoped commands — the only things stopping
  an out-of-scope `sudo` were Farnsworth's planning-prompt text (soft) and the OS-level sudoers.d
  grant itself (hard, but only blocks actual escalation, not the attempt). Now a config-declared
  allowlist (`config.yaml`'s `sudo_allowlist`, empty by default) is enforced in code, independent of
  whatever a plan's LLM-generated commands claim to need — anything other than
  `sudo systemctl start|stop|restart <unit>` matching a declared unit or glob grant is rejected
  outright, before ever reaching a shell. An independent Codex review found and this fixed **three**
  real bypasses in sequence: (1) the check only looked at segments starting with literal `sudo`, so
  a shell wrapper (`env sudo ...`, `sh -c '...'`) or a later line in a multi-line command skipped the
  check entirely; (2) a since-reverted attempt to tolerate an absolute-path `sudo` invocation used a
  regex permissive enough (`\S*/`) to let a command substitution disguised as a "path prefix" through
  while the shell still executed the embedded sudo call; (3) the unit-name capture itself (`\S+`)
  admitted a command substitution disguised as a unit name, which also happened to satisfy the
  `*.mount` glob's suffix match — fixed by constraining it to a strict systemd-unit-name character
  class. This host's real `config.yaml` now declares its actual existing grant so nothing that
  worked before stops working. Verified live: a real allowed action executes through Bender exactly
  as before; every bypass variant found, plus the exact historical near-miss command
  (`sudo mount -a`, once actually proposed by a real Farnsworth plan), is rejected with a clear
  `SafetyError` before `subprocess.run` is ever called.
- **Spec 5: setup wizard, install docs, sudoers.d generation.** `scripts/setup_wizard.py` is an
  interactive topology wizard built directly on `PlanetExpressConfig` (split out into a new
  `config_schema.py` so it can be imported before a config file exists) — stacks/forbidden
  stacks/paused containers/mounts/sudo scope, with Tab-completion on path prompts. It also
  generates and installs the matching `/etc/sudoers.d/planetexpress` grant from the same
  `sudo_allowlist` data, single source of truth with what `casa_bender.py` enforces in code.
  `deploy.sh` is generalized off this host (dynamic `INSTALL_DIR`/`RUN_USER`, no more hardcoded
  `casaroot`), drops the dead `CONTEXT_FILE` reference, gains real preflight checks (Docker daemon
  reachability, `docker compose` plugin, Python 3.11+), and now actually prompts for
  `LLM_PROVIDER`/API key/Telegram bot token+chat id (previously only `ANTHROPIC_API_KEY` was ever
  asked for, despite `TG_BOT_TOKEN`/`TG_CHAT_ID` being required for the bot to start at all).
  Systemd units become `$INSTALL_DIR`/`$RUN_USER`/`$RUN_GROUP`/`$CONFIG_FILE` templates rendered by
  a new `scripts/render_template.py`, rather than hardcoded to one host. New `INSTALL.md` walks a
  from-scratch install end to end. `config.py`'s `STATE_DIR`/`LOG_DIR` defaults drop their last
  hardcoded absolute path.

  This is the spec that most stress-tested the standing Codex-review gate: **9 review rounds, each
  finding real, previously-unfound issues**, not diminishing returns on a clean diff. Roughly in
  order of severity: (1) a sudoers wildcard bypass — a `*.mount`-style glob grant was written into
  sudoers as a literal wildcard, but sudoers matches `*` via `fnmatch()` against the *entire*
  remaining command-line string, crossing whitespace, so the rule also matched
  `systemctl stop ssh.service data.mount` — fixed by expanding globs to exact discovered unit names
  at generation time, never a raw wildcard; (2) the unit-name prompt in the sudo-allowlist flow had
  *zero* input validation, so a value like `"foo.service, /bin/bash"` (comma starts a second Cmnd in
  sudoers syntax) flowed straight into a NOPASSWD rule — fixed by validating against the same
  character class `casa_bender.py`'s own regex enforces; (3) running `deploy.sh` as root (or via
  `sudo bash deploy.sh`) would make the generated service run Bender as root, at which point bare
  commands already have full root access, completely bypassing Spec 4's entire sudo-allowlist model
  (which only ever inspects segments containing the literal word `sudo`) — now a hard preflight
  error; (4) a predictable-`/tmp`-path TOCTOU race on the generated sudoers/config candidate files —
  fixed with `tempfile.mkstemp`; (5) redeploying unconditionally overwrote `casa-stacks.service`,
  silently destroying a custom mount-readiness gate (`Requires=`/`After=`) an operator had added —
  now asks before overwriting; (6) `sed`-based template rendering corrupted values containing
  `&`/`\`/the delimiter, and separately could silently produce a broken (unquoted) unit file on a
  path containing a space — replaced with `scripts/render_template.py` (`string.Template`, no
  metacharacter risk), which also validates against space/quote/backslash/dollar/backtick/`%`
  up front; (7) a systemd unit name containing `:` (valid syntax, e.g. template/instance units) broke
  the generated sudoers file, since sudoers treats an unescaped `:` as a delimiter — fixed with a
  `_sudoers_escape()` helper, verified with a real `visudo -c` round-trip in the test suite; (8) two
  separate `~` (tilde) expansion bugs — bash never expands a literal `~` typed into a variable read
  at runtime, and `os.listdir()` never expands it either, so a config path or mount path typed with
  a leading `~` was silently stored/used wrong; (9) `deploy.sh` skipped the wizard *entirely* when
  `config.yaml` already existed (e.g. an upgrade), which also skipped the only code path that
  reconciles `/etc/sudoers.d/planetexpress` with `sudo_allowlist` — split into a
  `reconcile_sudoers()` that now always runs regardless of whether the config was just collected or
  reused. Also fixed along the way: a missing `network-online.target` dependency on
  `casa-stacks.service.template` (a real regression from generalizing away the reference
  deployment's host-specific mount-readiness unit, which had provided that transitively), a
  `getpass.getuser()`/`whoami` mismatch that could generate a sudoers grant for the wrong account, an
  unvalidated `LLM_PROVIDER` value that would pass install but fail every LLM call at runtime, an
  unresolved `visudo` `PATH` lookup, credentials echoed to the terminal during setup, and an empty
  actions list silently accepted mid-wizard. Two real usability bugs (not security issues) were also
  found via this session's own live dogfooding on a real terminal, before Codex was ever involved: an
  unregistered Tab key silently leaking a literal tab character into a path prompt, and typing
  "none" instead of pressing Enter producing garbage config entries — both fixed with readline path
  completion and blank-sentinel handling. **How to apply:** for an installer/wizard spec
  specifically — which touches far more real-world variation (paths, usernames, PATH environments,
  re-run/upgrade states) than a single pipeline's own code — budget for several review rounds as the
  default expectation, not the exception; this spec's fixes came in nine passes, each surfacing a
  genuinely distinct issue, not the same one restated.

---
