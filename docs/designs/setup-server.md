# Setup server (slice A4): the browser wizard's backend

Status: design for review, v3-next, 2026-10-08. Builds on `setup-plan.md` (discover and plan, built) and
`setup-apply.md` (apply and undo, built). The screens are in `planet_express_design_v23/handoffs/V3.2-SETUP-WIZARD.md`.
Decisions already locked in and not reopened here: self-signed HTTPS (D1), stop and offer undo (D2), one root
process holding the plan (D3), journal as the only state (D5).

## What this adds

A small HTTPS server, run by `setup.sh` as root, that lets a browser on the LAN drive exactly what the CLI
drives today: `discover`, `plan`, `apply`, `undo`. It adds no capability of its own. Every mutation still goes
through `apply()` and its handlers; the server only decides **when** to call it and **what answers** to give it.

```
browser ──HTTPS──▶ setup server (root, one process)
                     ├─ routes (Flask): pages + JSON, one page per stage
                     ├─ SetupSession: answers, the reviewed Plan (with its secrets), operator-TOTP enrolment state
                     ├─ worker thread: apply() / undo(), one at a time (apply already takes the lock)
                     └─ planet_express.setup: discover / plan / apply / undo / journal   (unchanged)
```

## Trust boundary

- **Transport.** HTTPS with a certificate generated at start (in-process with `cryptography`, so there is no
  dependency on an `openssl` binary), valid for the host's LAN addresses and name. The terminal prints the URL and
  the certificate's SHA-256 fingerprint; the browser warning is expected once and the fingerprint is how a careful
  operator checks it. The key never leaves memory except to a 0600 file in a root-only runtime directory that is
  removed on exit.
- **Entry.** `setup.sh` prints `https://HOST:PORT/?t=<token>`. The token is 256-bit random, lives in memory, and has
  a 15-minute life that renews while the wizard is in use and is capped at 2 hours. The first request with it is
  exchanged for an `HttpOnly; Secure; SameSite=Strict` cookie and redirected to a URL without the token, so it
  does not sit in history, logs or a Referer. The token is single use: a second exchange is refused.
- **Every request.** The cookie must be valid; `Host` must be one of the names the server was started with
  (DNS-rebinding defence); every POST must carry a matching `Origin` and a CSRF value bound to the session; the
  peer must be on a private address (RFC 1918, loopback, link-local, ULA). Anything else is a 403 that does not
  say which check failed.
- **Authority.** The browser can only say: *set this answer*, *review*, *approve plan_id*, *retry step*, *undo*,
  *test Telegram*, *verify this TOTP code*. It never sends a path, a command, a step or a file's contents. The server
  builds the plan from its own `discover()` and the answers, and `approve` must quote the `plan_id` the person
  was shown; a different one is refused. This is what makes a root process safe to drive from a browser.
- **Exposure.** The server binds to the specific private addresses of this host, never `0.0.0.0`. A background
  check (every 10 s, plus before each page) reuses `discover`'s exposure logic (a non-private address on any
  listening interface, ignoring docker and libvirt bridges) and exposes `reachable_from_outside`. While it holds the
  banner shows and `approve` is refused, so a root installer is never left open on a public interface.
- **Lifetime.** The server exits when the wizard finishes, the token's hard cap passes, or the operator presses
  Ctrl-C. It never starts at boot and is not a service.
- **Secrets.** Typed once into an HTTPS form, held in `SetupSession` memory, passed to `plan()` as it already
  expects, and written only by the handlers to root-only files. They are never returned by any route, never
  logged, never in the journal (the redactor already masks every value). A reload shows masked placeholders. The
  session is dropped when the process ends.

## Routes

Each stage is a route, so a reload lands on the same step. State is the **session answers plus the journal**:
nothing else is stored, and after a server restart the journal tells a returning browser where the install got to.

| route | method | purpose |
|---|---|---|
| `/` | GET | token exchange, then redirect to the first unfinished stage |
| `/stage/<name>` | GET | server-rendered page for one of the ten stages (the dashboard's Jinja and `pe-*` CSS) |
| `/api/state` | GET | session summary: story, stage, `reachable_from_outside`, token life, whether an apply has run |
| `/api/discover` | POST | re-run `discover()`; returns checks, pools, stacks (the data contract in the handoff) |
| `/api/answers` | PUT | validate a partial answer set against `SetupAnswers`; returns field errors, never echoes a secret |
| `/api/plan` | POST | build the plan from the current answers; returns the public plan (secrets masked) and its `plan_id` |
| `/api/telegram/find-chat` | POST | one `getUpdates` call with the token, on button press (not a background poll); returns the masked chat id |
| `/api/telegram/test` | POST | send one test message |
| `/api/operator/totp` | POST | generate a TOTP secret held in the session; returns the otpauth URI and a server-rendered QR |
| `/api/operator/verify` | POST | check one code with `web_auth.verify_totp`; sets `verified` on the session |
| `/api/llm/check` | POST | optional one-token request to the chosen provider to prove the key works |
| `/api/apply` | POST `{plan_id}` | start `apply()` on the worker; 409 if one is running; refused if exposed or the id is stale |
| `/api/events` | GET `?after=N` | the journal's events after sequence N (the log and the step rail) |
| `/api/retry` | POST `{step}` | re-run `apply()` for the same plan: finished steps skip, the failed one re-checks and retries |
| `/api/undo` | POST | start `undo()` for the applied plan |
| `/api/verify` | GET | the final checks (same shape as discover's), for the done screen |

**Polling, not SSE.** The browser polls `/api/events?after=N` every second. The journal already is an ordered,
append-only, human-readable log with sequence numbers, a reload rebuilds the whole view from it, and polling needs
no streaming support from the WSGI server. This answers the handoff's question 4.

## Decisions on the handoff's open questions

1. **Commands stay out of the UI.** The plan shows file contents (secrets masked) and the step's `description`, as
   the render does. No argv reaches the page; the existing design-system guard test covers it.
2. **Token: both.** In the URL once, then a cookie. The countdown comes from `/api/state`.
3. **Exposure probe:** the server runs it, as above, and publishes `reachable_from_outside`.
4. **Polling** (above).
5. **Recovery codes do not exist** in `web_auth`, and inventing a second credential here would be a security
   feature nobody reviewed. Drop the card. The recovery path is the one that already exists: another operator
   re-enrols you with `dashboard_operators`. The operator screen says so.

## TOTP enrolment

The secret is generated server-side (`web_auth.new_totp_secret`), shown once as a QR and a manual code, and held
in the session. The operator must enter a valid code before **Next** is enabled; that code proves they enrolled
the right device, so a typo in the manual code cannot lock them out of their own dashboard. The secret is passed
to `dashboard.init` through the plan's secret mechanism like every other secret. QR images are generated
server-side as PNG (the `segno` library, pure Python, no Pillow) and served as a data URI: nothing is fetched from
a third party and no SVG is introduced.

## Apply from the browser

`/api/apply` re-runs `plan()` and compares ids (`apply()` already does this inside its lock), so a host that
changed between review and click is refused with the reason. The worker thread runs `apply()`; the page polls the
journal. A failure shows the exact reason first (the handoff's rule), what did and did not change, and offers
**Retry step** (same plan) or **Back to plan** (new answers give a new plan id; satisfied steps are recognised).
**Undo** is offered when something was applied, and reports what could not be undone (accounts, the snapshot).

## Story coverage

| story | what the server needs beyond the routes |
|---|---|
| Fresh, MOS | pool picker from `discover.storage`; blocked-without-pool stage with the MOS API instructions (guide, not perform) |
| Fresh, Ubuntu | blocked-without-Docker stage with a re-check button |
| Adopt homelab | observe-only default; per-stack MANAGE/WATCH/IGNORE written to `forbidden_stacks`; ingress stack cannot be MANAGE |
| Repair / uninstall | `discover.existing_pe` selects it; repair shows a diff-only plan; uninstall runs the undo of the recorded apply, keeps the snapshot |

## Bootstrap: `setup.sh`

The wizard has to run before PE's own virtualenv exists, and `plan()` needs pydantic and PyYAML. `setup.sh`
(POSIX sh, tiny, no dependencies):

1. checks it is root and Python is 3.11+ (says exactly what is missing and how to get it, and stops);
2. creates a throwaway virtualenv in a root-only temp directory and installs a **pinned subset** (`pydantic`, `PyYAML`,
   `Flask`, `cryptography`, `segno`) with hashes, so the installer's own dependencies are not unpinned downloads;
3. runs `python -m planet_express.setup serve`, which prints the URL and fingerprint and serves until done;
4. removes the temp virtualenv on exit.

MOS has no pip, so the same script uses the get-pip bootstrap that `python.env` already does. The CLI
(`discover`, `plan`, `apply`, `undo`) keeps working without the server, for automation and for tests.

## Slices

| | scope |
|---|---|
| **A4a** | `serve` command, TLS, token and cookie, Host/Origin/CSRF/peer checks, exposure check, `/api/state`; tests for every refusal |
| **A4b** | discover/answers/plan routes and the first six stages (Welcome to Operator account) with the real CSS |
| **A4c** | apply, events, retry, undo; the Install and Done stages; the journal-rebuilds-the-view reload test |
| **A4d** | Telegram find-chat/test, TOTP enrolment, LLM check |
| **A4e** | `setup.sh`, repair/uninstall, and the real-VM runs (fresh MOS clone, disposable Ubuntu cloud-image VM) |

Each slice goes through the Codex second review. A4a is the one that matters most: it is a root process with an
HTTP surface, so the review is of the checks above, not of the screens.

## Tests

- **Every refusal:** no cookie, wrong cookie, replayed token, wrong `Host`, missing or foreign `Origin`, missing CSRF,
  a public peer address, an expired token, an exposed host. Each returns 403 without saying which.
- **Authority:** no route accepts a path, argv, step or file content; `approve` with a stale or invented `plan_id` is
  refused; two `apply` calls give one run and a 409.
- **No secret anywhere:** after a full run with distinctive secrets, scan every response body, the journal and the log
  output for them.
- **Reload:** kill the browser state mid-install, reload, and the view equals the journal.
- **A real browser pass** (the in-app browser) over each story against the fake host, then on the VMs.
