# Planet Express

**A self-hosted sysadmin for your Docker Compose homelab.** It watches every machine, tells you what is wrong in plain
words, and changes nothing until you say yes. When it does change something, it takes a checkpoint first, checks the
result itself, and can put things back.

![Overview: 102 of 102 containers healthy across four hosts](docs/screenshots/overview.png)

You run it on one host. It reads the others over SSH. You work in its **LAN dashboard**; **Telegram** is the
companion for when you are away: scan reports, approve-or-cancel cards and quick commands.

- **One picture of the fleet.** Every container on every Docker host, counted, checked and alerted on. A host it cannot
  read is shown as unknown, never as zero.
- **Findings, not alarms.** A scheduled scan collects facts (no AI involved), then a model files them as findings with
  a severity and a suggested next step.
- **Fixes you approve.** Known repairs are typed steps from a fixed catalogue: restart, update with a canary and
  automatic rollback, compose changes. Each one is bound to what you approved and re-checked before it acts.
- **An AI investigator with undo.** Describe a problem in the Console. An agent investigates on the host and comes back
  with up to three options, each with what it changes, what it will not touch, the risk and how to get back. You
  confirm one; it runs from a checkpoint, and PE only calls it done when its own checks pass.
- **Routes for every host.** Traefik on this host sees only this host's containers. PE turns the other hosts' Traefik
  labels into routes, carefully, so one proxy serves the whole lab.

**Status:** v4 release candidate (`v4.0.0-rc14.12`), dogfooded every day on the author's five-machine homelab.
See [CHANGELOG.md](CHANGELOG.md).

## The dashboard

<table>
<tr>
<td width="50%"><a href="docs/screenshots/console.png"><img src="docs/screenshots/console.png" alt="Console"></a><br><sub><b>Console</b>: ask about your homelab in plain words. The agent works on the host with your access, every command is on the record beside the answer, and a change only happens through a plan you confirm.</sub></td>
<td width="50%"><a href="docs/screenshots/missions.png"><img src="docs/screenshots/missions.png" alt="Missions"></a><br><sub><b>Missions</b>: every investigation and repair, by outcome. NEEDS YOU holds what wants a decision. The provider's credit and spend are shown; nothing stops at any figure, you decide.</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/screenshots/hosts.png"><img src="docs/screenshots/hosts.png" alt="Hosts"></a><br><sub><b>Hosts</b>: this host and every other one: CPU, memory, disk, temperature, containers and their routes. Read over SSH with three fixed read-only commands; an appliance without Docker gets metrics only.</sub></td>
<td width="50%"><a href="docs/screenshots/network.png"><img src="docs/screenshots/network.png" alt="Network"></a><br><sub><b>Network</b>: every Traefik route as a tile you can open, tagged with where it lives (another host, a file route, the LAN), plus AdGuard's numbers.</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/screenshots/backups.png"><img src="docs/screenshots/backups.png" alt="Backups"></a><br><sub><b>Backups</b>: Borg jobs judged by freshness, not just their last exit code, and the certificates and when they expire.</sub></td>
<td width="50%"><a href="docs/screenshots/crew.png"><img src="docs/screenshots/crew.png" alt="Crew"></a><br><sub><b>Crew</b>: who does what, and the ship's computer log: one line per area, written from the real state.</sub></td>
</tr>
</table>

Also there: **Memory** (lessons the agent offers and you confirm, and the update history) and **Config** (edit
`config.yaml` from the browser; sensitive keys stay locked to the host). A `/api/widget` endpoint serves the status
to a [Homepage](https://gethomepage.dev) dashboard.

## Quick start

You need a Linux host with Docker and Docker Compose, and Python 3.11 or newer.

```bash
git clone https://github.com/sovereignalmida/planet-express.git
cd planet-express
sudo ./setup.sh
```

`setup.sh` starts a browser setup wizard on your LAN (a one-time HTTPS link, valid for 15 minutes, private addresses
only). It **scans** the host, asks where Planet Express lives and **what it may do** (four tiers, from watch-only to
the full crew), sets up your **Telegram** bot and an **operator account** (passphrase plus authenticator app), and
takes an optional model key. Then it shows you **the plan**: every file, unit and grant, what needs root, what can be
undone, and what it will not touch. Nothing changes until you approve that plan. It can also **repair** and
**uninstall** an install, and undo what it did.

<details>
<summary>The wizard, screen by screen</summary>

<table>
<tr>
<td width="50%"><img src="docs/screenshots/wizard/02-scan.png" alt="Scan screen"><br><sub><b>Scan</b>: Docker, storage, ports and any existing install, and what to fix first.</sub></td>
<td width="50%"><img src="docs/screenshots/wizard/04-powers.png" alt="Powers screen"><br><sub><b>What it may do</b>: four tiers, each with the access it needs.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard/06-operator.png" alt="Operator account"><br><sub><b>Operator account</b>: proved with a real code before it is kept.</sub></td>
<td width="50%"><img src="docs/screenshots/wizard/08b-review-expanded.png" alt="Review the plan"><br><sub><b>Review the plan</b>: every step, its risk, and whether it can be undone.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard/09-install.png" alt="Installing"><br><sub><b>Install</b>: one approved step at a time, each read back from the host.</sub></td>
<td width="50%"><img src="docs/screenshots/wizard/10-done.png" alt="Done"><br><sub><b>Done</b>: verified, with an undo button.</sub></td>
</tr>
</table>

</details>

Ubuntu (systemd) and MOS (sysvinit, RAM root) are supported. [INSTALL.md](INSTALL.md) has the details: your other
hosts over SSH, Traefik routes for them, mission control and the Console, the model broker, and the older scripted
install (`bash deploy.sh`).

## How it stays safe

Planet Express is built on the idea that an automated sysadmin earns trust by being checkable, not by being clever.

- **Nothing runs without your yes**, except two things that are automatic by design and tell you afterwards: canary
  image updates (watched, and rolled back on their own if the service does not stay healthy) and a safe prune when the
  disk is genuinely short. That exemption is tied to those two step types, never to anything a model wrote.
- **Typed steps, no shell.** Outside the Console, every change is a step from a fixed catalogue: validated, bound to
  its target when proposed, re-checked just before it acts, run as an argument list, and judged by reading the host
  afterwards, not by an exit code. A model picks which step; it never writes what runs.
- **Missions are checkpointed and checked.** A Console session runs with your access (Docker, sudo), the same as you at
  a terminal, because you confirmed a specific plan. Before it changes anything, PE takes a checkpoint; every tool
  call is recorded; a short list of operations is blocked outright; and the run only completes when PE's own
  pre-agreed checks pass. **Undo** puts back what that run changed and leaves later changes by anyone else alone.
- **Secrets stay in Core.** The model key lives in one process; an agent session gets a short-lived local access code
  and per-session budgets instead. Tool output is redacted before it is stored.
- **Other hosts are read, not changed.** Three fixed read-only commands over SSH with pinned host keys. Their problems
  are reported and recorded, never turned into a plan for this host. Routes for them are written only where they
  cannot take traffic from anything that already works, checked against Traefik, and put back if Traefik refuses them.
- **The dashboard cannot reach the database.** It runs as its own user and asks Core over a socket; every change it
  asks for needs the same approval Telegram would.

## The crew

The parts are named after the Planet Express crew, because the jobs map neatly:

- **Leela** keeps watch: containers, disks, mounts, backups, certificates, other hosts. No AI.
- **Hermes** files the paperwork: findings with a severity.
- **Farnsworth** gives the orders: plans, the Telegram bot, the schedule. Nothing runs without his, meaning your, signature.
- **Bender** does the work, and no longer decides what it is.
- **Amy** digs in when the first fix does not stick; **Fry** onboards new stacks; **Zoidberg** runs the canary
  updates; **Scruffy** is the dashboard.

## Testing

About 6,700 tests run on every push: state, policy, the execution engine against a faked host, the dashboard, and the
setup wizard against a fake host that can be made to fail at every step. None of that touches a real Docker daemon,
sudo or systemd, so a green CI badge is not a claim about a real host. The parts that need one are rehearsed on
throwaway VMs before a release: the setup wizard end to end on fresh Ubuntu and MOS, mission control's checkpoint and
restore as root, and the canary updater against a local registry with a deliberately broken image. Every change also
gets an independent second review. [CHANGELOG.md](CHANGELOG.md) lists what those found.

## Project status

`v4-next` is the default branch; release candidates are tagged from it and run on the author's homelab.
`main` holds the last v3 release (3.1.2) until v4 is released.

## License

MIT. See [LICENSE](LICENSE).
