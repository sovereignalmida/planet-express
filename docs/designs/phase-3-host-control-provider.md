# v3 Phase 3 — HostControlProvider

Status: **DRAFT — Revision 0.1** (2026-10-02). Not yet reviewed by Chris. Parent docs:
`Planet Express v3 Architecture Brief.md` (§5-§6, §20 Phase 3) and its `... - Addendum.md`
(phase-order revision; §5's naming-collision resolution, which this doc inherits as settled —
see below). Builds on `docs/designs/phase-1-state-model.md` (merged, `main`), which is why this
is Phase 3 rather than Phase 1: the Addendum's whole argument was "let the state model decide
the interface's shape, not the reverse."

## 1. Naming — already settled, not reopened here

Per `phase-1-state-model.md` §2/§5 and the Addendum's revision to §5: the brief's `HostProvider`
collides with `planet_express/integrations/beszel.py`'s `HostProvider` (shipped on `main`,
meaning "remote host *data* source"). This interface is **`HostControlProvider`**, full stop.

## 2. Evidence: what's actually there today

Confirmed by direct codebase research (2026-10-02), not assumption — this determines the real
scope, which turned out larger than "wrap one chokepoint":

- **There is no single subprocess chokepoint.** `casa_bender.run_argv`/`run_argv_bounded`
  (`casa_bender.py:167-258`) is real and *is* consistently used by the newer typed-action engine
  (`planet_express/execution/engine.py`, `.../actions.py`, `.../binding.py`,
  `planet_express/application/command_service.py`). But `casa_leela.py`, `casa_zoidberg.py`,
  `casa_stackctl.py`, `casa_boot.py`, and at least five files under `scripts/` each have their
  **own, independent** `subprocess.run` wrapper or direct call, never touching Bender. Wrapping a
  provider around `run_argv` alone would cover the engine path cleanly and leave the rest
  completely untouched.
- **The one real `unit.action` step type is the cleanest existing entry point.**
  `engine.py:479-505`'s `_unit_active()`/`_unit_action()` already does exactly
  "`is_service_running`" (`systemctl is-active <unit>`) and
  "`start_service`/`stop_service`/`restart_service`" (`sudo systemctl <action> <unit>`, gated by
  `bender._check_sudo_allowlist`) — this is Phase 3's natural first migration target, not a new
  capability to invent.
- **Host facts (uptime, memory) are collected by `casa_leela.py`, not the engine, via its own
  `_run()`.** `casa_leela.py:908-920`'s `check_system()` runs `free -h`, `uptime`, and a bounded
  `journalctl -p err -S '1 hour ago'` directly, independent of Bender entirely. No CPU% or
  filesystem/disk stats collector exists anywhere in this path today (disk usage is queried ad
  hoc elsewhere, e.g. `setup_wizard.py`'s `docker info`, not part of this interface's natural
  scope).
- **`reboot`/`shutdown` do not exist as a capability anywhere in the codebase today.** Checked
  directly — the only hits for "reboot"/"shutdown" are prose comments and unrelated *socket*
  `shutdown()` calls (closing HTTP connections). This is new ground, not a migration.
- **No host-type detection exists.** No OS/init-system check anywhere in `config.py`/
  `config_schema.py`. A `SystemdHostControlProvider` would be the *only* implementation that can
  exist until an actual MOS (or other non-systemd) test host exists — which the Addendum already
  flagged as a prerequisite for Phase 3's own proof criterion ("same codebase runs correctly on
  two host types") to be falsifiable at all. One implementation plus an interface is a wrapper,
  not a proven abstraction — true here too, stated plainly rather than oversold.
- **`planet_express/core/hosts.py`** (T48, merged) sets the house style for a new core type
  module: `@dataclass(frozen=True)` throughout, every optional field's docstring explains *why*
  it's optional (not just that it is), named constants justified by one-paragraph rationale tied
  to measured data, `Literal` aliases for small enums, and strict purity — collection/adapter
  code lives in `execution`/`integrations`, never in the `core` type module itself.

## 3. Requirement: don't invent capability the project doesn't have

Reboot/shutdown and CPU/filesystem metrics don't exist today. A `HostControlProvider` interface
that defines `reboot()`/`get_filesystems()` methods with no real backing implementation would be
exactly the kind of brief-sketch-becomes-code mistake the Addendum already pushed back on
once (§5-6 designing the interface before the reasoning that needs it exists). This doc defines
the full interface the brief sketches, but is explicit about which methods are **implemented**
(backed by a real, tested `SystemdHostControlProvider` method) versus **declared but
unimplemented** (raises `NotImplementedError`, documented as future work, never silently
stubbed with fake data).

## 4. Goals and non-goals

Goals (this landing)
1. Define `HostControlProvider` as a `typing.Protocol` in `planet_express/core/host_control.py` —
   pure types, matching `core/hosts.py`'s conventions.
2. Ship one concrete implementation, `SystemdHostControlProvider`
   (`planet_express/execution/host_control_systemd.py`), as a **faithful extraction** of what
   `engine.py`'s `_unit_action`/`_unit_active` and `casa_leela.py`'s `check_system()` already do
   — same `systemctl`/`journalctl`/`free`/`uptime` commands, same sudo-allowlist gating, same
   parsing. Behavior-unchanged extraction, not new behavior, pinned by characterization tests
   (same pattern `actions.py`'s own docstring describes: "Extracted from casa_zoidberg.py...
   with behavior unchanged, pinned by tests").
3. Prove the extraction is faithful: tests asserting the provider's output matches what the
   existing functions produce today, run against both mocked and (where safe) the real
   `casa_bender.run_argv`/`subprocess` boundary.

Non-goals (explicitly deferred, not forgotten)
- **Rewiring any existing call site.** `engine.py`, `casa_leela.py`, `casa_zoidberg.py`,
  `casa_stackctl.py`, `casa_boot.py`, and `scripts/*` keep calling `systemctl`/`journalctl`
  directly in this landing. Migrating them to use `HostControlProvider` instead is "Phase 3b" —
  deliberately separate so this lands as one reviewable, low-risk change (mirroring how Phase 1
  shipped the graph before wiring `casa_boot.py` to it, in its own later commit).
- **`reboot()`/`shutdown()`.** No existing capability to extract; inventing one is a new,
  higher-stakes capability decision, not a refactor. Declared in the Protocol per the brief's
  sketch, raises `NotImplementedError`, documented as genuinely future work.
- **`get_cpu()`/`get_filesystems()`.** No existing collector. Same treatment:
  declared, not implemented, not faked.
- **A second (e.g. `MosHostControlProvider`) implementation.** Blocked on an actual MOS test
  host existing, per the Addendum's own "provider testability" open item. Nothing here should
  claim portability until that exists.
- **Host-type auto-detection / dispatch logic.** With one implementation, there's nothing to
  dispatch between yet.

## 5. The model

```python
# planet_express/core/host_control.py (sketch, not final)

class HostControlProvider(Protocol):
    def is_service_running(self, unit: str) -> bool: ...
    def start_service(self, unit: str) -> ServiceActionResult: ...
    def stop_service(self, unit: str) -> ServiceActionResult: ...
    def restart_service(self, unit: str) -> ServiceActionResult: ...
    def get_host_logs(self, unit: str, *, lines: int) -> tuple[str, ...]: ...
    def get_uptime(self) -> HostUptime | None: ...
    def get_memory(self) -> HostMemory | None: ...
    # Declared, not implemented this landing -- see §4 non-goals:
    def get_cpu(self) -> HostCpu | None: ...
    def get_filesystems(self) -> tuple[HostFilesystem, ...]: ...
    def reboot(self) -> None: ...
    def shutdown(self) -> None: ...
```

Result/data types (`ServiceActionResult`, `HostUptime`, `HostMemory`, ...) follow `core/hosts.py`'s
pattern: frozen dataclasses, every optional field's absence-meaning documented, not just typed.

`SystemdHostControlProvider`'s methods map directly onto what already exists:
- `is_service_running` → `engine.py`'s `unit_active()` (`systemctl is-active`)
- `start_service`/`stop_service`/`restart_service` → `_unit_action`'s
  `sudo systemctl <action> <unit>`, same `_check_sudo_allowlist` gate, same before/after
  active-state verification
- `get_host_logs` → the same bounded `journalctl -u <unit> ... -n <lines>` shape already used in
  `casa_leela.py:953` and Bender's `READONLY_DIAGNOSTIC_PREFIXES` flag-scrutiny rules
- `get_uptime`/`get_memory` → `casa_leela.py`'s `check_system()`'s `uptime`/`free -h` parsing,
  extracted rather than duplicated

## 6. Open questions (not yet resolved, need Chris)

- **Does Phase 3b (the actual call-site migration) happen at all before Phase 4/5/6, or does
  the provider just sit alongside the existing direct calls indefinitely until a second
  implementation actually needs it?** Migrating `casa_leela.py`/`casa_boot.py`/etc. to call
  through the provider is real, non-trivial work across ~6 files for a benefit (portability)
  that can't be exercised until a MOS host exists to be the second implementation. Worth asking
  whether it's worth doing now versus leaving the extraction as a proven-but-unused interface
  until Phase 6 gives it a real second consumer.
- **`ServiceActionResult`/`HostUptime`/etc.'s exact fields** aren't nailed down above (marked
  "sketch, not final") — want to finalize these against what `_unit_action`'s current return
  shape (`StepOutcome`) and `check_system()`'s dict actually carry, rather than inventing a
  schema speculatively.
