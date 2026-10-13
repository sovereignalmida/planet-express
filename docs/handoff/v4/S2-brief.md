# S2 — Ship's Memory (v4 slice 2)

Branch: worktree off `v4-next`. Python 3.11+, **no new dependencies** (sqlite3 FTS5 only; verify FTS5 is
available in the target Python and fail loudly if not). Read first: `docs/designs/v4-slice-0-contracts.md`
§13 and §14 (rev 0.4); `docs/designs/v4-mission-control.md` §8-§9; `docs/designs/v4-server-memory-export.md`
(the generic import format); `planet_express/core/redact.py` (use its detector as the secret pre-check).
Independent of S1: you ship your own DDL and tests on a standalone sqlite connection; the orchestrator
merges your `DDL` into the v6 migration. Do NOT edit `core/store.py`.

## Deliver (`planet_express/memory/`)
1. `models.py`: strict Pydantic `Card` (id slug, title, type fact|procedure|constraint|decision, scope tags
   `["mount:/x","stack:media"]`, statement, procedure steps with per-step check text, evidence refs,
   source observed|operator-stated|researched|inferred, status candidate|confirmed|deprecated,
   created_at, last_verified_at, review_after, supersedes[], conflicts_with[]); size bounds (statement
   ≤ 4 KiB, ≤ 20 steps). `Edge`, `HistoryEntry`.
2. `schema.py`: `DDL` for `memory_cards`, `memory_fts` (FTS5 external-content over title+statement+tags,
   kept in sync by triggers), `memory_edges`, `memory_history` (append-only, records actor on every
   transition).
3. `store.py`: `upsert_candidate`, `confirm(card_id, operator)` (only an operator actor may confirm a
   procedure or constraint; runtime/system actors are refused), `deprecate`, `supersede`, `mark_conflict`,
   `review_due(now)`.
4. `retrieval.py`: `retrieve(scope_tags, query, limit, byte_budget)` returns **confirmed cards only**,
   ordered by scope match, then confirmed recency, then BM25; capped by count and bytes; rendered as quoted
   data blocks with card ids for prompt injection. Candidates are never returned here.
5. `importer.py`: idempotent by `id`; parses the Markdown-with-front-matter and JSONL formats in
   `v4-server-memory-export.md`; imported items always `candidate`, `source` as declared but never above
   `operator-stated` unless a human confirms; **never overwrites a confirmed card** (creates a conflict
   candidate); rejects cards containing secret-shaped content (redaction detector) with a clear error;
   a CLI entry `python -m planet_express.memory.importer <dir> --db <path> [--dry-run]`.
6. A tiny synthetic fixture set under `tests/fixtures/v4/memory/` (≥ 12 cards across 3 incident shapes:
   stale network namespace after recreate, stale NFS handle, disk pressure with held rollback) written from
   public-safe, invented facts only (no real hostnames, IPs, tokens).

## Acceptance tests (write first; `tests/test_memory_*.py`)
- Cards round-trip through both import formats; malformed cards rejected with precise errors.
- Imported/runtime-proposed cards are `candidate`; `retrieve()` never returns them.
- Runtime/system actor cannot confirm a procedure/constraint; operator can; every transition appends history.
- Import never overwrites a confirmed card; re-import is idempotent; a card with a planted secret is rejected.
- Retrieval returns the right confirmed cards for the three fixture incident shapes (scope+query), respects
  count and byte caps, orders as specified; supersession hides superseded cards; stale cards appear in
  `review_due`.
- FTS stays in sync across insert/update/delete (trigger tests).
- Importing plain Markdown notes alone works with no other PE state.

## Constraints
- New package only; no edits to existing modules. No network, no shell. Match surrounding style.
- Report: files, tests + results, deviations, open questions. Stop and report on ambiguity; do not widen scope.
