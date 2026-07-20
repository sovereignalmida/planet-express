# Planet Express

A self-hosted sysadmin agent for a Docker Compose homelab: it watches your stacks, diagnoses real
failures, proposes and (with your approval) executes fixes, canary-updates images with automatic
rollback, and talks to you over Telegram.

**Status: pre-release, actively being generalized.** This project started as a bespoke agent
running on one person's home server, hardcoded to that host. It's now being reworked into something
anyone with their own Compose-based homelab can install — see [CHANGELOG.md](CHANGELOG.md) for
what's landed so far. It is dogfooded on the author's own fleet from day one of that rework, not
developed in isolation and thrown over the wall.

## What it does

Five roles, one per pipeline stage (yes, they're Futurama-named — see below):

- **Leela** (monitor) — collects container health, disk usage, mount status, and journal errors.
  No LLM call; pure data collection.
- **Hermes** (analyzer) — turns Leela's snapshot into severity-ranked findings.
- **Farnsworth** (orchestrator + planner + bot) — runs the pipeline on a schedule, turns findings
  into concrete remediation plans with rollback steps, and is the Telegram bot that asks for your
  approval before anything executes.
- **Bender** (executor) — runs one approved plan's steps in order, hard-stops on the first
  failure. Has its own independent safety layer (forbidden commands, forbidden stacks, a
  network-stack guard) that doesn't trust the plan alone.
- **Amy** (diagnostician) — only runs after a normal remediation has already failed once; digs
  into logs and, when useful, searches the web for a documented fix. Never executes anything
  itself.

Plus a canary auto-updater (**Zoidberg**) that updates one service at a time, watches it, and rolls
back automatically if it doesn't come up healthy.

There's also a read-only web dashboard (**Scruffy**) for a glance-and-go status view, and a
`/api/widget` JSON endpoint for embedding that status in a [Homepage](https://gethomepage.dev)
dashboard.

## Why Futurama names

The pipeline stages map onto the crew: Leela keeps watch, Hermes files the paperwork, Farnsworth
gives the orders (and holds the checkbook — nothing executes without his, i.e. your, approval),
Bender does the actual work, Amy figures out what's really wrong when the first fix doesn't stick.
It's stuck around because it's a genuinely useful mental model for what each stage is responsible
for, not just a joke.

## Project status

This repo is being built out in small, independent specs rather than one big rewrite — see
`CLAUDE.md` for the standing engineering process (including an independent second-review gate) and
the project's planning history for the current roadmap. Still pre-release, but `git clone` +
`bash deploy.sh` is now a real install path — see [INSTALL.md](INSTALL.md) for the full
walkthrough, including how to get a Telegram bot token and what the optional sudo grant is for.

## License

MIT — see [LICENSE](LICENSE).
