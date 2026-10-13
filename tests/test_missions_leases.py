import threading

import pytest
from missions_helpers import make_store, new_mission

from planet_express.missions import leases
from planet_express.missions.leases import (
    LeaseConflict,
    LeaseLost,
    LeaseSuspect,
    StaleFence,
)
from planet_express.missions.store import MissionStore

H = "host:local/"
STACK, SVC, OTHER_SVC = H + "stack:media", H + "service:media/qbit", H + "service:other/x"
MOUNT, SUBMOUNT, UNDER, ELSEWHERE = H + "mount:/data", H + "mount:/data/tv", H + "path:/data/tv/a", H + "path:/etc"


@pytest.mark.parametrize(("a", "b", "expected"), [
    (STACK, SVC, True), (SVC, STACK, True), (STACK, OTHER_SVC, False), (SVC, SVC, True),
    (SVC, H + "service:media/sonarr", False),
    (STACK, MOUNT, False), (STACK, H + "stack:media2", False),
    (MOUNT, SUBMOUNT, True), (SUBMOUNT, MOUNT, True), (MOUNT, UNDER, True), (UNDER, MOUNT, True),
    (SUBMOUNT, UNDER, True), (MOUNT, ELSEWHERE, False), (UNDER, H + "path:/data/tv/b", False),
    (H + "mount:/data", H + "mount:/database", False),
    (H + "mount:/", UNDER, True),
    (STACK, "host:other/stack:media", False),
    (H + "unit:x.service", SVC, False), (H + "unit:x.service", H + "unit:x.service", True),
])
def test_overlap_matrix(a, b, expected):
    assert leases.overlaps(a, b) is expected


@pytest.mark.parametrize("bad", ["stack:media", "host:local/stack", "host:local/path:rel",
                                 "host:local/path:/a/../b", "host:local/service:nostack", "host:local/zzz:x"])
def test_malformed_ids_rejected(bad):
    with pytest.raises(ValueError):
        leases.parse_resource(bad)


def two_missions(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    return store, clock, new_mission(ms).id, new_mission(ms).id


def test_conflicts_by_containment_and_all_or_none(tmp_path):
    store, _, a, b = two_missions(tmp_path)
    fences = leases.acquire(store, [STACK], a, 60)
    assert set(fences) == {STACK}
    with pytest.raises(LeaseConflict):
        leases.acquire(store, [SVC], b, 60)
    # all-or-none: the free resource in the same request must not stay leased
    with pytest.raises(LeaseConflict):
        leases.acquire(store, [OTHER_SVC, SVC], b, 60)
    assert leases.acquire(store, [OTHER_SVC], b, 60)


def test_declared_expansion_takes_children(tmp_path):
    store, _, a, b = two_missions(tmp_path)
    fences = leases.acquire(store, [STACK], a, 60, declared={STACK: [MOUNT, H + "path:/srv/media"]})
    assert set(fences) == {STACK, MOUNT, H + "path:/srv/media"}
    with pytest.raises(LeaseConflict):
        leases.acquire(store, [UNDER], b, 60)


def test_same_mission_reacquire_keeps_fence(tmp_path):
    store, _, a, _ = two_missions(tmp_path)
    f1 = leases.acquire(store, [SVC], a, 60)
    assert leases.acquire(store, [STACK, SVC], a, 60)[SVC] == f1[SVC]


def test_concurrent_acquire_exactly_one_wins(tmp_path):
    store, _, a, b = two_missions(tmp_path)
    barrier = threading.Barrier(2)
    results = {}

    def go(mid):
        barrier.wait()
        try:
            results[mid] = leases.acquire(store, [STACK], mid, 60)
        except LeaseConflict as exc:
            results[mid] = exc

    threads = [threading.Thread(target=go, args=(m,)) for m in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    wins = [m for m, r in results.items() if isinstance(r, dict)]
    assert len(wins) == 1 and len(results) == 2


def test_fence_strictly_increases_across_release_and_reacquire(tmp_path):
    store, _, a, b = two_missions(tmp_path)
    seen = []
    for mid in (a, b, a, b, a):
        seen.append(leases.acquire(store, [SVC], mid, 60)[SVC])
        assert leases.release(store, mid) == 1
    assert seen == sorted(set(seen)) and len(seen) == 5


def test_expiry_marks_suspect_and_does_not_free(tmp_path):
    store, clock, a, b = two_missions(tmp_path)
    f = leases.acquire(store, [SVC], a, 30)[SVC]
    clock.t += 31
    # not freed by the clock, even before any sweep
    with pytest.raises(LeaseConflict):
        leases.acquire(store, [SVC], b, 60)
    assert leases.mark_expired_suspect(store) == [SVC]
    with pytest.raises(LeaseConflict):
        leases.acquire(store, [SVC], b, 60)
    with pytest.raises(LeaseSuspect):
        leases.release(store, a)
    with pytest.raises(StaleFence):
        leases.check_fence(store, SVC, a, f)
    with pytest.raises(ValueError):
        leases.release_after_proven_dead(store, a, " ")
    assert leases.release_after_proven_dead(store, a, "cgroup empty, token revoked") == 1
    assert leases.acquire(store, [SVC], b, 60)[SVC] > f


def test_same_mission_cannot_revive_an_expired_lease_by_reacquiring(tmp_path):
    store, clock, a, _ = two_missions(tmp_path)
    f = leases.acquire(store, [SVC], a, 30)[SVC]
    clock.t += 31
    with pytest.raises(StaleFence):
        leases.check_fence(store, SVC, a, f)
    with pytest.raises(LeaseSuspect):
        leases.acquire(store, [SVC], a, 30)
    with pytest.raises(StaleFence):
        leases.check_fence(store, SVC, a, f)


def test_heartbeat_extends_and_late_heartbeat_loses(tmp_path):
    store, clock, a, _ = two_missions(tmp_path)
    f = leases.acquire(store, [SVC], a, 30)[SVC]
    clock.t += 20
    leases.heartbeat(store, a, 30)
    clock.t += 20
    leases.check_fence(store, SVC, a, f)
    clock.t += 31
    with pytest.raises(LeaseLost):
        leases.heartbeat(store, a, 30)
    with pytest.raises(StaleFence):
        leases.check_fence(store, SVC, a, f)


def test_stale_and_foreign_fences_rejected(tmp_path):
    store, _, a, b = two_missions(tmp_path)
    f = leases.acquire(store, [SVC], a, 60)[SVC]
    leases.check_fence(store, SVC, a, f)
    for args in ((SVC, a, f - 1), (SVC, a, f + 1), (SVC, b, f), (SVC + "x", a, f)):
        with pytest.raises(StaleFence):
            leases.check_fence(store, *args)
    leases.release(store, a)
    with pytest.raises(StaleFence):
        leases.check_fence(store, SVC, a, f)


def test_mark_suspect_explicit(tmp_path):
    store, _, a, _ = two_missions(tmp_path)
    leases.acquire(store, [SVC, STACK], a, 60)
    assert leases.mark_suspect(store, a, [SVC]) == [SVC]
    assert leases.mark_suspect(store, a) == [STACK]
