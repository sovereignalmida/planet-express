# S9b — the repair rehearsal's agent must go through the REAL Docker proxy

Context (2026-10-22, first real rehearsal on an Ubuntu 24.04 VM, record in the orchestrator's scratchpad): `scripts/rehearse_repair.py`
runs the scripted agent as root against the HOST Docker socket (documented in its `not_exercised`). After G4-F2 item 4 the tripwire
treats any container added during a session that the proxy did not record as `out_of_scope`, so scenario (a) correctly paused
(`paused_tripwire`: "c2fab46c7829_pe-rehearsal-gateway-1: not created through the proxy"). Scenario (b) then failed as collateral
(`checkpoint_refused: pe-rehearsal/gateway: 2 containers match` because (a) left a renamed half-recreated gateway container).

## Do
1. Make the rehearsal's agent reach Docker ONLY through a real `dockerproxy.server.ProxyServer` per mission (real `Upstream` to
   the host socket, a real `Policy`/`Scope.from_grant` for the grant, a gate that recognises the agent's process, the real
   proxy recording of creates into the store). The agent sets `DOCKER_HOST=unix://<proxy sock>` (the supervisor/proxy service
   already has this wiring: reuse `DockerProxyService` and `assembly` rather than hand-building if possible). Read
   `tests/test_dockerproxy_privileged.py` (versioned_request, the real create/start test) and `missions/dockerproxy/service.py`.
2. The scripted agent's compose/docker commands must work through the proxy (compose uses many API endpoints: if the proxy
   refuses a call the real agent would need, that is a FINDING to report, not something to bypass). Do not widen the proxy
   policy to make the rehearsal pass; report which calls were refused and why.
3. Make the scenarios idempotent: before each scenario remove leftover renamed containers (`<hash>_<name>`) of the rehearsal
   project; teardown must leave nothing (`docker ps -a` empty of the project, networks removed).
4. Update `not_exercised` honestly (the proxy socket is now exercised; the cgroup launcher/uids still are not unless you can do it
   cheaply with the real launcher). Update docs/handoff/v4/rehearsals.md and release-proof.md item 7 wording.
5. Unit tests for the new wiring (fakes ok). You may NOT run the rehearsal or privileged tests on this machine. Never set
   PE_REHEARSAL_SKIP_GATE. Do not push. Trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
6. Run `codex review --base v4-next` after green; fix or decline each finding in writing.
Report: files changed, tests, proxy calls refused (with reasons), Codex findings, anything the orchestrator must run on the VM.
