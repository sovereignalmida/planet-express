# S3 shared interface (read by S3a and S3b; do not change without the orchestrator)

`planet_express/missions/containment.py` is owned by S3a (it only declares the types below; S3b
implements them elsewhere). Both agents code to this exactly.

```python
@dataclass(frozen=True)
class ContainedSession:
    session_id: str
    kind: Literal["local_cgroup", "local_uid", "fake"]
    cgroup_path: str | None          # absolute path under the cgroup v2 mount
    session_uid: int | None          # the uid the process runs as (worker identity)

class SessionContainment(Protocol):
    def create(self, session_id: str) -> ContainedSession: ...
    def spawn(self, cs: ContainedSession, argv: list[str], env: dict[str, str], cwd: str,
              pass_fds: tuple[int, ...] = ()) -> int: ...   # returns the leader pid, already inside cs
    def pids(self, cs: ContainedSession) -> list[int]: ...
    def freeze(self, cs: ContainedSession) -> None: ...
    def thaw(self, cs: ContainedSession) -> None: ...
    def kill(self, cs: ContainedSession) -> None: ...        # freeze, kill every member, wait until empty
    def is_empty(self, cs: ContainedSession) -> bool: ...    # the ONLY "provably dead" predicate
    def cleanup(self, cs: ContainedSession) -> None: ...     # remove cgroup dir etc; only when empty
```

`planet_express/missions/runtime.py` (S3a) declares the adapter protocol and result models:
`AgentRuntime.start/status/cancel/collect` per brief §7, `RunHandle`, `AgentUpdate`,
`AgentResult` (strict, bounded: `diagnosis`, `evidence[]`, `suggested_actions[]`,
`verification_plan` (optional), `memory_candidates[]`, `unknowns[]`, `confidence`). S3b's command
adapter returns exactly this, parsed from the child's final JSON.

Worker RPC (S3b): methods `mission.get_context`, `mission.post_event`, `mission.heartbeat`,
`mission.submit_result`, `memory.search` on a separate socket; handlers receive `(params,
session)` where `session` is the resolved `mission_sessions` row (S3a provides
`resolve_session(token_hash, peer_cgroup|peer_uid) -> SessionRow | None` in
`planet_express/missions/sessions.py`; S3b calls it).

Rules for everyone: Observe/Propose only in this slice (no mutation capability exists), missions
OFF by default, no new dependencies, test-first, no shell strings (argv lists only), match the repo
style, and never write real hostnames/IPs/credentials into fixtures. Before starting, run
`git merge --ff-only v4-next` in your worktree (it may be cut from an older base) and verify
`docs/handoff/v4/` exists. Commit with the Co-Authored-By trailer; do not push.
