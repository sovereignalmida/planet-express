# Contributing

## Dev setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
```

`requirements-dev.txt` pulls in `requirements.txt` plus `ruff`. Python 3.11+ is required — the
codebase uses newer syntax that older interpreters can't parse (see `deploy.sh`'s own preflight
check).

## Running lint and tests locally

```bash
venv/bin/ruff check .
venv/bin/pytest
```

Both run in CI (`.github/workflows/ci.yml`) on every push/PR. The test suite is pure-logic only —
state-schema round-trips, safety-check allow/deny rules, notifier/dashboard/template rendering
against faked collaborators. It never touches a real Docker daemon, sudo, or systemd, so it can't
catch everything; see README.md's "What's tested, what isn't" section for what still requires a
manual pre-release check against a real host.

## Making a change

- Keep specs small and independent rather than bundling unrelated changes into one PR.
- If your change touches `casa_bender.py`'s safety checks, sudo/execution scope, or
  `casa_farnsworth.py`'s planning/approval logic, read the second-review gate in `CLAUDE.md` first
  — it applies to contributions too, not just the maintainer's own work.
- Add or update tests for any pure-logic change. If a change can only be verified against a real
  Docker/sudo environment, say so explicitly in the PR description rather than leaving it implied.

## Reporting bugs / proposing changes

Open a GitHub issue or PR. For anything touching safety/execution logic, include what you tested
it against (unit tests, a real homelab, both).
