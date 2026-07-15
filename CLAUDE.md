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

This gate is most load-bearing on the sudo/execution-allowlist work specifically: the current
`_safety_check()` in `casa_bender.py` is a pure blocklist with no allowlist check at all for
sudo-scoped commands — the only things stopping an out-of-scope `sudo` today are prompt text and
the OS-level sudoers.d grant. Any change to that logic is exactly the class of "unenforced safety
boundary" bug an independent second reviewer exists to catch.

## Project shape

This is being reworked from a single-host bespoke agent into an installable, config-driven project,
in small independent specs rather than one big rewrite. See the project's plan history for the
current roadmap and the decisions already locked in (brain stays a standalone daemon, not an MCP
server; Docker Compose only; full-pipeline-on by default, config-driven; git-clone + systemd
install, not a container). Don't re-litigate those without the user raising it again.
