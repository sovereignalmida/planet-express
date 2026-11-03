# Trust mode: your approval is the permission

Status: approved 2026-11-03 (operator: planning sessions get full access too; strict mode deleted now, not kept a release). Supersedes the consequence-control parts of `v4-mission-control.md` §6 for Repair missions.

## Why

Three live restart-only Repairs of a single container failed. Each attempt was stopped by a different layer:

- The hook asked about the restart.
- The installed hook copy was stale.
- The Docker proxy refused a container with a read-write mount under a home directory.

Each layer was reasonable by itself. Together they made an authorised `docker restart` impossible on the operator's own machine.

The parts that are worth something were never the restrictions. They are:

- the plan the operator confirms
- the checkpoint
- the full record
- PE's own check of the result
- rollback
- the dashboard

Trust mode keeps those and drops the rest.

## The rule

- **Every session has full access,** planning ones included: the operator's choice. A planning session is asked to investigate and propose, not change, and its record shows if it did; only an authorised Repair is wrapped in a checkpoint and verification.
- **After you confirm,** the Repair session has real access, the same as you at a terminal:
  - the real Docker socket (docker group)
  - passwordless sudo
  - your files, through sudo
- **Always:** a short hard-block list of truly catastrophic actions, plus checkpoint, record, verification and rollback around every run.

There is no switch: the strict model's code is deleted, not kept behind a flag.

## What goes

| Today | In trust mode |
|---|---|
| Docker proxy (route allowlist, mount rules, lifecycle grace, `/info` gap) | Deleted. Sessions use the real socket. |
| Grant grammar: `docker: lifecycle`, scoped sudo rules, paths, mounts, credentials, restart-only | A Repair grant is the plan, its targets (for leases and verification) and the trust mode, all hash-bound as now. |
| Scoped per-grant sudoers and the backup floor tied to it | One sudoers rule for the session group. |
| Protected set enforced as ask/deny | Recorded and shown on the result card ("touched /etc/…"), never a block. |
| Hook `ask` verdicts (a headless ask is a refusal anyway) | Gone. The hook allows a command, hard-blocks it, or records it. |
| Tripwire pausing a run, refusing to start on a blind baseline, the `accept_blind` list | Record only. Its diff goes on the result card. Blind resources are noted, never fatal. |
| Side-door preflight: "no session in the docker group", "only generated sudoers" | Changed to: a session uid is never root or Core; the only passwordless sudo is PE's own Repair-group rule. |
| A failed-verify Repair holding its leases until you cancel it | If the post-checkpoint shows nothing changed, it closes as `failed` and releases its leases. If something did change, it waits for you as now, and the Console says which mission to close. |

## What stays

- **Confirm and authorise:** the hash-bound grant, so what runs is exactly what you approved.
- **Checkpoint:** taken before the run and compared after it, with rollback.
- **PE's own verification:** the agent's report never passes a mission, and "not done" stops a pass. That includes the rc13.2 check that a restart actually happened.
- **Session identity and cgroup:** the kill switch, cancel and timeouts. The model broker keeps the API key in Core.
- **The full record:** every tool call, the transcript and the result card.
- **Leases:** two missions never work on the same stack at once.
- **Typed runbooks:** they remain the fast path for known repairs.

## Hard blocks (the whole list)

The hook enforces these, and so do a few sudo `!` rules. With sudo, a determined agent can get round them, and that is accepted. They exist to catch mistakes, and the record and checkpoint catch the rest.

- Wiping a filesystem or disk: `mkfs*`, `wipefs`, `dd` to a block device, or `rm -rf` of `/`, `/etc`, `/home`, `/var/lib/docker`, or the stacks root
- Deleting Docker data: `docker volume rm/prune`, `docker system prune -a/--volumes`, `compose down -v`
- Reading PE's own secrets: `/etc/planetexpress`, the model key, and session tokens
- Changing PE itself: its config, its sudoers rule, its hook files and its database. This keeps the record honest.

## Fixes that come with it

- **Core copies the hook script into the per-session hook directory at every launch.** Today the launch uses `/usr/local/lib/planetexpress/pe_hook.py`, which you copy by hand. A per-launch copy cannot go stale, and the manual install step goes away.
- **The brief says the session has full access** and is to do the job by whatever way works: `docker compose` or `docker restart`, run with sudo when needed.

## Acceptance test (live host, before tagging)

1. Restart Prowlarr through the Console. It completes, and verification shows a new start time.
2. Change one value in a stack's `.env` and recreate the service. It completes.
3. Roll back step 2 from the Console. The file and the container are back as they were.
4. Ask for something on the hard-block list. It is refused, recorded, and the mission ends `needs_operator`.
5. A planning session runs `sudo true` and `docker ps`. Both work and are recorded.


## How sessions get real access

- **Identity.** Each session keeps its own uid. That uid is what lets PE kill a session, prove it is gone, and tell sessions apart on the worker socket. But `sudo` refuses a uid that has no account. So setup creates one system account per session uid, named `pe-s<uid>` with a private group of the same number, no home and no login shell. The range shrinks to 64 uids.
- **Groups.** The launcher gives every session the groups that launch.conf names, whatever the tier:
  - `docker`: the real socket
  - `pe-session`: sudo
  - `adm` and `systemd-journal`: logs

  The allowlist is gone. The only groups refused are gid 0, Core's own group, and gids inside the session range.
- **Sudo.** Setup writes `/etc/sudoers.d/planetexpress-sessions`, containing `%pe-session ALL=(ALL:ALL) NOPASSWD: ALL`, and validates it with `visudo -c`. The launcher stops setting `no_new_privs`, which would otherwise disable sudo.
- **Accepted consequences.** With sudo and the Docker socket, a session can:
  - leave its cgroup
  - start containers that the kill switch does not reach
  - read Core's environment, which holds the model key

  The full record, the checkpoint and verification are what answer for these.

## Implementation order (rc14)

1. **Launcher and setup:** the accounts, groups, sudoers rule and no `no_new_privs`. Preflight checks that the accounts are PE's own (instead of "range free") and that the probe has the configured groups.
2. **Hook:** the hard-block list only, with no asks and no protected paths. Core copies `hooks.py` into the per-session hook directory at every launch and runs it from there.
3. **Remove the tripwire:**
   - the monitor and signals
   - the baseline at start
   - the `paused_tripwire` handling
   - operator pause, resume and take-over

   Rollback stays, rebuilt directly on the checkpoint service.
4. **Remove the Docker proxy, the side-door gate, the protected set and its discovery, scoped sudo generation, the backup floor, restart-only grants and the approval card's host-root view.** The grant becomes the plan, its targets, budgets and verification.
5. **Lease release:** a failed Repair whose post-checkpoint shows no change closes as `failed` and releases its leases. The brief says the session has full access.
6. **Docs, both reviews, deploy steps, and the live acceptance test.**
