# Setup `apply`: design

Status: decisions D1-D6 accepted as recommended (2026-10-08). Nothing here is built yet. Branch `v3-next`, 2026-10-08.
Third piece of the setup core, after `discover` and `plan` (`setup-plan.md`). `apply` executes a
plan the operator approved. It writes system files as root, so it gets the strictest treatment in the
project, and it deliberately reuses the patterns the runbook engine and `compose_files.py` already
proved rather than inventing new ones.

## What `apply` is

`apply(plan_id)` runs the steps of one approved plan, in order, and nothing else. It never accepts
steps from a client: the server computes the plan from the answers, stores it, and `apply` runs
*that stored plan*. A client can only say "run the plan I reviewed, by id".

Properties it must have, each with a test:

1. **Bound, then re-checked.** Before starting, discovery is re-run and the plan rebuilt from the same
   answers; if the `plan_id` differs, the host drifted since review and `apply` refuses, saying what
   changed. Then each step re-checks its own preconditions immediately before acting
   (compare-and-swap, the `compose.write` rule: a file that changed since approval is never overwritten).
2. **Outcome is read from the host, not from exit codes.** A step is `applied`, `not_applied` or
   `unknown`, decided by reading state afterwards, exactly as the engine does (`StepOutcome.effect`).
3. **Durable and resumable.** An append-only journal records every step before and after it runs. A
   crash or a closed browser tab leaves a state `apply` can pick up from.
4. **Idempotent.** Every handler has "ensure" semantics. Running a finished plan again changes nothing;
   `plan` on the resulting host proposes only kept files.
5. **Stops on the first failure.** "NOTHING FURTHER WILL RUN" is literal. Later steps never run
   against a half-done earlier step.
6. **Undoable, explicitly.** Each reversible step records evidence at apply time; undo is a separate
   operator action, never automatic.
7. **Never a shell.** Handlers run argv lists or file operations through a small host interface.

## Execution model

```
approve(plan_id)
  lock                      exclusive flock; a second apply is refused
  re-discover, re-plan      plan_id must equal the approved one, else refuse with the drift
  for step in plan.steps:   (topological order, as built by plan())
      journal  step_started        <- fsync'd BEFORE any side effect (the engine's "dispatched")
      handler.check(host)          precondition read; may return "already satisfied"
      handler.act(host, journal)   the change; records inverse evidence before the point of no return
      handler.verify(host)         reads state back; decides applied / not_applied / unknown
      journal  step_ok | step_failed(effect, reason)
      on failure: stop
  journal  done | stopped
```

On resume after a crash, a step with `step_started` and no result is **reconciled**, not re-run:
`handler.reconcile(host)` reads the host and answers applied, not applied or unknown, the same job
`settle_crashed_execution` and `_reconcile_canary` do in the engine. `unknown` stops and asks.

### The journal

Append-only JSON lines in a root-only directory, one file per plan id, each event fsync'd:

```
{"seq":7,"ts":...,"type":"step_started","step":"s05"}
{"seq":8,"type":"evidence","step":"s05","data":{"created_file":true,"dev":2049,"ino":131077,"sha256":"..."}}
{"seq":9,"type":"log","step":"s05","line":"wrote /etc/planetexpress/config.yaml (mode 0640)"}
{"seq":10,"type":"step_ok","step":"s05","effect":"applied"}
```

- The UI reads it with `GET /events?after=N`; a reload rebuilds the whole view from it. No second
  state store, so the screen and the truth cannot disagree.
- Plain files, not the SQLite `Store`. `Store` only needs a path, so it could run without config, but its
  schema is the runbook engine's (approvals, executions, attempt limits, incidents, auth counters) and
  setup events are a different domain; sharing it would couple the two for no gain. At about thirty steps
  a log file is simpler, tailable and human-readable. A torn final line (crash mid-write) is ignored on read.
- **No secrets, ever.** Events hold secret *names*, never values. A redaction filter wraps every log
  line, and handlers never log file contents, only paths, modes and sha256 of what they wrote.
- Location: systemd `/var/lib/planetexpress-setup/<plan_id>/`; MOS keeps `/` in RAM, so it lives on the
  pool (`<pe_home>/setup/<plan_id>/`). Mode 0700, root. It also holds `plan.json` (public plan) and
  `evidence/` (backups of replaced files, 0600).

## The handler contract

One handler per catalogue kind, all written against a small `Host` interface so they can be tested
without a machine:

```python
class Handler(Protocol):
    def check(self, host, step) -> Satisfied | Proceed | Refuse     # precondition, read-only
    def act(self, host, step, journal, secrets) -> None             # the change, argv or file ops only
    def verify(self, host, step) -> Effect                          # applied | not_applied | unknown
    def reconcile(self, host, step) -> Effect                       # after a crash, from host state
    def inverse(self, host, step, evidence) -> str                  # undo, refuses if anything moved
```

`Host` exposes `lstat/read/mkdir/write_atomic/replace/unlink/chown/chmod/fsync_dir`, `run(argv)`,
`getpwnam/getgrnam`, `exists`. `RealHost` is the system; `FakeHost` is an in-memory tree with a command
recorder, plus a `FaultyHost` that fails at the Nth operation to simulate a crash between any two.

## Handlers, one by one

| kind | check (immediately before) | act | verify | inverse |
|---|---|---|---|---|
| `dir.ensure` | no symlink in the path; if it exists it must be a directory | create missing components, record which; set owner/mode on those only | stat | `rmdir` only if empty and the inode matches |
| `file.write` | CAS: `already_present` and, if present, its sha256 still what planning saw; no symlink; parent is a directory owned by root or the target user, not group or world writable | temp file in the same directory, `fsync`, owner/mode, **identity recorded**, `rename`, `fsync` the directory. `keep` and present is a no-op | read back, compare sha256, check mode and owner | restore the backup, or unlink the file *only if* its device+inode is the one this step wrote |
| `sudoers.install` | as `file.write`, plus the content must validate | write a temp file in `/etc/sudoers.d` whose name contains a dot (sudo ignores it), **`visudo -c -f` it**, then rename into place `0440 root:root` | `visudo -c` on the live file | restore or remove; never leaves an invalid file behind |
| `python.env` | python is 3.11+; venv path absent or ours | `venv`, fetch pip if asked, `pip install -r requirements.txt` | the interpreter runs and imports the declared dependencies | remove the venv only if this step created it (inode evidence) |
| `access.provision` | users and groups named in the plan | run the commands returned by `scripts/web_access.py::plan_web_access` (pure, already reviewed) for systemd; groups and modes for MOS | `id`, `getfacl` or `stat` read back | reported as **not undone automatically**; the user and group are harmless |
| `dashboard.init` | env file sha256 if present | build values with `dashboard_operators.apply_operator_change` (pure), hash the passphrase, write via the `file.write` path | `web_auth.load_operators` parses what was written | restore previous content |
| `service.install` | unit absent, or present and `keep` | write via `file.write`, then `systemctl daemon-reload` (sysvinit: init script and `/etc/default`) | `systemctl show -p LoadState` is `loaded` | remove the file, reload |
| `service.enable` | the unit exists and loads | `systemctl enable [--now]`; sysvinit: run the script's `start` | `is-enabled`, `is-active` read back | `disable`, and `stop` only if this step started it |
| `boot_hook.install` | existing hook content | **merge a marked block**, never replace the file (see below) | read back, block present once | remove the block |
| `state.snapshot` | an install exists | `scripts/state_snapshot.py create --label pre-setup` (stdlib-only, as `deploy.sh` does) | the snapshot path exists | none; it *is* the safety net |
| `verify.smoke` | n/a | run Leela's status scan **as the service user, not root**, with a minimal environment | exit and parsed output | n/a |

### Secrets in `act`

Placeholders are substituted in memory at the last moment. The secrets file is created from
`mkstemp` (0600) so no byte is ever world-readable, even transiently. The passphrase is hashed (scrypt)
by the handler and only the hash is stored. The TOTP secret has to be stored, because the dashboard
needs it, which is why that file is root-only. Python cannot guarantee zeroing memory; the server drops
its references when apply ends.

## Failure policy

- **Stop and offer, never auto-rollback.** A half-installed host is usually easier to finish than to
  rewind, auto-rollback destroys evidence, and it could revert something an operator edited meanwhile.
  This is also what the engine does: only canary updates carry a built-in inverse.
- **Retry step.** Re-runs the handler, which re-checks its preconditions. Safe because of idempotence.
- **Back to plan.** Changing an answer gives a new plan id; steps already satisfied are recognised by
  their `check` and skipped.
- **Undo.** Reverse order over *this apply's* completed reversible steps, each validated against its
  evidence, and it stops at the first refusal with a list of what remains. A file changed since the
  step wrote it is **never** reverted over (the `restore` rule). Steps that cannot be undone are named.

## Trust boundary of the server (detail in a later doc, decided here because `apply` depends on it)

- One process, root, but it holds no authority a client can widen: it computes and stores the plan,
  and the only mutating calls are `approve(plan_id)`, `retry(step)` and `undo()`. Paths and step
  contents never arrive from the browser.
- A one-time token in the URL is exchanged for an `HttpOnly`, `SameSite=Strict` cookie. Peers must be on
  private addresses, `Host` must match what the server bound (against DNS rebinding), and `Origin` is checked on
  every POST. It exits when the wizard finishes or the token expires.
- **Transport is a real decision (D1 below).** Over plain HTTP on the LAN, the token, the Telegram
  token and the passphrase cross the wire readable by anyone on the segment.

## Plan changes `apply` needs first (slice A0): done

Found while designing, and now built in `plan` and `discover`:

1. **`state.snapshot` step** for an existing install, first in the plan, and every other step depends on
   it, so nothing changes before the safety net exists (`deploy.sh` does the same).
2. **Compare-and-swap is in the type.** `file.write`, `sudoers.install` and `service.install` carry
   `expect_absent` or `expected_sha256`, and a `replace` without one cannot be constructed. `discover`
   records the sha256 of every present install file (`existing_pe.sha256`, a byte-exact hash, size-capped).
   A file that exists but could not be hashed is kept, with a warning, rather than replaced unseen.
3. **Boot hook merge.** `boot_hook.install` merges a `# BEGIN planetexpress` block into each hook file and
   never replaces it. The blocks are functions that `return`; a block containing `exit` cannot be built,
   because it would skip the operator's own commands after it. A hook file that is byte-for-byte one of
   the whole-file versions this project once shipped (`LEGACY_HOOK_SHA256`, from git history) is entirely
   ours and is replaced; anything else is merged into. Exact hashes, never a guess from the content.
4. **pip bootstrap.** Unchanged and recorded: `get-pip.py` comes from the network unpinned, and
   `requirements.txt` has no hashes, exactly as `deploy.sh` does today. `--require-hashes` is future
   hardening, and the plan already warns that the host needs internet.

## Testing

- **Handler tests on `FakeHost`:** each handler's check, act, verify and inverse, including refusal when the
  file changed, a symlink appears in the path, or the parent is writable by others.
- **Crash injection:** `FaultyHost` fails at every operation index of every handler; after reconcile and
  resume, the host is either unchanged or complete, never torn, and the journal agrees with it.
- **Properties:** apply then inverse restores the original tree; apply twice is a no-op; plan, apply,
  discover, plan again proposes nothing new.
- **No secret anywhere** in journal, logs or events, asserted by scanning them for the answers' values.
- **Real hosts.** The pattern that found real bugs in `discover` and `plan`: a fresh MOS VM from a copy of the
  image, and a disposable Ubuntu VM (cloud image under libvirt), each taken through the whole plan; then adopt
  on a throwaway clone of the live configuration. Never the live host itself.

## Slices

| | scope |
|---|---|
| **A0** | the plan and discover changes above |
| **A1** | `Host` (real and fake), journal, executor with drift check, lock, resume; handlers `dir.ensure`, `file.write`, `verify.smoke`; CLI `apply` |
| **A2** | the remaining handlers: services, `sudoers.install`, `python.env`, `access.provision`, `dashboard.init`, `boot_hook.install`, `state.snapshot` |
| **A3** | undo, retry, reconcile after a crash, `FaultyHost` sweep |
| **A4** | the server and wiring the screens (own design doc) |

Every slice that touches `file.write`, `sudoers.install` or privilege goes through the Codex second
review before it counts as done (`CLAUDE.md`).

## Decisions needed

- **D1, transport.** Self-signed HTTPS with the fingerprint printed in the terminal (my recommendation);
  or serve on `127.0.0.1` only and reach it through an SSH tunnel; or plain LAN HTTP and accept that the
  token, the Telegram token and the passphrase are visible on the segment.
- **D2, failure policy.** Stop and offer undo (recommended), or automatic rollback.
- **D3, privilege model.** One root process with a server-held plan (recommended for a first-run tool that
  runs for minutes), or a split into an unprivileged web process and a root helper over a socket.
- **D4, boot hook.** Merge a marked block (recommended) or keep replacing the file.
- **D5, journal.** Where it lives (above) and that it is kept until uninstall, with backups of replaced
  files inside it.
- **D6, validation targets.** OK to create disposable VMs on this machine: a fresh MOS from the image,
  and an Ubuntu cloud-image VM.
