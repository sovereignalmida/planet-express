# MosHostControlProvider — the second `HostControlProvider`, and what it proves

Status: **DRAFT — Revision 0.1** (2026-10-03). Parent docs: `phase-3-host-control-provider.md`
(defines the `HostControlProvider` Protocol and ships the first implementation,
`SystemdHostControlProvider`) and `v3-migration-story.md` §6 (names "provider testability" —
"same codebase runs correctly on Ubuntu/systemd *and* a MOS-based test host" — as unfalsifiable
until a non-systemd test host exists).

## 1. What changed: a real non-systemd host now exists

A MOS VM (Devuan 6 "excalibur") is up locally via QEMU — see
`planet-express-v3-architecture-pushback.md` memory for the build. This closes the
infrastructure gap migration-story §7 left open ("stand up a second non-systemd host now,
even cheaply"). This doc is the first thing built against it.

Checked directly rather than assumed from the brief's "OpenRC" guess: MOS is **plain sysvinit**,
not OpenRC. `/usr/sbin/service <name> <action>` runs `/etc/init.d/<name> <action>`. No
`systemctl`, no `rc-service`/`rc-status`.

## 2. A real parity bug, found before it could be inherited

`SystemdHostControlProvider._raw_state()` trusts `systemctl is-active`'s exit behavior directly —
safe, because systemd guarantees `active`/`inactive`/`failed` as a stable, machine-readable
vocabulary tied correctly to the real process state.

Devuan's `/etc/init.d/*` scripts make no such guarantee. Inspected directly on the VM:
`/etc/init.d/docker`'s `status)` case is:

```sh
status)
  if [ -f "$DOCKER_SSD_PIDFILE" ] ; then
    status_of_proc -p "$DOCKER_SSD_PIDFILE" "$DOCKERD" "$DOCKER_DESC"
  else
    echo "Docker is not running."
  fi
  ;;
```

When the pidfile is missing, the script `echo`s and falls off the end of the `case` with no
`exit` — the script's own exit code is whatever `echo` returned, which is **0**. Confirmed live:
`service docker status` returned exit `0` for both "Docker is running." and "Docker is not
running." A provider built the way `SystemdHostControlProvider` was — trust the exit code as the
source of truth — would silently read a stopped Docker as running on this host. `cron`'s script,
by contrast, uses `/lib/lsb/init-functions` correctly and returns exit `3` when stopped — so this
isn't "sysvinit is unreliable," it's "individual scripts vary, and nothing declares which."

## 3. The fix: cross-check text against exit code, scoped to the services this host actually runs

`MosHostControlProvider._raw_state()` (`planet_express/execution/host_control_mos.py`) reads
**both** signals from one `service <unit> status` call:

- **Text**: MOS's own scripts consistently phrase the human-readable line as `"... is running"` or
  `"... is not running"` (confirmed across `docker`, `cron`, `ssh` on the live VM) — checked by
  substring match, `"is not running"` tested before `"is running"` since the former contains the
  latter's words.
- **Exit code**, only where LSB actually asserts something: `0` → candidate `"running"`, `3` →
  candidate `"stopped"`. Any other code (`1`, `2`, `4`, a timeout) asserts nothing and is not used
  to agree or disagree.

Decision table:

| Text parses? | Exit asserts a state? | Agree? | Result |
| --- | --- | --- | --- |
| no | — | — | unreadable (`None`) — unrecognized output is never guessed at |
| yes | no (other exit code) | — | trust text — it's the only signal available |
| yes | yes | yes | trust it — both signals agree |
| yes | yes | **no** | unreadable (`None`) — this is the docker-script case; refuse rather than report a value one of the two signals contradicts |

This is deliberately **not** a general free-text status parser for arbitrary future services —
it's scoped to the phrasing MOS's own scripts actually use today, the same way
`SystemdHostControlProvider` is scoped to what `systemctl is-active` actually returns. A future
service with different phrasing surfaces as the "text doesn't parse" row (`None`), not a silent
misread — the same fail-closed shape `is_service_running`'s docstring already commits to.

## 4. Non-goals (this landing)

> **Update (2026-10-03):** `start_service`/`stop_service`/`restart_service` were `NotImplemented`
> in this doc's first revision, for the reason below. The gate has since been scoped
> (`docs/designs/mos-sudo-gate-scoping.md`) and built, on Chris's explicit decision to build it
> ahead of any call site existing — see that doc's §6. The original reasoning is left in place
> because it's still why this landing didn't build the gate itself.

- **No `start_service`/`stop_service`/`restart_service`, originally.** These raised
  `NotImplementedError`, same as `reboot()`/`shutdown()` still do. Reason: `SystemdHostControlProvider`'s
  mutating actions are a *faithful extraction* of `engine.py`'s existing `_check_sudo_allowlist()`
  gate — a real, already-reviewed security boundary this code mirrors exactly. No equivalent gate
  for `sudo service <unit> <action>` existed anywhere in this codebase to extract from at the time.
  Inventing one — deciding which services are mutable, what pattern a sudoers entry would match,
  how the allowlist config shape extends — was a new security-relevant design decision, not a port,
  and didn't belong in the same landing as a read-path parity fix.
- **No general directory-log tailing.** `get_host_logs()` runs `tail -n <lines> /var/log/<unit>` —
  correct for the services that log to a single flat file (`docker`, `cron`, `api`, `syslog`-style
  services). A unit whose log is a directory (`nginx`, `samba`, `wsddn` on this host) surfaces
  `tail`'s own "Is a directory" error as the returned line, the same honest-failure shape
  `get_host_logs` already uses for a `journalctl` failure — not a guess at which file inside the
  directory is "the" log.
- **No call-site migration** — same as Phase 3's original landing; nothing calls
  `MosHostControlProvider` yet.
- **`get_uptime_seconds`/`get_metrics` are unchanged** — `uptime`/`free -h` behave identically on
  Devuan and Ubuntu; this provider imports `SystemdHostControlProvider`'s own parsing (same
  execution layer, not a cross-layer import) rather than duplicating two regexes a second time.

## 5. What this proves, and what it still doesn't

This makes Phase 3's portability claim falsifiable for the first time, and it already caught one
real bug the systemd-only version couldn't have surfaced (the docker-script exit-code lie). As of
the sudo gate landing (`docs/designs/mos-sudo-gate-scoping.md`), the mutating half of the Protocol
is implemented too — `MosHostControlProvider._service_action()` mirrors
`SystemdHostControlProvider._unit_action()`'s control flow exactly, gated by `config.yaml`'s
`sudo_allowlist` under `host_control_provider: mos`. What's still unproven: there is still no real
call site for either provider's mutating methods (Phase 3b, deferred since Phase 3's original
landing), and installing Planet Express itself on a MOS host — a materially bigger, separate
problem — remains out of scope (see the scoping doc's §4/§6).
