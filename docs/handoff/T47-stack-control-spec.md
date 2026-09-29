# T47 · Stack control — retiring dockge

Written 2026-09-29, against the code as it stands at `v2.3.0`. Everything below was read out of
the implementation rather than carried over from a design package; where an earlier handoff
(`V2.3-STACK-CONTROL.md`, in the v22 design package) said something different, this is what the
code actually does.

## What already exists

The execution engine needs nothing. Every action dockge performs is already a typed step with a
params model, a risk class, a verifier and an inverse:

| step | risk | what it is |
|---|---|---|
| `stack.up` | **R1** | bring a stack up |
| `stack.down` | R2 | take a stack down |
| `stack.up_all` / `stack.down_all` | R2 / R3 | every stack |
| `service.start` / `stop` / `restart` | R1 / R2 / R1 | one service |
| `compose.write` | R3 | the compose editor. Compare-and-swap, symlink refusals |
| `compose.restore` | R3 | its inverse, with device+inode evidence before deleting |

The approval path exists too. `dashboard-direct` is already a direct-run origin, bounded by
`config.AUTONOMY.direct_request_risks`, which defaults to `["R1"]` — and the live host has no
`autonomy:` block, so that default is what is running.

**So a signed-in operator can already start a stack without approval, and cannot stop one.**
That asymmetry is the whole of what is left, and it is a policy value, not a feature.

## What is missing

Surface. The dashboard can start exactly one mutation today — a container restart, through its
own route. Nothing in the UI proposes a `stack.up`, `stack.down` or `compose.write` runbook. The
v2.2 drawer already shows a stack and its containers, which is where these belong.

## The decision this spec exists for

Dockge starts and stops a stack on a click. Planet Express requires approval for every mutation
above R0. Replacing dockge means choosing, explicitly, one of:

**A. Leave the ceiling at R1.** Start-a-stack is a click; stop-a-stack raises an approval card in
Telegram. Honest, and no policy change — but "stop" going to the phone while "start" does not is
the kind of asymmetry people work around, and working around an approval is worse than not having
one.

**B. Raise `direct_request_risks` to `["R1", "R2"]`.** A signed-in operator's click is the
approval for start *and* stop. The dashboard's auth is not thin — passphrase, TOTP, device trust,
CSRF, per-operator epochs — and a stack the operator is looking at is a target they have already
identified. R3 stays on the approval path.

**C. Raise it to R3 as well**, putting the compose editor on the same footing. Not recommended:
`compose.write` is the one step that changes what a stack *is* rather than what it is doing, and
it is the step whose blast radius does not fit in a button's worth of context.

Recommendation: **B**, with `compose.write` deliberately left above the line. That makes the
drawer a real replacement for dockge's day-to-day use, and keeps the one irreversible-ish
operation behind a second pair of eyes.

This is a policy value in `config.yaml`, editable from the Config tab, so it is also reversible
without a deploy — which is a good reason to pick it deliberately rather than discover it.

## The compose editor

Separate question from the ceiling, and the riskier half.

- It edits **one** file: the compose file of the stack whose drawer is open. Not a file browser.
  The path comes from the stack binding, never from the request.
- `compose.write` already fails closed on a symlink and does a compare-and-swap against the
  sha256 it was proposed with, so two operators editing at once cannot silently overwrite each
  other. The surface must show that refusal as a conflict, not as an error.
- A write is followed by the operator choosing whether to bring the stack up; the spec should not
  chain them automatically, because a compose file that fails to parse should not also take the
  stack down with it.
- Out of scope: creating a new stack, deleting a stack, editing anything outside
  `/home/casaroot/stacks/<stack>/docker-compose.yml`.

## What it must not do

- No arbitrary path. The stack is bound at proposal time and re-checked before the step acts;
  that is the existing contract and the surface does not get to opt out of it.
- No new capability. If something here seems to need a new step type, that is a separate spec —
  adding a step type adds a capability, and the step catalogue is the entire contract.
- No bypass of the host-mutation lock. Stack control serialises against scans like everything
  else.

## Suggested shape

One slice, in the v2.2 drawer:

1. The drawer header gains UP / DOWN for the stack, and each row gains START / STOP / RESTART.
   Each proposes a runbook through the existing path; the ceiling decides whether it runs or
   raises a card.
2. A COMPOSE view in the same drawer: read the file, edit, save through `compose.write` with the
   sha it was read at, and surface a conflict as a conflict.
3. Remove the dockge container and its route, the way Homepage was removed
   (`/tmp/pe-deploy-*`-style script, prepared and handed over, never run by an agent).

Screenshot the drawer before and after, as the design package asks.

## Open question for the owner

Which ceiling — A, B or C? Everything else here follows from it, and nothing should be built
before it is answered.
