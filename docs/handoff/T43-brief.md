# Task T43 — slice 5b-4: `compose.write` with staged bindings; `/install` and Amy's edits

Branch `v2`. Spec: `docs/designs/slice-5b-multistep-execution.md` §4.2 (staged bindings), §4.6
(`compose.write`), §5 (flow mapping) and §7 (landing 5b-4), decision D35 (`/install` may only create
new LAN-only stacks). Builds on 5b-3.

## Why
Compose edits are the last write path outside the engine. Today `/install` (Fry) and Amy's edits go
through `casa_bender.propose_compose_diff` / `apply_pending_diff`: a JSON file of pending diffs, a
separate approval vocabulary (`approve_diff:<id>`), a plain `write_text` with a `.bak.<ts>` copy,
and **a second, unrelated approval** to start the stack afterwards. After this landing a compose edit
is a typed step in a runbook, and one approval covers the write and the start.

## Required behaviour

1. **`compose.write` step type** (`runbook.py`), R3, rollback `conditional`, target `stack`.
   - Params: `{stack, content_sha256}` plus **exactly one** of `expected_old_sha256` (edit) or
     `expected_absent: true` (new stack). The content itself lives in the runbook's `artifacts`
     under its sha256, which T38 already validates (key = digest of content, no unused artifacts).
   - Binding: the resolved compose path under `stacks_root`, with **no symlink anywhere on the
     path**, plus whether the stack directory existed at proposal time.
   - Outputs: `{compose_path, compose_sha256}` so later steps in the same runbook can bind to what
     this step actually wrote.
2. **The write itself** (engine), immediately before which the expectation is re-checked
   (compare-and-swap; a file that changed since proposal fails the step **without writing**):
   record `pre_state` **first** (backup path and sha, whether the directory existed, and its inode
   if this step creates it), then write a temp file in the same directory, `fsync`, preserve mode,
   owner and ACL of the replaced file, `rename`, `fsync` the directory. `.env` files are never
   written — secrets stay human-only, and a params value naming one is a validation error.
3. **The inverse** (conditional, so ROLL BACK offers it): restore the recorded backup for an edit;
   for a new file, remove it **and** the directory only if this step created it, it is the same
   directory (inode), and it is empty. Either way only if the file still holds the content this
   step wrote — a human edit since then is never silently reverted.
4. **Staged bindings** (§4.2): a new stack has nothing to bind at proposal time. A `compose.write`
   with `expected_absent` binds an expected-absent path; later steps on that stack bind to this
   step's output (`{"from_step": n, "output": "compose_path"}`) and to the service names parsed from
   the approved artifact at proposal time. They resolve only after the write step passed and the
   file's sha equals the artifact's. Container identity for later `check.container` steps comes from
   the `stack.up` step's outputs, which already fail on a missing, duplicate or replaced container.
5. **Proposal-time rules, on the parsed artifact** (not on the model's prose): forbidden stacks
   refused; for a new stack, the name charset (`[a-z0-9][a-z0-9-]*`) and D35's LAN-only domain rule
   — a router rule naming anything outside `lan_only_domain` is refused; path containment under
   `stacks_root` with no symlinks.
6. **Flows**
   - **`/install`** builds `[compose.write(expected_absent), stack.up, check.container…]` as one
     runbook with one approval and one card showing the diff. New LAN-only stacks only (D35).
   - **Amy's compose edits** build `[compose.write(expected_old_sha256), service.restart|stack.up,
     check.container]` the same way.
   - `pending_diffs.json` is not written by the typed path; the legacy `propose_compose_diff` /
     `apply_pending_diff` / `approve_diff:` callback stay behind `legacy_plans_enabled` until 5b-5,
     exactly as the legacy plan path does.

## Tests
Params validation (both expectations, neither, `.env`); artifact/sha agreement; CAS refusal when the
file changed; the atomic write (temp + rename, mode/owner preserved, directory fsync); the inverse
for an edit, for a created file, for a created directory, and its refusals (content changed since,
directory not empty, different inode); staged bindings resolving from the write step's output;
`/install` refusing a non-LAN domain, a forbidden stack and an existing stack; one approval covering
write + start; the legacy path unchanged behind the switch.

## VM rehearsal
`/install` a new LAN-only stack end to end (card → approve → write → up → check), an Amy-style edit
of a fixture stack, a CAS refusal (file edited between proposal and approval), a rollback of both an
edit and a new-stack write, and a kill between the write and the `stack.up`.

## Out of scope
Deleting the legacy diff path, `pending_diffs.json`, `_run_command`, `shell=True`, `_safety_check`
and the legacy switch (all 5b-5).
