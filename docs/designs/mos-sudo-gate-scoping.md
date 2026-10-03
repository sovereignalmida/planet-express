# Scoping: a sudo gate for `MosHostControlProvider`'s mutating actions

Status: **SCOPING NOTE — not a design, no code**. Written to surface what deciding this actually
involves, before anything is built. Parent docs: `phase-3-host-control-provider.md`,
`mos-host-control-provider.md` (ships `MosHostControlProvider` read-only; its
`start_service`/`stop_service`/`restart_service` raise `NotImplementedError` specifically because
this gate doesn't exist yet).

## 1. What the systemd gate actually is — two layers, generated from one source

Not just a regex. Two independent layers, deliberately redundant:

1. **Code-level**, in `casa_bender.py`: `_check_sudo_allowlist()` matches a command against
   `_SUDO_SYSTEMCTL_RE` (`^sudo\s+systemctl\s+(start|stop|restart)\s+([A-Za-z0-9_.@:-]+)$`,
   strict — Codex found real bypasses when this was looser), then checks the parsed
   `(unit, action)` against `config.yaml`'s `sudo_allowlist` (`SudoUnitGrant`/`SudoGlobGrant`,
   `config_schema.py:99-116`).
2. **OS-level**, via `scripts/setup_wizard.py`'s `reconcile_sudoers()` /
   `generate_sudoers_snippet()`: renders the *same* `cfg.sudo_allowlist` into
   `/etc/sudoers.d/planetexpress` `NOPASSWD` lines, `visudo -c` verified before install. Globs are
   **expanded to exact discovered unit names** at generation time (`systemctl list-units`), never
   written as a literal sudoers wildcard — an independent Codex review found a literal
   `*.mount`-style sudoers wildcard matches across the whole remaining command line, not just one
   token.

Both layers read `cfg.sudo_allowlist` — one config source, enforced twice, by two different
mechanisms (Python regex vs. the OS's own sudo). This is the thing a MOS equivalent has to
reproduce in shape, not just "add another regex."

## 2. Three things that are genuinely different for `service`, not just renamed

- **Argument order reverses.** `systemctl <action> <unit>` vs. `service <unit> <action>` — the
  unit and the action swap positions in the command line. A gate regex copied from
  `_SUDO_SYSTEMCTL_RE` with s/systemctl/service/ would parse the wrong group as the unit.
- **Unit-name charset is narrower.** systemd units legitimately contain `.`, `@`, `:` (template/
  instance units — the escaping bug `_sudoers_escape()` exists for). `/etc/init.d/*` script names
  on the live MOS VM (`docker`, `cron`, `ssh`, `nginx`, ...) are plain `[A-Za-z0-9_-]+` — simpler,
  but "simpler" still needs its own validated regex, not systemd's borrowed one (a looser charset
  copied over for "safety margin" would just reopen the shell-metacharacter bypass class Codex
  already closed once for the systemd version).
- **No glob-to-exact-unit discovery mechanism exists yet.** `generate_sudoers_snippet()`'s glob
  expansion calls `_discover_mount_units()` → `systemctl list-units --type=mount`. The MOS
  equivalent would be "list `/etc/init.d/*`" — mechanically easy, but it's new code, not reuse,
  and glob grants may not even be a meaningful concept for sysvinit scripts (there's no `*.mount`-
  shaped pattern to match against). Worth asking whether glob grants are needed at all for a first
  MOS cut, or whether exact-unit grants cover every real case.

## 3. The gap this surfaces that isn't really about sudo: nothing selects a provider yet

`phase-3-host-control-provider.md`'s own non-goals already named this and deferred it: "Host-type
auto-detection / dispatch logic... with one implementation, there's nothing to dispatch between
yet." There are two implementations now, and a sudo gate can't be finished without answering the
question that non-goal deferred:

**How does an install declare which command family it's gating — `systemctl` or `service`?**

`cfg.sudo_allowlist` today is one untyped list of `(unit, action)` grants; `casa_bender.py` always
renders/checks them as `systemctl`. The gate can't "just add a MOS branch" without something
telling it which branch applies to *this* install. That needs its own small decision (e.g. a
`host_control_provider: systemd | mos` field in `config.yaml`, read once, used by both
`_check_sudo_allowlist()` and `generate_sudoers_snippet()` to pick the command template) — small
in code, but it's the actual missing piece, not a detail inside "write a MOS regex."

## 4. What this scoping note deliberately does not cover

**Installing Planet Express itself on a MOS host** — i.e. the thing Chris is actually excited
about ("test deploy PE on the MOS VM") — is a materially bigger, separate problem this gate does
not solve:

- PE's own processes (`casa-planetexpress`, `casa-dashboard`) are installed as **systemd units**
  today (`deploy.sh`, `scripts/setup_wizard.py`'s install path). Running PE *on* MOS means either
  writing `/etc/init.d/casa-planetexpress` equivalents (sysvinit has no `Restart=on-failure`,
  no cgroup sandboxing directives — different guarantees, not a 1:1 port) or running it
  unmanaged for a test.
- `MosHostControlProvider` as built controls services **on the host PE already runs on** (same as
  `SystemdHostControlProvider` — `bender.run_argv` is always local, no remote-execution layer
  exists anywhere in this codebase). It does not let an Ubuntu-hosted PE install reach over and
  control the MOS VM's docker stacks remotely. Whichever host PE is actually installed on is the
  host whose provider applies.

This note is scoped to exactly what was asked — the sudo gate for mutating actions — because
conflating it with "install PE on MOS" would make neither decision cleanly, and the second one is
a real, separate design conversation (install mechanism, process supervision without systemd,
whether it's worth it for a test host at all vs. just validating the *abstraction* read-only as
Phase 3/MOS already have).

## 5. Open questions (need Chris)

- **Is a MOS sudo gate worth building before there's a call site for it at all?** Nothing calls
  `start_service`/`stop_service`/`restart_service` on either provider yet (Phase 3b, the call-site
  migration, is still unstarted for the systemd provider too). Building the gate now proves the
  abstraction further; building it only when something needs it avoids designing security surface
  speculatively — same tension the project already resolved once for Phase 2 ("keep it as a
  shell-suggestion feature, not wanted").
- **Provider selection**: a new `config.yaml` field now, or deferred until there's an actual
  second *install* (vs. just a second local test VM) to select between?
- **Glob grants for MOS**: worth building the `/etc/init.d/*` discovery mechanism, or exact-unit
  grants only for a first cut (mirroring how tight `mos-host-control-provider.md`'s own read path
  stayed)?
- **The bigger question underneath this one, if "test deploy PE on the MOS VM" is the actual
  near-term goal**: is that worth scoping as its own doc now, in parallel, rather than discovering
  it piecemeal while trying to finish the sudo gate? (§4 above is a first pass at naming what it
  would involve, not a plan.)
