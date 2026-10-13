# v4 development input: what the server-side Claude should export

**Purpose:** seed PE v4 *development* (fixtures, evaluations, gap analysis, test memory) from the
operator's casaserver Claude session and skill pack.

**Hard rule.** This is a development and learning artefact for this operator's fleet only.
Nothing here ships, and no v4 contract, install path or acceptance test may depend on it. Another
site may have no skill pack, no notes and no incident history. For them PE builds memory from its
own scans and missions (v4 brief §9) and from a generic import format. Design the importer and the
product against the **generic format below**, and treat this export as one possible producer of it.

## What to export, in priority order

The first four drive design decisions; the rest feed memory seeding and fixtures.

1. **The gap list: what you do that PE won't or can't.**
   For every kind of fix you perform that PE refuses or lacks: the trigger, what you ran or
   edited (commands and files, with real paths), why PE's v3 model couldn't, and how risky it felt.
   *Drives:* the first Repair missions and what the worker environment needs. Highest value.

2. **Tool and privilege inventory.**
   Which tools you use (docker, compose, systemctl/service, mount, borg, journalctl, ssh, curl,
   editors, package managers), with rough frequency, whether each needed sudo, and **which
   credential or API each task needs** (names and where they live, never values).
   *Drives:* privilege tiers, grant-to-credential mapping, which credentials a Repair session
   must never receive.

3. **Incident register (anonymised shape).**
   For each past incident: symptoms as first seen, misleading signals, root cause, fix steps in
   order, what proved it fixed, what you got wrong first, time to resolve, whether it recurred.
   *Drives:* the three incident fixtures, retrieval evaluations, the "wrong first guess" cases.

4. **"What healthy really means" per service/stack.**
   The application-level check that proves it works (not just the container being up): endpoint,
   expected response, file or mount that must be readable, query, dependency order.
   *Drives:* the pre-agreed verification catalogue and the first Repair proof.

5. **Constraints and protected domains.**
   Things never to touch or only touch in a window: ingress/DNS, firewall, credentials, data
   volumes, backup jobs and windows, reboot rules, anything the operator told you not to do and why.
   *Drives:* the default protected-domain list, tripwire design, constraint cards.

6. **Procedures (ordered, proven).**
   Recovery sequences such as the stale-NFS recovery, boot ordering, backup restore, canary
   rollback, with preconditions, order, and the check at each step.

7. **Fragilities.** Known failure modes and limitations (e.g. NFS stale while container health is
   green), with the evidence that showed it.

8. **Decisions and deferred work.** Deliberate trade-offs ("keep NFS until the storage migration")
   with rationale and a review-by date.

9. **Operator preferences and working style.** Maintenance windows, what not to be asked about,
   how much autonomy is acceptable for which class of work, notification preferences.

10. **Host quirks.** Ubuntu vs MOS differences, init/service behaviour, boot hooks, anything that
    broke a naive approach.

11. **Topology and dependency map.** Stacks, services, mounts, networks, start order, external
    dependencies. (PE can discover most of this itself; export only what discovery would miss,
    for example *why* a dependency exists.)

12. **Where you were wrong or got stopped.** Mistakes, near-misses, commands you refused to run,
    and cases where the right answer was "ask the operator". *Drives:* escalation rules and the
    hostile or ambiguous fixtures.

## Format (this is the generic import format)

One file per card, Markdown with front matter, or a single JSONL with the same fields.

```markdown
---
id: nfs-stale-media          # stable slug
type: fact | procedure | constraint | decision
scope: ["mount:/mnt/media", "stack:media"]
source: operator-stated | observed | researched | inferred
confidence_note: "seen in two incidents"   # free text; PE assigns status itself
evidence: ["incident-2026-08-14", "doc:borg-notes"]
review_after: 2027-01-13
supersedes: []
---
Statement, procedure steps (numbered, with the check after each), and constraints.
```

Incident entries are a separate `incidents/` JSONL: `symptoms, misleading, root_cause, fix_steps,
verification, wrong_first_guess, duration, recurred, tags`.

Tool and gap entries are a separate `gaps/` JSONL: `task, trigger, commands_or_files, needs_sudo,
needs_credentials (names only), why_pe_could_not, perceived_risk`.

## Rules for the export

- **No secrets.** Credential names and locations only. No tokens, passwords, keys, or full
  `.env` contents. If an item needs one, write `<credential: cloudflare-dns-token>`.
- **No raw transcripts.** Distil. Cite the session or incident ID as evidence instead.
- **Mark provenance honestly.** `observed` only if you saw it; `operator-stated` only if the
  operator told you; otherwise `inferred`. Do not upgrade anything.
- **Say when unsure.** Prefer "unknown" over a plausible guess. Every guess becomes a candidate
  card that a human must confirm.
- **Use real paths and names**, but keep each card bounded (a screenful). Split large procedures.
- **Private incident details stay private**: do not include anything from other projects.

## Prompt to give the server-side session

> Read your memory, skill pack and recent session history. Produce the v4 development export as
> specified in `docs/designs/v4-server-memory-export.md`: first the **gap list**, then the
> **tool and privilege inventory** (credentials by name only), then the **incident register**,
> then **what healthy means** per stack, then constraints, procedures, fragilities, decisions,
> preferences, host quirks, topology gaps, and your mistakes. Follow the card format exactly. Include
> no secret values and no raw transcripts. Mark provenance honestly and say "unknown" when unsure.
> Write everything under `export/v4/` as separate files and finish with an index listing counts per
> category and your own confidence in each category.

## How the export is used (and not used)

- Gap list, tool inventory, incident register and health checks inform Phase 0 decisions and
  become test fixtures under `tests/fixtures/v4/` (anonymised). They are not shipped.
- Cards are loaded through the *product's* import path, as `candidate`/`operator-stated`, into a
  **development** database only, which also tests that the importer works for a user with just
  Markdown notes.
- A site with nothing to import must still pass every v4 acceptance test that does not require
  historical memory; fixtures for those tests are synthetic.
