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

This gate is most load-bearing on execution safety. The sudo scope is code-enforced:
`_check_sudo_allowlist()` in `casa_bender.py` fails closed on any `sudo` that isn't a declared
`sudo systemctl start|stop|restart <unit>` grant. The boundary that is still NOT enforced by
structure is legacy LLM-written plans: their steps are shell strings run by `_run_command()` with
`shell=True`, guarded only by `_safety_check()`'s pattern checks, until Planet Express 2.0 (slice 5)
replaces them with typed actions that run through `run_argv()` (argv, no shell, minimal
environment). Changes to any of these (the pattern checks, the sudo allowlist, `run_argv()`, or
the host-mutation lock in `casa_farnsworth.py`'s `PipelineState`) are exactly the class of
"unenforced safety boundary" bug an independent second reviewer exists to catch.

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
