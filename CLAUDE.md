# Planet Express — Claude Code instructions

## Second-review gate (standing process, applies to every spec — not just releases)

Before treating any spec's work as done — and especially anything touching `casa_bender.py`'s
safety checks, sudo/execution scope, or `casa_farnsworth.py`'s planning/approval logic — run an
independent review with the Codex CLI in addition to Claude's own `/code-review`. Don't wait to be
asked; this is a standing part of the workflow, same convention as the Billarr project. Codex is
installed and authenticated on this host (`codex login status`).

Run one of:

    codex review --commit <sha>     # a specific commit
    codex review --base main        # everything on this branch since it diverged from main
    codex review --uncommitted      # staged/unstaged/untracked changes not yet committed

Read every finding it reports and either fix it or tell the user explicitly why it's being left
as-is — don't silently drop findings. Both reviews stay in the loop; they catch different things.

This gate is most load-bearing on execution safety. **The safety model is changing for v4**
(`docs/designs/v4-mission-control.md` §6, approved direction). v3's rule that every host change is a
typed step, with no shell anywhere, is retired for *missions*: a supervised agent session may use
its native tools. Safety moves from "what may the agent type" to consequence control. Until a v4
slice lands, the v3 text below still describes the running code:

- v3 (current code): every change is a typed step in an approved runbook, validated per type,
  target bound at proposal and re-checked before acting, run as argv through `run_argv()`, outcome
  decided by reading host state. `_run_command()`, `shell=True` and `_safety_check()` are gone.
  Typed runbooks remain the fast path for known repairs in v4, not the boundary.

What a second reviewer should attend to, v3 and v4 together:

- **v3 surface that stays:** the step catalogue (`planet_express/execution/runbook.py`), the sudo
  scope (`_check_sudo_allowlist()`), policy and limits (`planet_express/execution/policy.py`),
  `compose.write` (`planet_express/execution/compose_files.py`) and the unattended-origin list
  (only `zoidberg`/`update.canary` and `system`/`prune.safe`).
- **Host-mutation lock and network-guard exceptions (v3, still load-bearing):** the `PipelineState`
  lock in `casa_farnsworth.py` (it serialises every mutation against every scan, and now fires the
  quiet re-scan from `end_mutation`/`transition`), and the AdGuard exception to the network guard
  (`allow_guarded_update`, `resolve_target`, and the automatic-run exclusion in `policy.py`). Any
  change to either gets the second review.
- **v4 surface (when built):** the checkpoint/restore service; the tripwire monitor and protected
  domains; the grant schema, its hash binding and the grant-to-credential/environment builder;
  standing grants (their scope, expiry, and that they never cover Change/Critical); the worker
  identity, privilege tiers and the per-principal RPC method allowlist (the worker must never reach
  approval/decision methods); per-resource leases and fencing; the session supervisor's crash
  classification; redaction before persistence; memory quarantine (candidates are never injected
  into a session until operator-confirmed).
- **Slice 6 as built:** `missions/grants.py` (validation, grant-to-env builder), `missions/protected.py` (default protected
  set; override recording), `missions/resources.py` (containment, protected-wins), `missions/tripwire/` (signals, diff,
  monitor), `missions/launcher/side_doors.py` (worker identity, Docker TCP, sudoers), and `missions/tools.py`. Brokered tools
  (`tool.invoke`, a reviewed capability, each addition gets the second review): `traefik.read`, `compose.dry_run`,
  `app.health_get`, `arr.queue_item` (read); `dns.rewrite` (write, gated until slice 7).
- **Independent verification:** a mission completes only when PE's own pre-agreed checks pass,
  never on an agent's assertion.

None of these fails safe by accident. They fail safe because someone checked.

## Project shape

This is being reworked from a single-host bespoke agent into an installable, config-driven project,
in small independent specs rather than one big rewrite. See the project's plan history for the
current roadmap and the decisions already locked in (brain stays a standalone daemon, not an MCP
server; Docker Compose only; full-pipeline-on by default, config-driven; git-clone + systemd
install, not a container). Don't re-litigate those without the user raising it again.

# gstack

For all web browsing, use the `/browse` skill from gstack. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`,
`/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`,
`/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`,
`/setup-browser-cookies`, `/setup-deploy`, `/setup-gbrain`, `/retro`, `/investigate`,
`/document-release`, `/document-generate`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`,
`/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
