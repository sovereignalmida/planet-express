"""Resource leases with fences (contract §2, §2.1, §2.3).

A lease is never freed by the clock. Expiry only marks it suspect (the holder may still be running and
native Docker mutations carry no fence), and only `release_after_proven_dead` frees a suspect lease.
Fences come from `resource_fences`, which is never deleted, so a fence is never reused.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from planet_express.core.store import Store

from .events import insert_event


class LeaseConflict(RuntimeError):
    def __init__(self, resource: str, held: str, holder: str):
        super().__init__(f"{resource} overlaps {held}, leased by {holder}")
        self.resource, self.held, self.holder = resource, held, holder


class LeaseSuspect(RuntimeError):
    """The lease is suspect; only release_after_proven_dead may free it."""


class LeaseLost(RuntimeError):
    """The holder's lease expired before it heartbeat; it is now suspect."""


class StaleFence(RuntimeError):
    """The presented fence is not the current, live fence of a lease held by that mission."""


class Resource(NamedTuple):
    host: str
    kind: str
    name: str


_FS_KINDS = ("mount", "path")
_KINDS = ("stack", "service", "mount", "path", "unit", "volume", "network", "route", "dns")


def parse_resource(rid: str) -> Resource:
    if not rid.startswith("host:") or "/" not in rid:
        raise ValueError(f"malformed resource id {rid!r}")
    host, rest = rid[5:].split("/", 1)
    kind, sep, name = rest.partition(":")
    if not host or not sep or not name or kind not in _KINDS:
        raise ValueError(f"malformed resource id {rid!r}")
    if kind in _FS_KINDS:
        parts = name.split("/")
        if not name.startswith("/") or ".." in parts or "." in parts or (len(name) > 1 and "" in parts[1:]):
            raise ValueError(f"resource path must be absolute and normalised: {rid!r}")
    if kind == "service" and name.count("/") != 1:
        raise ValueError(f"service resource must be <stack>/<service>: {rid!r}")
    return Resource(host, kind, name)


def _fs_within(inner: str, outer: str) -> bool:
    return inner == outer or outer == "/" or inner.startswith(outer.rstrip("/") + "/")


def contains(outer: str, inner: str) -> bool:
    """§2.3 containment. A stack contains its services, mounts and paths contain whatever filesystem
    location lies under them (a path over a nested mount is treated as overlap too: the conservative choice). Anything else is exact match only; a stack's compose
    paths and declared mounts enter through `declared` at acquire time, not here."""
    if outer == inner:
        return True
    a, b = parse_resource(outer), parse_resource(inner)
    if a.host != b.host:
        return False
    if a.kind == "stack" and b.kind == "service":
        return b.name.split("/", 1)[0] == a.name
    if a.kind in _FS_KINDS and b.kind in _FS_KINDS:
        return _fs_within(b.name, a.name)
    return False


def overlaps(a: str, b: str) -> bool:
    return contains(a, b) or contains(b, a)


def expand(resources: Sequence[str], declared: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """The request plus whatever the caller declares each resource contains (a stack's compose paths and
    mounts). Order is stable and duplicates are dropped."""
    out: dict[str, None] = {}
    for rid in resources:
        parse_resource(rid)
        out[rid] = None
        for extra in (declared or {}).get(rid, ()):
            parse_resource(extra)
            out[extra] = None
    return list(out)


def _now(store: Store) -> int:
    return int(store._clock())


def next_fence(conn: sqlite3.Connection, resource: str) -> int:
    row = conn.execute(
        "INSERT INTO resource_fences (resource, fence) VALUES (?, 1) "
        "ON CONFLICT(resource) DO UPDATE SET fence = fence + 1 RETURNING fence",
        (resource,),
    ).fetchone()
    return int(row[0])


def acquire_in_txn(conn: sqlite3.Connection, resources: Sequence[str], mission_id: str, ttl: int,
                   now: int, declared: Mapping[str, Sequence[str]] | None = None) -> dict[str, int]:
    """All-or-none inside the caller's BEGIN IMMEDIATE. Returns resource -> fence."""
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    wanted = expand(resources, declared)
    held = conn.execute("SELECT resource, mission_id FROM mission_leases").fetchall()
    for want in wanted:
        for row in held:
            if row["mission_id"] != mission_id and overlaps(want, row["resource"]):
                raise LeaseConflict(want, row["resource"], row["mission_id"])
    fences: dict[str, int] = {}
    for want in wanted:
        mine = conn.execute(
            "SELECT fence, expires_at, suspect_at FROM mission_leases WHERE resource=? AND mission_id=?",
            (want, mission_id),
        ).fetchone()
        if mine is not None:
            if mine["suspect_at"] is not None or mine["expires_at"] <= now:
                # An expired lease is never revived by re-acquiring it: the old fence stays stale until
                # Core proves the holder dead and releases it (§2.1).
                raise LeaseSuspect(f"{want} is suspect or expired")
            conn.execute(
                "UPDATE mission_leases SET expires_at=?, last_heartbeat=? WHERE resource=?",
                (now + ttl, now, want),
            )
            fences[want] = int(mine["fence"])
            continue
        fence = next_fence(conn, want)
        conn.execute(
            "INSERT INTO mission_leases (resource, mission_id, fence, acquired_at, expires_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (want, mission_id, fence, now, now + ttl, now),
        )
        fences[want] = fence
    return fences


def acquire(store: Store, resources: Sequence[str], mission_id: str, ttl: int,
            declared: Mapping[str, Sequence[str]] | None = None) -> dict[str, int]:
    with store._write() as conn:
        return acquire_in_txn(conn, resources, mission_id, ttl, _now(store), declared)


def heartbeat(store: Store, mission_id: str, ttl: int) -> None:
    """Refresh every lease the mission holds. A lease already past expiry is not revived: it becomes
    suspect and the caller learns it lost it."""
    now = _now(store)
    lost: list[str] = []
    with store._write() as conn:
        for row in conn.execute(
            "SELECT resource, expires_at, suspect_at FROM mission_leases WHERE mission_id=?", (mission_id,)
        ).fetchall():
            if row["suspect_at"] is not None or row["expires_at"] <= now:
                lost.append(row["resource"])
                if row["suspect_at"] is None:
                    conn.execute("UPDATE mission_leases SET suspect_at=? WHERE resource=?",
                                 (now, row["resource"]))
        if not lost:
            conn.execute(
                "UPDATE mission_leases SET expires_at=?, last_heartbeat=? WHERE mission_id=?",
                (now + ttl, now, mission_id),
            )
    if lost:
        raise LeaseLost(f"lease lost for {', '.join(sorted(lost))}")


def release(store: Store, mission_id: str, resources: Sequence[str] | None = None) -> int:
    """The holder gives leases back. Refused while any of them is suspect: a holder that may be dead
    or runaway does not get to free its own resources."""
    with store._write() as conn:
        rows = conn.execute("SELECT resource, suspect_at FROM mission_leases WHERE mission_id=?",
                            (mission_id,)).fetchall()
        chosen = [r for r in rows if resources is None or r["resource"] in set(resources)]
        if any(r["suspect_at"] is not None for r in chosen):
            raise LeaseSuspect("suspect leases are freed only by release_after_proven_dead")
        for r in chosen:
            conn.execute("DELETE FROM mission_leases WHERE resource=?", (r["resource"],))
        return len(chosen)


def mark_suspect(store: Store, mission_id: str, resources: Sequence[str] | None = None) -> list[str]:
    now = _now(store)
    with store._write() as conn:
        rows = conn.execute(
            "SELECT resource FROM mission_leases WHERE mission_id=? AND suspect_at IS NULL", (mission_id,)
        ).fetchall()
        marked = [r["resource"] for r in rows if resources is None or r["resource"] in set(resources)]
        for rid in marked:
            conn.execute("UPDATE mission_leases SET suspect_at=? WHERE resource=?", (now, rid))
        return marked


def mark_expired_suspect(store: Store) -> list[str]:
    """Expiry sweep: leases past `expires_at` become suspect. Nothing is freed."""
    now = _now(store)
    with store._write() as conn:
        rows = conn.execute(
            "SELECT resource FROM mission_leases WHERE expires_at <= ? AND suspect_at IS NULL", (now,)
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE mission_leases SET suspect_at=? WHERE resource=?", (now, r["resource"]))
        return [r["resource"] for r in rows]


def release_after_proven_dead(store: Store, mission_id: str, proof: str) -> int:
    """Core calls this only after the session's cgroup/uid is confirmed empty and its token and
    endpoints are revoked (§2.1). `proof` is recorded; an empty one is refused."""
    if not proof.strip():
        raise ValueError("a proof of death is required")
    now = _now(store)
    with store._write() as conn:
        rows = conn.execute("SELECT resource FROM mission_leases WHERE mission_id=?", (mission_id,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM mission_leases WHERE resource=?", (r["resource"],))
        if rows:
            insert_event(conn, mission_id, "decision", "core",
                         {"leases_released_after_proven_dead": sorted(r["resource"] for r in rows),
                          "proof": proof}, now)
        return len(rows)


def check_fence(store: Store, resource: str, mission_id: str, fence: int) -> None:
    """Raise StaleFence unless `fence` is the live, non-suspect, unexpired fence of this mission's lease."""
    now = _now(store)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT mission_id, fence, expires_at, suspect_at FROM mission_leases WHERE resource=?",
            (resource,),
        ).fetchone()
    if (row is None or row["mission_id"] != mission_id or row["fence"] != fence
            or row["suspect_at"] is not None or row["expires_at"] <= now):
        raise StaleFence(f"stale fence {fence} for {resource}")
