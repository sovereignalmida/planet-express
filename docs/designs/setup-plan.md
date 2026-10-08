# Setup `plan`: design

Status: draft, written with the first implementation. Branch `v3-next`, 2026-10-08.
Part of the setup core behind the wizard (`installer-brief.md` section 6). `discover` is built;
this is the `plan` step. `apply` comes after, and consumes exactly what `plan` returns.

## What `plan` is

`plan(discovery, answers) -> Plan`. A pure function: no host access beyond the discovery report it
is given, deterministic, secret-free once serialised. The same inputs give the same plan and the
same `plan_id` (a hash of the public JSON). `apply` will re-run discovery and refuse if the host no
longer matches the plan, the same "bind at proposal time, re-check before acting" rule as runbooks.

A step is typed data, not a command: `kind`, validated `params`, `risk`, `reversible`,
`needs_root`, `depends_on`, and a `preview` that is file contents or a description. **No shell
command ever reaches the UI**, and no step is a shell string. This is the setup-time version of
the rule that governs runtime changes (`CLAUDE.md`: there is no shell).

## What the real installer does (the procedure `plan` mirrors)

From `deploy.sh`, in order: preflight; state/logs directories; venv and dependencies; config.yaml
and sudoers (`scripts/setup_wizard.py`); the secrets env file (only if absent); dashboard access
(`scripts/web_access.py`) and operators (`scripts/dashboard_operators.py`); systemd units;
smoke test; enable and start. Content generation is reused, not reimplemented:
`PlanetExpressConfig` for config.yaml, `generate_sudoers_snippet` for sudoers (it carries several
Codex-found fixes), `scripts/render_template.render` for units.

## Step kinds (v1)

| kind | does | risk | reversible | needs root |
|---|---|---|---|---|
| `dir.ensure` | create a directory | R1 | yes (if it created it) | no |
| `python.env` | venv plus `requirements.txt` | R1 | yes (remove venv) | no |
| `file.write` | one file; `if_exists` is `keep` or `replace` | R1 under the install, R2 system-wide | yes (restore or delete) | by path |
| `sudoers.install` | scoped NOPASSWD grant, checked with `visudo -c` | R3 | yes | yes |
| `access.provision` | dashboard user, RPC group, read-only ACLs | R2 | partly | yes |
| `dashboard.init` | dashboard env file, session secret, first operator | R2 | yes | yes |
| `service.install` | systemd unit, or sysvinit script plus `/etc/default` | R2 | yes | yes |
| `boot_hook.install` | merge a marked block into MOS `/boot/optional/scripts/*.sh`; never replace | R3 (the boot image) | yes | yes |
| `state.snapshot` | snapshot an existing install's state first | R1 | yes | no |
| `service.enable` | enable, and optionally start | R2 | yes | yes |
| `verify.smoke` | read-only check that Leela can see Docker | R0 | n/a | no |

Existing files are never overwritten silently: `if_exists: keep` is the default for the env file
(as `deploy.sh` does), and a replace is called out in the plan.

## Invariants the builder enforces (each has a test)

1. **The service never runs as root, except where nothing else is possible and the operator said so.**
   `deploy.sh` refuses root outright (an unprivileged service is what makes the sudo allowlist
   mean anything). MOS has **no `sudo`** (no binary, no `/etc/sudoers.d`), so PE there must run as
   root. The plan is *blocked* for `run_as=root` unless the host is MOS and
   `accept_root_service` is true, and then carries a warning that says why.
2. **Secrets never appear in a plan.** Steps hold `{{secret:NAME}}` placeholders; values live
   beside the plan, outside its public JSON and its id. Secrets go only to the root-only env files,
   never to config.yaml.
3. **Powers are risk tiers, because that is what the config can enforce.** The only working gate is
   `autonomy.forbidden_risks`, checked before every runbook including the automatic ones
   (`policy.decide_runbook`). Canary updates and safe prune are both R2, so they cannot be toggled
   apart; unit control (R3) also needs a sudoers grant.

   | tier | allowed | `forbidden_risks` |
   |---|---|---|
   | observe | read-only scans, reports | R1 R2 R3 R4 |
   | restart | + restart/start services, stack up (R1) | R2 R3 R4 |
   | stacks | + stop, stack down, canary updates, safe prune (R2) | R3 R4 |
   | full | + unit control, compose edits, down-all (R3) | R4 |

   R4 is always forbidden (the schema requires it).
4. **Per-stack MANAGE vs WATCH cannot be expressed today.** Config has `forbidden_stacks`
   (ignored) and nothing finer; control is global, by tier. The wizard offers *include* or
   *ignore* per stack, and the tier is the only dial for what PE may do to the included ones.
5. **No host change without a step**, and `will_not_touch` states what setup will not do: stop or
   recreate a container, edit a compose file, touch an ignored stack, or write a secret to
   config.yaml.
6. **MOS needs a persistent install.** `/` is RAM, so every path must be under a pool; boot hooks
   reinstall init scripts and `/etc/default` from there at each boot.

## Hosts that already have Planet Express

`discover` reports which install files already exist (`existing_pe.present`) and where the install and
config really are (read from the unit file or `/etc/default`, not assumed). `plan` then:

- uses the existing config path instead of creating a second one, and creates no extra config directory;
- keeps every existing file and unit (`if_exists: keep`), as `deploy.sh` does for `casa-stacks.service`;
- drops steps that would be no-ops (an existing dashboard login with no operator to add);
- stops warning about Telegram, LLM or operator when the files that hold them are already there;
- makes every `will_not_touch` line conditional on the host. In particular, **a kept config never
  receives the chosen power tier**, so the plan says exactly that instead of promising a restriction
  it will not apply. Changing an existing install's powers is a config edit, not an install step.

These came from planning against the real live host, not from the unit tests: the first version would
have overwritten its units, created a second config, and claimed the host was locked down when the
existing config was left as it was.

## Bootstrap: setup runs before the project has a virtualenv

`discover` is stdlib-only on purpose, so it can run on a bare host. `plan` needs pydantic and PyYAML
(it validates with the real config schema), and the browser server will need Flask. Those cannot come
from the project venv, because creating that venv is one of the plan's own steps. So the launcher
that starts setup (a small `setup.sh`, still to write) creates a throwaway bootstrap environment first.
On MOS that means fetching pip, because MOS Python ships without ensurepip, so the same
internet requirement the plan warns about applies before the wizard even opens.

## Not in this slice

Repair and uninstall plans, `apply`, the browser server, Telegram detection (an interactive screen
action, not a plan step), and per-stack watch-only mode (needs a backend feature first).
