import sqlite3
import threading

import pytest
from missions_helpers import advance_to, make_store, new_mission, repair_grant, token

from planet_express.missions import leases
from planet_express.missions.approvals import record_approval
from planet_express.missions.models import Grant
from planet_express.missions.state import IllegalTransition
from planet_express.missions.store import MissionStore, SessionConflict

KW = dict(kind="local_cgroup", session_ttl_s=600, lease_ttl_s=60)


def lease_rows(store):
    with sqlite3.connect(store.path) as conn:
        return conn.execute("SELECT COUNT(*) FROM mission_leases").fetchone()[0]


def ready(tmp_path, grant=None):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, grant)
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    return store, ms, m


def test_missing_approval_refused_and_no_lease_left_behind(tmp_path):
    store, ms, m = ready(tmp_path)
    with pytest.raises(IllegalTransition):
        ms.start_session(m.id, token_hash=token(), **KW)
    assert lease_rows(store) == 0
    assert ms.live_session(m.id) is None and ms.get_mission(m.id).status == "awaiting_decision"


def test_approved_start_takes_leases_session_and_status(tmp_path):
    store, ms, m = ready(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    sid = ms.start_session(m.id, token_hash=token(), cgroup_path="/sys/fs/cgroup/pe/x", **KW)
    live = ms.live_session(m.id)
    assert live["id"] == sid and live["cgroup_path"] == "/sys/fs/cgroup/pe/x"
    assert ms.get_mission(m.id).status == "executing"
    assert list(eval_fence(live)) == ["host:local/stack:media"]
    leases.check_fence(store, "host:local/stack:media", m.id, eval_fence(live)["host:local/stack:media"])


def eval_fence(live):
    import json
    return json.loads(live["fence_json"])


def test_second_start_fails_and_revoke_allows_new_session(tmp_path):
    store, ms, m = ready(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    sid = ms.start_session(m.id, token_hash=token(1), **KW)
    with pytest.raises(SessionConflict):
        ms.start_session(m.id, token_hash=token(2), **KW)
    assert ms.live_session(m.id)["id"] == sid and lease_rows(store) == 1
    ms.revoke_session(sid)
    assert ms.live_session(m.id) is None


def test_unique_index_is_the_backstop(tmp_path):
    store, ms, m = ready(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    ms.start_session(m.id, token_hash=token(1), **KW)
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO mission_sessions (id, mission_id, token_hash, issued_at, expires_at, kind, "
                     "fence_json) VALUES ('S-x', ?, 'h', 1, 2, 'local_uid', '{}')", (m.id,))


def test_concurrent_starts_yield_one_session(tmp_path):
    store, ms, m = ready(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    barrier = threading.Barrier(2)
    out = []

    def go(n):
        barrier.wait()
        try:
            out.append(ms.start_session(m.id, token_hash=token(n), **KW))
        except (SessionConflict, IllegalTransition) as exc:
            out.append(exc)

    threads = [threading.Thread(target=go, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, str) for r in out) == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_sessions").fetchone()[0] == 1
    assert lease_rows(store) == 1


def test_lease_conflict_rolls_back_status_and_session(tmp_path):
    store, ms, m = ready(tmp_path)
    other = new_mission(ms)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    leases.acquire(store, ["host:local/service:media/qbit"], other.id, 60)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(m.id, token_hash=token(), **KW)
    assert ms.get_mission(m.id).status == "awaiting_decision" and ms.live_session(m.id) is None
    assert lease_rows(store) == 1


def test_observe_session_takes_no_leases_and_never_executes(tmp_path):
    store, ms, m = ready(tmp_path, Grant.model_validate({"tier": "observe", "scope": {"stacks": ["media"]}}))
    advance_to(ms, m.id, "needs_operator", "investigating")
    ms.start_session(m.id, token_hash=token(), **KW)
    assert ms.get_mission(m.id).status == "investigating" and lease_rows(store) == 0


def test_bad_token_hash_rejected(tmp_path):
    _, ms, m = ready(tmp_path)
    with pytest.raises(ValueError):
        ms.start_session(m.id, token_hash="plaintext-token", **KW)


def test_explicit_resources_only_add_to_the_grant_set(tmp_path):
    store, ms, m = ready(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    ms.start_session(m.id, token_hash=token(), resources=["host:local/unit:x.service"], **KW)
    with sqlite3.connect(store.path) as conn:
        got = {r[0] for r in conn.execute("SELECT resource FROM mission_leases")}
    assert got == {"host:local/unit:x.service", "host:local/stack:media"}


@pytest.mark.parametrize("extra", [[], ["host:local/unit:x.service"]])
def test_resources_cannot_replace_grant_set_to_dodge_a_conflict(tmp_path, extra):
    store, ms, m = ready(tmp_path)
    other = new_mission(ms)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    leases.acquire(store, ["host:local/stack:media"], other.id, 60)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(m.id, token_hash=token(), resources=extra, **KW)
    assert ms.get_mission(m.id).status == "awaiting_decision"


def test_empty_scope_repair_grant_cannot_start(tmp_path):
    store, ms, m = ready(tmp_path, Grant.model_validate({"tier": "repair"}))
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    with pytest.raises(ValueError, match="nothing to lease"):
        ms.start_session(m.id, token_hash=token(), **KW)
    assert ms.get_mission(m.id).status == "awaiting_decision"


def test_create_mission_validates_before_insert(tmp_path):
    from pydantic import ValidationError
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    with pytest.raises(ValidationError):
        ms.create_mission(title="x" * 257, origin="operator", grant=repair_grant(), goal="g",
                          success_criteria="s", runtime="r")
    with pytest.raises(ValidationError):
        ms.create_mission(title="x", origin="nonsense", grant=repair_grant(), goal="g",
                          success_criteria="s", runtime="r")
    assert ms.list_missions() == []


# ── a stack lease also leases the stack's directory (G4 final review) ──────────────────────────────────────────────

def started_mission(store, ms, grant):
    m = new_mission(ms, grant)
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    return m


def test_a_stack_lease_conflicts_with_a_path_lease_inside_the_stack_directory(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    ms.stacks_root = "/srv/stacks"
    a = started_mission(store, ms, repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}))
    b = started_mission(store, ms, repair_grant(scope={"paths": ["/srv/stacks/media/config"], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(b.id, token_hash=token(2), **KW)             # the same files, reached by two different names
    assert ms.live_session(b.id) is None and ms.get_mission(b.id).status == "awaiting_decision"


def test_the_conflict_works_the_other_way_round_and_leaves_disjoint_paths_alone(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    ms.stacks_root = "/srv/stacks"
    b = started_mission(store, ms, repair_grant(scope={"paths": ["/srv/stacks/media/config"], "hosts": ["local"]}))
    elsewhere = started_mission(store, ms, repair_grant(scope={"stacks": ["photos"], "hosts": ["local"]}))
    a = started_mission(store, ms, repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}))
    ms.start_session(b.id, token_hash=token(1), **KW)
    ms.start_session(elsewhere.id, token_hash=token(2), **KW)          # a different stack's directory is unrelated
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(a.id, token_hash=token(3), **KW)


def test_without_a_stacks_root_behaviour_is_unchanged(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)                                            # no stacks_root: the old, narrower behaviour
    a = started_mission(store, ms, repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}))
    b = started_mission(store, ms, repair_grant(scope={"paths": ["/srv/stacks/media/config"], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    ms.start_session(b.id, token_hash=token(2), **KW)


def test_stack_lease_paths_are_normalised_for_the_lease_module():
    from planet_express.missions.store import stack_lease_paths
    grant = repair_grant(scope={"stacks": ["media"], "hosts": ["local", "edge"]})
    out = stack_lease_paths(grant, "/srv//stacks/./")
    assert out == {"host:local/stack:media": ["host:local/path:/srv/stacks/media"],
                   "host:edge/stack:media": ["host:edge/path:/srv/stacks/media"]}
    leases.parse_resource(out["host:local/stack:media"][0])             # accepted by the strict parser
    assert stack_lease_paths(grant, None) == {}


def test_two_service_scoped_missions_in_one_stack_conflict_because_they_share_the_compose_file(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    ms.stacks_root = "/srv/stacks"
    a = started_mission(store, ms, repair_grant(scope={"services": ["media/sonarr"], "hosts": ["local"]}))
    b = started_mission(store, ms, repair_grant(scope={"services": ["media/radarr"], "hosts": ["local"]}))
    c = started_mission(store, ms, repair_grant(scope={"services": ["photos/immich"], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(b.id, token_hash=token(2), **KW)             # one docker-compose.yml, two writers
    ms.start_session(c.id, token_hash=token(3), **KW)                   # a different stack is unrelated


def test_a_symlinked_stacks_root_cannot_hide_an_overlap(tmp_path):
    from planet_express.missions.store import stack_lease_paths
    real = tmp_path / "real-stacks"
    real.mkdir()
    link = tmp_path / "stacks"
    link.symlink_to(real)
    out = stack_lease_paths(repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}), str(link))
    assert sorted(out["host:local/stack:media"]) == sorted([f"host:local/path:{link}/media", f"host:local/path:{real}/media"])


def test_a_relative_stacks_root_is_refused_rather_than_leased_wrongly():
    from planet_express.missions.store import stack_lease_paths
    with pytest.raises(ValueError, match="absolute"):
        stack_lease_paths(repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}), "stacks")


def test_leases_cover_the_configured_alias_and_the_resolved_root_of_the_stacks_directory(tmp_path):
    from planet_express.missions.store import stack_lease_paths
    real = tmp_path / "real-stacks"
    real.mkdir()
    link = tmp_path / "stacks"
    link.symlink_to(real)
    grant = repair_grant(scope={"stacks": ["media"], "hosts": ["local"]})
    # the checkpoint service knows only the resolved root; the tripwire knows the configured one: both are passed
    out = stack_lease_paths(grant, (str(real), str(link)))
    assert sorted(out["host:local/stack:media"]) == sorted([f"host:local/path:{real}/media", f"host:local/path:{link}/media"])

    store, clock = make_store(tmp_path / "db")
    ms = MissionStore(store)
    ms.stacks_roots = (str(real), str(link))
    a = started_mission(store, ms, grant)
    b = started_mission(store, ms, repair_grant(scope={"paths": [f"{link}/media/config"], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(b.id, token_hash=token(2), **KW)             # a path grant written under the alias still collides


def test_a_stack_directory_that_is_itself_a_symlink_is_leased_by_its_destination_too(tmp_path):
    from planet_express.missions.store import stack_lease_paths
    root = tmp_path / "stacks"
    root.mkdir()
    target = tmp_path / "elsewhere" / "media-real"
    target.mkdir(parents=True)
    (root / "media").symlink_to(target)
    grant = repair_grant(scope={"stacks": ["media"], "services": ["media/sonarr"], "hosts": ["local"]})
    out = stack_lease_paths(grant, str(root))
    for key in ("host:local/stack:media", "host:local/service:media/sonarr"):
        assert f"host:local/path:{target}" in out[key] and f"host:local/path:{root}/media" in out[key]
    store, clock = make_store(tmp_path / "db")
    ms = MissionStore(store)
    ms.stacks_roots = (str(root),)
    a = started_mission(store, ms, grant)
    b = started_mission(store, ms, repair_grant(scope={"paths": [f"{target}/config"], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(b.id, token_hash=token(2), **KW)             # the same files, reached through the symlink's destination


def test_a_path_scope_that_is_a_symlink_alias_is_leased_where_it_really_points(tmp_path):
    from planet_express.missions.store import stack_lease_paths
    real = tmp_path / "stacks" / "media"
    real.mkdir(parents=True)
    alias = tmp_path / "stacks" / "media-alias"
    alias.symlink_to(real)
    out = stack_lease_paths(repair_grant(scope={"paths": [str(alias)], "hosts": ["local"]}), str(tmp_path / "stacks"))
    assert out[f"host:local/path:{alias}"] == [f"host:local/path:{real}"]
    store, clock = make_store(tmp_path / "db")
    ms = MissionStore(store)
    ms.stacks_roots = (str(tmp_path / "stacks"),)
    a = started_mission(store, ms, repair_grant(scope={"stacks": ["media"], "hosts": ["local"]}))
    b = started_mission(store, ms, repair_grant(scope={"paths": [str(alias)], "hosts": ["local"]}))
    ms.start_session(a.id, token_hash=token(1), **KW)
    with pytest.raises(leases.LeaseConflict):
        ms.start_session(b.id, token_hash=token(2), **KW)             # media and media-alias are the same directory
