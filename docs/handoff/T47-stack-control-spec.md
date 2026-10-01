# T47 · Stack control, and an elevated operator — retiring dockge

Written 2026-09-29 against the code at `v2.3.0`. Everything below was read out of the
implementation, not carried over from a design package; where the v22 package's
`V2.4-STACK-CONTROL.md` says something different, this is what the code does.

**Owner decision, 2026-09-29: R2 and R3 from the dashboard, behind an elevated session.**
Telegram keeps everything it does today, but stops being the only way to authorise anything.

## What already exists

The execution engine needs nothing added. Every action dockge performs is already a typed step
with a params model, a risk class, a verifier and an inverse:

| step | risk | what it is |
|---|---|---|
| `stack.up` | R1 | bring a stack up |
| `stack.down` | R2 | take a stack down |
| `stack.up_all` / `stack.down_all` | R2 / R3 | every stack |
| `service.start` / `stop` / `restart` | R1 / R2 / R1 | one service |
| `compose.write` | R3 | the compose editor. Compare-and-swap, symlink refusals |
| `compose.restore` | R3 | its inverse, with device+inode evidence before deleting |

Approval parity is further along than it looks. The dashboard already **approves and denies**
runbooks (`/api/approvals/<id>/decide`), with attribution. What no surface can do today is
*originate* above R1: `dashboard-direct` and `telegram-direct` are both bounded by
`AUTONOMY.direct_request_risks`, which defaults to `["R1"]`, and the live host has no `autonomy:`
block — so that default is what is running.

**So today an operator can start a stack from either surface, and stop one from neither.**

## What this spec adds, stated plainly

Operator-originated R2 and R3. That is a new capability, not a shortcut to an existing one:
approving a runbook the brain proposed is bounded by what was proposed, while originating one is
bounded only by the step catalogue. It deserves its own gate rather than inheriting the session's.

## The elevated session ("sudo")

A signed-in operator can do R0 and R1 as today. R2 and R3 require an **elevated session**, which
is exactly sudo's bargain: prove again, briefly, that it is you.

**Re-enter the passphrase. Not TOTP.** TOTP means reaching for the phone, and the point of this
work is that the phone stops being the control. Passphrase re-entry defeats the threat that
actually exists here — a walked-up-to browser holding a 30-day device-trust cookie — and the
material is already there (`web_auth.hash_passphrase`, per-operator hashes in the environment).

Shape:

- On an R2/R3 request without elevation, the API answers `403` with a typed
  `elevation_required`, and the surface prompts in place. Never a redirect: these arrive from
  `fetch`, and a redirect would land HTML in a JSON handler.
- Success mints a short-lived elevated marker: signed, and **bound to the current device token's
  sha256 and the operator's epoch**, the same way the trust cookie already binds. A stolen
  marker on another session is worthless, and bumping an operator's epoch revokes it with
  everything else.
- **10 minutes**, refreshed on each successful elevated action, with a hard cap of 60 minutes so
  a long editing session cannot become a permanently elevated one.
- Logout clears it. So does a passphrase change.
- Verified server-side on every elevated action — signature, binding, expiry, and the operator
  still being in `operators` — never by the presence of a cookie.

**No roles, no admin account.** Operators are a short, environment-defined list, and every one of
them can already authorise an R3 through the approvals surface. Adding a permission model to gate
a thing they can already reach by another route would be ceremony, not security. If a read-only
operator is ever wanted, that is an additive change and its own spec.

**Audit.** An elevated origination records the operator, the step, the target and the time, the
same way an approval decision does today. The point of removing the Telegram round-trip is to
remove the round-trip, not the record.

## Policy change

`AUTONOMY.direct_request_risks` moves from `["R1"]` to `["R1", "R2", "R3"]`, and the elevation
requirement becomes the thing that distinguishes them. R4 stays forbidden — the schema refuses to
let it be anything else.

Two consequences to be deliberate about:

- This raises the ceiling for **`telegram-direct` as well**, since it is the same value. If
  Telegram should stay at R1 while the dashboard goes to R3, the ceiling has to become
  per-origin. Recommended: make it per-origin. An elevated session is a thing the dashboard can
  express and a chat message cannot, so the two surfaces should not share one number.
- `AUTONOMY` is editable from the Config tab. A config edit is itself a mutation that lands in
  the same policy — check that raising your own ceiling is not a thing an unelevated session can
  do. If it is, that is the whole gate undone in one step.

## The compose editor

The riskier half, and separate from the ceiling.

- It edits **one** file: the compose file of the stack whose drawer is open. Not a file browser.
  The path comes from the stack binding, never from the request.
- `compose.write` already does a compare-and-swap against the sha256 it was proposed with, and
  fails closed on a symlink. The surface must show that refusal as a **conflict** — someone else
  changed this — and not as an error.
- A write does not chain into `stack.up`. A compose file that fails to parse should not also take
  the stack down with it; the operator chooses.
- Out of scope: creating a stack, deleting a stack, or touching anything outside
  `/home/casaroot/stacks/<stack>/docker-compose.yml`.

## What it must not do

- No arbitrary path. The target is bound at proposal time and re-checked immediately before the
  step acts. The surface does not get to opt out of that.
- No new step type. If something here seems to need one, it is a different spec: adding a step
  type adds a capability, and the catalogue is the entire contract.
- No bypass of the host-mutation lock. Stack control serialises against scans like everything
  else.
- No elevation inherited by the brain. `planner`, `incident` and `zoidberg` origins are unchanged
  by all of this; an operator elevating does not make the autonomous paths more capable.

## Suggested shape

1. **Elevation first, with nothing using it.** The marker, the `403 elevation_required` contract,
   the prompt, expiry and revocation, tested on its own. It is the security boundary, so it lands
   and gets gated before anything depends on it.
2. **Per-origin ceiling** in `AutonomyConfig`, defaulting to today's behaviour, plus the check
   that an unelevated session cannot raise it.
3. **Stack control in the v2.2 drawer**: UP / DOWN on the header, START / STOP / RESTART per row,
   each proposing through the existing path.
4. **COMPOSE view** in the same drawer: read, edit, save through `compose.write` at the sha it was
   read at, conflict surfaced as a conflict.
5. **Remove dockge** — container and route — the way Homepage went: a prepared script, one
   command, handed over and never run by an agent.

Screenshot the drawer before and after, as the design package asks.

## Still open

- Per-origin ceiling, or one number for both surfaces? (Recommended: per-origin.)
- Should R3 additionally require TOTP, accepting the phone round-trip for the compose editor
  only? (Recommended: no — it reintroduces exactly what this work removes.)
