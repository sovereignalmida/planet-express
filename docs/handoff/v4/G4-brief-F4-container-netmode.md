# G4-F4 — serve `NetworkMode: container:<id>` for an in-scope target (execution-safety; second review required)

Why: the First Repair incident (CLAUDE.md/v4-mission-control: VPN-gateway recreate orphaning containers that join its network
namespace via `network_mode: service:<gw>`) cannot be repaired through the proxy: Compose recreates dependents with
`HostConfig.NetworkMode=container:<gateway-id>` and `dockerproxy/policy.py` `_scoped_network_name` (~line 524) refuses every
`container:` mode. Found by Codex on S9b and by the first real rehearsal. Decision (orchestrator, 2026-10-22): serve it narrowly.

## Rule (all must hold, otherwise refuse exactly as today)
- Only `container:<ref>`; `host` stays refused (and `NetworkMode: host` anywhere else, incl. `--net=host` equivalents).
- `<ref>` must be a syntactically valid id/name (same `_NAME` check) and RESOLVE through the daemon inspector to exactly one container.
- That target container must classify `in_scope` for this mission's grant (same resource identity the proxy uses for container
  targets: by what Core/the daemon knows, never by labels the requesting body supplies) and never `protected`; protected wins; an
  excluded container is refused; a container outside scope, unknown, or ambiguous is refused with a clear reason.
- Re-checked at start time too (a container created with a joined namespace, then the target swapped: the start path re-inspects).
- Read-only missions (Observe/Propose) still cannot create at all (unchanged).
- Any other place that accepts NetworkMode (create, update/connect, `networks/connect` with container-mode) is audited: either
  covered by the same rule or still refused. List what you audited in the report.
- The refusal reasons are specific so a rehearsal record shows them (`the container network target is outside this mission's scope`...).

## Do
Tests first (synthetic fixtures; tests/test_dockerproxy_policy.py style): in-scope target allowed; out-of-scope, protected, excluded,
unknown, ambiguous name, `host`, `container:` of an id that resolves to a container created by ANOTHER mission, a body whose labels
claim the target is in scope but the daemon says otherwise, and the start-time re-check. Then implement. Then `codex review
--base v4-next` and fix or decline each finding in writing. Run the non-privileged suite (ignore the four *_privileged files).
Do not push; never run privileged tests or rehearsals here; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
Report: files changed, tests, the audit list, Codex findings and dispositions.
