# G4-F1 — protected-set discovery is never fed (Codex G4 P1)

Finding (Codex `--base v3-next`, confirmed by the orchestrator): `planet_express/missions/runner.py` (~line 249) calls
`build_protected_set(cfg, stacks_root=..., record=...)` with none of `storage_mounts`, `volumes`, `ingress`, `dns`,
`credentials`, `backup`. No caller anywhere passes them. So on a real host `data_volumes`, `ingress`, `dns`,
`credentials`, `backup` hold only built-in defaults: the tripwire and the Docker proxy scope checks do not see what
the host would contribute. Protected domains are a stated safety boundary (CLAUDE.md, spec §protected).

## Goal
Feed discovery for real, and fail closed when it cannot be established. Observe/Propose keep working; only Repair
capability (the controls) is withheld, with a logged reason, exactly like the existing "failure to build any control".

## Do
1. Read `planet_express/missions/protected.py` (Discovery, build_protected, defaults), `tripwire/build.py`, `runner.py`,
   `assembly.py`, and how setup/config already knows mounts, stacks, Traefik/AdGuard/DNS config paths, secrets files, backup (borg) config.
2. Write a `discover_protected_inputs(cfg, ...)` (new module in `planet_express/missions/`, injectable runners for tests)
   returning the six inputs from config + live host (mounts via /proc/self/mountinfo or findmnt, docker volumes via the
   existing bounded runner). Anything configured explicitly in `missions.protected` still wins/merges as today.
3. Wire it into `build_mission_runner`. If a REQUIRED domain resolves empty on a host that has that thing (e.g. docker
   volumes exist but none discovered, discovery command failed), Repair controls are not built; log why; record an event.
   Decide and document what "required" means; a host genuinely without ingress/backup must not be locked out
   (explicit config `missions.protected_absent: [..]` acknowledgement is acceptable).
4. Tests first: discovery from fixtures, failure => controls withheld, explicit-absent acknowledgement, the tripwire and
   the proxy classify a discovered volume as protected (protected wins). Synthetic fixtures only; no real hostnames/IPs/secrets.
5. Docs: one paragraph in INSTALL-facing docs if config keys are added (S9 owns the big INSTALL section; just note keys in your report).

## Constraints
Branch from `v4-next`; do not push; Co-Authored-By trailer; never run privileged tests here; do not touch docs/private.
This is execution-safety code: after tests are green run `codex review --base v4-next` and fix or decline each finding in
writing (report them). Report: files changed, test counts, the definition of "required", Codex findings and dispositions.
