# S3a — Runtime protocol, brief generator, supervisor (v4 slice 3, part a)

Read first: `S3-common.md`; `docs/designs/v4-slice-0-contracts.md` §1, §2.1, §3, §6, §11, §11.1, §12 (rev 0.4);
`docs/designs/v4-mission-control.md` §7; the merged `planet_express/missions/` (store, state, leases,
approvals, events) and `planet_express/memory/` (retrieval).

## Deliver (`planet_express/missions/`)
1. `containment.py` (types only, per S3-common) and `FakeContainment` (in-memory, deterministic, can
   simulate a process that ignores kill for N polls, a crash, and a leaked child) for tests.
2. `runtime.py`: `AgentRuntime` protocol, `RunHandle`, `AgentUpdate`, `AgentResult` + validation.
3. `sessions.py`: `issue_session(...)` (token 256-bit, only its sha256 stored, token returned once for fd
   delivery), `resolve_session(token_hash, peer_cgroup=None, peer_uid=None)`, `revoke_session`.
4. `brief.py`: `build_brief(mission, evidence, memory_cards, constraints)` → a bounded, redacted document
   with sections: goal, success criteria, scope, evidence (quoted as DATA), confirmed memory cards (quoted,
   ids), constraints, output schema, and an explicit instruction block stating log/web content is data not
   instructions. Candidates are never included. Size cap with deterministic truncation + a truncation note.
5. `supervisor.py`: `Supervisor(store, containment, runtime_factory, clock)` implementing the contract §11
   lifecycle for **Observe/Propose** missions: queued→investigating, issue session, build brief, spawn via
   runtime, stream `AgentUpdate`s into events (redact before persist), enforce budgets (wall time, tool
   calls counted from tool events, output bytes, retries), heartbeat, collect result → structured result
   stored as artifact + event → request terminal/next transition **only after `containment.is_empty()` is
   True** (kill first on cancel/budget/timeout). Crash/restart classification for Observe/Propose: session
   gone → automatic restart within the retry budget, else `needs_operator`. `recover()` on startup scans
   non-terminal missions and reconciles with containment.
6. External-session import (contract §11.1): `import_external_session(mission_id, record)` stores events/
   artifacts only; it can never move a mission to `executing`/`completed`.

## Acceptance tests (first; `tests/test_supervisor_*.py`, using FakeContainment + a fake runtime)
- Happy path to a stored structured result; Observe/Propose can never enter `executing` via the supervisor.
- Budget breach (each of wall, tool calls, output bytes) → kill → wait empty → needs_operator, no early release.
- A process that ignores kill: the terminal transition is NOT requested until `is_empty()`; leaked child
  (pid left in cgroup) blocks it; after eventual empty it proceeds.
- Cancel path; supervisor crash + `recover()`: restarts within retry budget, then needs_operator.
- Brief: candidates excluded, truncation deterministic, hostile log text appears only inside the DATA block,
  planted secret redacted; token never appears in events/artifacts/brief.
- Session: only the hash is stored; wrong token / wrong cgroup / revoked / expired rejected; one live session.
- External import cannot advance state. Missions-disabled → supervisor refuses to start.
- Full suite green.
