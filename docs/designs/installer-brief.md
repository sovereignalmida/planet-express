# Planet Express installer: design brief (for Claude Design)

Status: draft for review. Branch: `v3-next`. Written 2026-10-08.

## 1. What we are designing

A first-run **setup experience** for Planet Express (PE): a guided, browser-based wizard that takes a
host from "nothing installed" (or "a homelab already running") to "PE installed, configured,
verified, and the dashboard open". Today the install is `deploy.sh` plus a 700-line terminal Q&A
(`scripts/setup_wizard.py`), with a separate path for MOS. It asks questions the machine could
answer itself, previews nothing, and cannot be resumed.

The wizard must feel like the dashboard: same cockpit design system, same crew. It is not a
separate product.

## 2. Who leads: the crew

The codebase already has the cast, and they map onto the three stages of the work.

| Stage | Character | Why |
|---|---|---|
| Host and narrator | **Fry** | Already PE's onboarding character (`casa_fry.py`, the Telegram `/install`). The friendly guide who walks you through. |
| Discover | **Leela** | She is the scanner. "Leela is scanning your ship." |
| Plan and review | **Farnsworth** | The planner. He shows the plan and asks for approval: "Good news, everyone, here is the plan." |
| Apply | **Bender** | The executor. Runs the approved steps, one visible step at a time. |
| Final report | **Hermes** | Files the "what was installed" report. |

Fry is the voice you hear throughout. The other characters take over at their stage, shown with the
existing crew portraits (`assets/`). Keep the humour light and never in the way of an error
message: when something fails, the text is plain and exact first, the quip second.

## 3. Principles

1. **The machine answers what it can.** Discover first, ask second. Every question shows what was
   detected and lets the user override it.
2. **Nothing changes until the plan is approved.** The plan is a reviewable list of exactly what will be
   created, written or started, with the file content or command visible.
3. **No silent installs.** Docker, users, sudoers entries and init scripts are listed in the plan and
   confirmed. Where PE can only guide (installing Docker on Ubuntu), it says so.
4. **Idempotent and resumable.** Close the tab mid-install, come back, continue where it stopped.
5. **Adopting a homelab is observe-only by default.** PE does not touch existing stacks until the
   user opts in per stack.
6. **Reversible where possible.** Each plan step shows whether it can be undone.
7. **Security of the wizard itself.** It runs as root and writes sudoers, so see section 7.

## 4. The four stories

### A. First time on MOS, from nothing
The reference VM: a fresh MOS install, no pool, no stacks. Facts the wizard must handle:
- MOS keeps `/` in **RAM**. PE and its data must live on a **persistent pool**. If no pool exists,
  the wizard guides creating one (the MOS API can do it) before anything else.
- PE is restored at boot by MOS hooks in `/boot/optional/scripts` (`post-start.sh`,
  `shutdown.sh`), not by `/etc`.
- Compose is the standalone `docker-compose`, not `docker compose`.
- Docker must be enabled and pointed at the pool through the MOS API.
- First stack: offer a template (nginx or similar) so the dashboard has something to show.

### B. First time on Ubuntu, from nothing
systemd host, maybe no Docker. Detect Docker and Compose; if missing, guide the install and wait.
PE installs systemd units, a service user, sudoers entries and the config file.

### C. Adopting an existing homelab (the "live host" story)
Docker is running, dozens of stacks, mounts, Traefik, backups. Discovery reads it all (stacks root,
compose files, mounts, exclusions, backup jobs) and proposes a config for review. Default is
**observe-only**: monitoring and the dashboard, with stack control, auto-update and prune each
switched on deliberately. Clearly separate "PE will read" from "PE will change".

### D. Re-run, upgrade, repair, uninstall
Same wizard, different entry. Shows current state, diffs the proposed change, and offers repair
(for example "the dashboard user is missing") and a clean uninstall plan.

## 5. Screens (each needs empty, loading, success, error and blocked states)

1. **Welcome.** Fry. Detected host in one line ("Ubuntu 24.04, systemd") and the path chosen
   (fresh install, adopt, repair). One button to continue.
2. **Scan results** (Leela). A checklist of what was found, each row ok, warning or blocked, with a
   one-line reason and a fix or override: OS and init system, Docker and Compose, persistent storage
   (MOS), stacks found, mounts, running containers, existing PE, network and ports, sudo and
   privileges.
3. **Where PE lives.** Install path and persistence (the MOS pool picker or create-pool flow), stacks
   root, and which stacks PE manages versus only watches, with forbidden and excluded stacks.
4. **What PE may do.** Choose capability levels: observe only, restart containers, stack up and down,
   canary auto-update, safe prune. Each shows what sudo or host access it needs.
5. **Telegram.** Create a bot (BotFather steps), paste the token, then **detect the chat ID live**
   ("send any message to your bot now") and send a test message. Skippable, with the consequence stated.
6. **Operator account.** Name, passphrase, and TOTP enrolment with a QR code and a manual-entry code.
   Verify with one code before continuing. Recovery information shown once.
7. **LLM (optional).** Provider and key, or skip with the consequence stated.
8. **Review the plan** (Farnsworth). The full change list grouped by area. Each step: title, risk
   class, reversible or not, and an expandable file or command preview. Approve all or step through.
9. **Install** (Bender). Live progress step by step, streamed log per step, per-step retry on
   failure, a clear "nothing further will run" state when stopped. Survives reload.
10. **Verify and done** (Hermes). A report: services up, a test Telegram message sent, dashboard
    reachable, first scan complete, and the three things to do next. A button opens the dashboard.
11. **Repair and uninstall** variants of 8 to 10.

A persistent side rail shows progress through the stages and where the user is.

## 6. Data contract the screens render

The wizard is a client of three headless operations, all JSON. Design to these shapes; the backend
will be refactored to produce them.

`discover` returns a report:
- `host`: os, version, init_system (`systemd` or `mos`), arch, hostname
- `docker`: installed, version, compose_flavour (`plugin` or `standalone`), root_dir
- `storage`: persistent (bool), pools [name, mount, free], candidate_install_paths
- `stacks`: [name, path, services, running, forbidden_suggested]
- `mounts`, `containers_running`, `ports_in_use`, `existing_pe` (version, config_path)
- `checks`: [id, label, status (`ok`, `warn`, `blocked`), detail, fix, overridable]

`plan` takes the user's answers and returns:
- `steps`: [id, kind, title, target, risk (reuse the runbook risk classes), reversible, preview,
  needs_root, depends_on]
- `warnings`, `summary` (counts by kind), `will_not_touch` (the observe-only guarantees)

`apply` streams events: `step_started`, `log` (line), `step_ok`, `step_failed` (reason, retry
allowed), `done`, each carrying the step id so a reloaded page can rebuild the view. `verify`
returns the same check shape as discover, for the final report.

## 7. Security constraints the design must reflect

- Setup mode runs as root and writes sudoers and init scripts. It is reached by a **one-time token
  URL printed in the terminal**, is valid for a limited time, and exits when the wizard finishes.
  The UI should say plainly when it is open and how to close it.
- It listens on the LAN only, never port-forwarded; the welcome screen warns if it is reachable
  from outside.
- Secrets (bot token, passphrase, TOTP secret, LLM key) are shown or typed once, never echoed back
  or written to logs, and the plan preview masks them.
- Anything the wizard cannot do safely (installing Docker, opening a firewall port) it guides rather
  than performs.

## 8. Design system and deliverables

Use the existing system in `docs/designs/planet_express_design_v23`: `system/cockpit.css`, the 11
rules in `system/README.md`, `system/ACTION-SCREENS.md` (login, approval and execution surfaces are
close relatives of screens 6, 8 and 9), and the crew portraits in `assets/`. Do not edit the sealed
first 582 lines of `cockpit.css`.

Deliver in the repo's handoff format (see `handoffs/V2.4-STACK-CONTROL.md`): a `V4-INSTALLER.md`
handoff, the screens as `.dc.html` references, and any new components added to `COMPONENTS.md`.
Mobile width matters: people will run this from a phone next to the server.

## 9. Open questions for the designer and for us

- One long page with a stepper, or separate screens per stage? (Resumability favours separate screens.)
- How much of the plan review should be hidden behind "advanced" for the first-time user?
- Do we show the crew banter in logs, or only at stage transitions?
- Telegram detection: should the wizard poll for the chat ID automatically or use a "Check now" button?
- For adoption, how do we show 80+ discovered containers without overwhelming the page?

## 10. Out of scope

Multi-host fleet install, cloud or container-based deployment, and automatic installation of Docker.
