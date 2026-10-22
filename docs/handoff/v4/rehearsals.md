# v4 rehearsals on real hosts (release proof items 7 and 9)

Everything here runs on a **disposable** host: a throwaway VM, never a machine whose containers, accounts or sudoers
matter. The scripts refuse otherwise (`PE_PRIVILEGED_TESTS_OK` / `PE_REHEARSAL_OK` are set by the wrappers, never by you,
plus `--i-know-this-is-disposable`). Replace `HOST` with the VM's address; keep real addresses out of tracked files.

What each rehearsal proves, and what it does not:

| Rehearsal | Script | Proves |
| --- | --- | --- |
| Privileged tests | `scripts/run_privileged_tests.sh` | the launcher helper, cgroup tree, uid identity, checkpoint file modes and ACLs, tripwire on a real filesystem, the Docker proxy's socket ownership and peer credentials |
| Containment | `scripts/mission_containment_rehearsal.py` | the REAL launcher: preflight, a throwaway session, freeze/thaw/kill of a process tree incl. a double-fork escaper, the cgroup emptying |
| Repair (item 7) | `scripts/rehearse_repair.py` via `scripts/run_rehearsal.sh` | the real checkpoint service, tripwire monitor, verifier, approval flow and supervisor against real Docker with a synthetic compose project: (a) gateway recreate orphans dependents, the mission rebinds them in dependency order, an in-namespace probe and a project-completeness check pass; (b) a failed canary is rolled back, then an approved bounded correction recreates only `web` |

The repair rehearsal's agent is a deterministic command (no LLM) running as root in a process group. Its `DOCKER_HOST` is a
real per-mission Docker proxy socket (the daemon's `DockerProxyService`, real upstream to the host socket, policy derived from
the mission's grant, creates recorded into the store for the tripwire); the agent refuses to run against any other socket.
Calls the proxy refuses are not worked around: they appear under `proxy.<scenario>.refused` in the JSON record. Each scenario
starts by removing renamed leftovers (`<hash>_pe-rehearsal-*`) and re-upping the project, and teardown must leave no container
or network. The rehearsal does not exercise the cgroup launcher or session uids (the proxy recognises the agent by process
group, not cgroup and unique uid); those are covered by the first two rows. The JSON record lists this under `not_exercised`.

## Ubuntu / systemd

Prerequisites on the VM: Docker with the compose plugin, python3 with `requirements.txt` installed (a venv is fine:
set `PE_PYTHON`), cgroup v2, `sudo`, `rsync`, root ssh from the machine you run this on.

```bash
# 1. privileged test suite (sudo launch strategy; the unprivileged Core must be able to read the tests, so use /opt)
PE_PYTHON=/opt/pe/venv/bin/python PE_VM_DIR=/opt/pe-privileged-tests \
    scripts/run_privileged_tests.sh --vm HOST

# 2. host setup for missions (the plan shows every step; or the launcher CLI directly, as root on the VM)
sudo python3 -m planet_express.missions.launcher.setup_steps repair --core-user planetexpress \
    --workspace-root /var/lib/planetexpress/mission-workspaces
sudo python3 -m planet_express.missions.launcher.setup_steps probe  --core-user planetexpress \
    --workspace-root /var/lib/planetexpress/mission-workspaces         # every line must say "ok"

# 3. containment rehearsal, as the Core user
sudo -u planetexpress python3 scripts/mission_containment_rehearsal.py \
    --workspace-root /var/lib/planetexpress/mission-workspaces         # exit 0 only if everything passed

# 4. repair rehearsal (item 7). --core-user is the account Core runs as: the side-door gate checks sudoers rules that
#    reach it. PE_REHEARSAL_SKIP_GATE=1 only if the VM has not been through the launcher setup (the record says so)
PE_PYTHON=/opt/pe/venv/bin/python scripts/run_rehearsal.sh --vm HOST -- --core-user planetexpress > repair-record.json
echo $?        # 0 pass, 1 a step failed, 2 refused (nothing touched)
```

A worker and repair flow "does not silently degrade to an unmanaged session": with the side doors open (docker group,
Docker TCP, unmanaged sudoers) a Repair start is refused and the mission waits; step 2's `probe` and the dashboard's
mission card both say why. Record the refusal case too: open one door on the VM (for example add the session account to
the `docker` group), confirm a Repair mission stays in `awaiting_decision` with the reason, then close it.

## MOS / sysvinit

MOS root is RAM and PE lives on the data pool (`PE_HOME`, normally `/mnt/data/pe`). Core runs as root, the launcher
strategy is `direct` (no sudo), and `launch.conf`, the helper copy and `launcher-boot.sh` live under `PE_HOME`.
**The directories above them must be root-owned and not group/world writable** (the helper refuses anything else):
`chown root:root /mnt/data && chmod 755 /mnt/data` (the setup plan and the launcher preflight both print this exact fix).

```bash
# 1. privileged test suite
PE_PYTHON=/mnt/data/pe/venv/bin/python scripts/run_privileged_tests.sh --vm HOST

# 2. host setup (as root on the VM): the same steps; --flavour mos and --pe-home pick the pool paths
python3 -m planet_express.missions.launcher.setup_steps repair --core-user root --flavour mos \
    --pe-home /mnt/data/pe --workspace-root /mnt/data/pe/state/mission-workspaces
python3 -m planet_express.missions.launcher.setup_steps probe --core-user root --flavour mos \
    --pe-home /mnt/data/pe --workspace-root /mnt/data/pe/state/mission-workspaces

# 3. containment rehearsal, as root (the launch.conf path follows missions.launch_conf, or pass --launch-conf)
python3 scripts/mission_containment_rehearsal.py --workspace-root /mnt/data/pe/state/mission-workspaces \
    --launch-conf /mnt/data/pe/launch/launch.conf

# 4. reboot the VM and re-run `probe`: launcher-boot.sh (called by the post-start hook) must recreate the sessions
#    cgroup and put the helper back, because MOS root is RAM.
```

MOS has the standalone `docker-compose`, not the compose plugin. `scripts/rehearse_repair.py` uses the plugin and
refuses on a host without it, so the repair rehearsal on MOS is **not claimed** until a MOS-compatible run exists (the
checkpoint's MOS provider is conformance-tested but MOS parity is not claimed before a real-compose run). The record in
`release-proof.md` shows it as PENDING.

## Recorded results

| Date | Host | What | Result |
| --- | --- | --- | --- |
| 2026-10-20 | MOS test VM (direct strategy) | privileged tests, containment rehearsal | passed (see `STATE.md`; `/mnt/data` chowned `root:root 755`, operator-approved 2026-10-21) |
| 2026-10-20 | Ubuntu test VM (sudo strategy) | privileged tests, containment rehearsal | passed (see `STATE.md`) |
| PENDING | Ubuntu test VM | `run_rehearsal.sh --vm HOST` (item 7a and 7b) | not yet run: needs a human to start it on the VM |
| PENDING | either VM | refusal case: an open side door keeps a Repair mission waiting (item 9) | not yet run |
| PENDING | MOS test VM | repair rehearsal | not claimed (no compose plugin) |

Paste each new run's JSON record (or its path) and the date into this table when it is done.
