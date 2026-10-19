# S7b — Scope-enforcing Docker API proxy and generated sudo scope (v4 slice 7, part B) — SAFETY SURFACE (G1 + G3, reviewer not Claude)

Read first: `docs/designs/v4-slice-0-contracts.md` §6.1 (the whole proxy spec), §2.3, §4 (`docker`, `sudo`), §15,
§22; `docs/designs/v4-mission-control.md` §5, §6.3, §6.8; `planet_express/missions/{grants,resources,sessions,
worker_rpc,runner}.py`, `planet_express/missions/launcher/` (socket handling, side_doors.py, setup_steps.py),
`planet_express/setup/` (step catalogue, journal, undo) and the existing generated-sudo code in the repo
(`planet_express/execution/policy.py` `_check_sudo_allowlist`, the setup sudoers generator, MOS sudo gate notes in
`docs/designs/mos-sudo-gate-scoping.md`). `git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer.
Independent of S7a except the Supervisor hook where the per-session proxy socket is created and removed.

## Goal
Give the Repair worker Docker access that cannot reach root: a deny-by-default, schema-aware proxy on a per-session
Unix socket, plus a generated scoped sudoers file that the worker/launcher tier uses, shown in setup and removed by undo.

## Deliver
1. `planet_express/missions/dockerproxy/`: a proxy server per contracts §6.1: own Unix socket per session (created by
   Core at issuance, 0660 to the session user, removed at revoke), session token required on every connection,
   every write checks: session not revoked, cgroup/uid binding, grant hash still approved, resource id inside the
   session's effective scope and current fences, tripwire/broker not paused. Deny-by-default endpoint and API-version
   allowlist, per-endpoint field allowlists, the full refusal list and container create/update field rules from §6.1
   (no Privileged, CapAdd allowlist, no host namespaces, no Devices/Sysctls/Tmpfs/SecurityOpt beyond allowlist,
   no GroupAdd to protected gid, VolumesFrom in-scope only, mounts only scoped named volumes or binds inside the grant
   paths/mounts and never the Docker socket, no volume create, no image build/import/load, no prune, no `remote=`/URL
   params). Request body size cap, header/URL normalisation (path traversal, double slashes, encoded variants,
   `Upgrade`/hijack handling for exec/attach restricted to in-scope containers, HTTP smuggling: reject ambiguous
   framing), every accepted write recorded as an event BEFORE it is forwarded; inspect responses redacted (env values,
   secret-looking labels). Streaming/logs bounded. Fail closed on any parse doubt.
2. A separate read-only proxy variant for discovery consumers (brief §14.1 item 9): GET/inspect/list only.
3. Generated sudo scope: `planet_express/missions/sudo_gen.py` builds the worker-tier sudoers file from the grant
   `sudo.rules` (unit + actions, exact argv, no wildcards, no `ALL`, no env passing), validated with `visudo -c` in
   tests where available; setup step (probe/repair/undo through the existing journal) writes it root-owned 0440 and
   shows the exact text in the plan preview; uninstall removes it; the §6.8 sudoers side-door check recognises exactly
   this generator's output (hash) as "managed". `sudo.mode=broad` stays an elevated per-mission approval (S7a) and is
   never generated for standing grants.
4. Supervisor/runner hook: create the proxy socket with the session (so the worker env/mount sees only its own),
   tear it down at revoke/kill/pause; `docker: raw` only when `sudo.mode=broad`.

## Acceptance tests (write first; each refusal class has one)
- Refusals: privileged, host mount, docker-socket mount, out-of-scope container/network/volume/image object, each host
  namespace mode, devices, dangerous caps, volume create, build/import/prune, `remote=`, protected-gid GroupAdd,
  VolumesFrom out of scope, bind outside grant paths, symlink/`..`/encoded path tricks, oversized and smuggled
  requests, unknown endpoint, unknown API version, hijacked exec to an out-of-scope container.
- Accepted writes in scope are forwarded and recorded first (event exists even if forwarding fails); a revoked/paused/
  stale-grant/other-session token is refused; a same-uid process in another session cannot use the socket.
- Redaction of inspect responses; bounded streaming; fail closed on malformed JSON.
- Sudo generation: exact text snapshot, no wildcard possible by construction (property test over rule inputs),
  `visudo -c` passes where visudo exists, setup preview shows it, undo removes it and restores the prior state,
  side-door check accepts the generated file and rejects an altered one.
- Privileged subset (docker + MOS VM via `scripts/run_privileged_tests.sh`): real unix-socket permissions, peer
  credential checks, and a real Docker daemon where available (skip otherwise).
- Missions disabled => nothing starts. Full suite green.

## Report
Files changed, test counts, deviations (each), open questions.
