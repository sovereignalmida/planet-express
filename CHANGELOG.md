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

---
