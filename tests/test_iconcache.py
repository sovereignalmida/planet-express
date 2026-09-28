"""The dashboard's only outbound internet call.

Everything else it fetches is a container on a bridge address, so these tests are mostly about
what this refuses: the wrong host, the wrong bytes, too many bytes, and a path that is not a
path. The happy case is one test; the rest is the boundary.
"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG",
                      str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core import iconcache
from planet_express.core.iconcache import PNG_MAGIC, IconCache

PNG = PNG_MAGIC + b"rest of a perfectly good png"


class FakeResponse:
    """A response that is consumed as it is read, the way a socket is.

    The first version returned the same prefix on every call. Nothing noticed while the code
    read once; the moment it read in chunks, every fetch looped on the same bytes until it
    tripped the size cap. A double that cannot run out is not a stream.
    """

    def __init__(self, status, body):
        self.status, self._body, self._at = status, body, 0

    def read(self, amount=None):
        end = len(self._body) if amount is None else min(len(self._body), self._at + amount)
        chunk = self._body[self._at:end]
        self._at = end
        return chunk


class FakeConnection:
    """Records what was asked for, so a test can assert the URL was built and not passed in."""
    def __init__(self, calls, status=200, body=PNG, raises=None):
        self.calls, self.status, self.body, self.raises = calls, status, body, raises
        self.host = None

    def __call__(self, host, timeout=None):
        self.host = host
        return self

    def request(self, method, path, headers=None):
        if self.raises:
            raise self.raises
        self.calls.append((method, self.host, path, dict(headers or {})))

    def getresponse(self):
        return FakeResponse(self.status, self.body)

    def close(self):
        pass


@pytest.fixture
def cache(tmp_path):
    calls = []
    factory = FakeConnection(calls)
    c = IconCache(tmp_path / "icons", connection_factory=factory)
    c.calls, c.factory = calls, factory
    return c


def test_an_icon_is_fetched_once_and_then_served_from_disk(cache):
    assert cache.warm(["sonarr"]) == {"fetched": 1, "cached": 0, "missing": 0, "skipped": 0}
    assert cache.path_for("sonarr").read_bytes() == PNG
    assert cache.warm(["sonarr"])["cached"] == 1
    assert len(cache.calls) == 1, "a cached icon must not be fetched again"


def test_the_url_is_built_here_from_a_pinned_host(cache):
    cache.warm(["sonarr"])
    method, host, path, headers = cache.calls[0]
    assert method == "GET"
    assert host == iconcache.CDN_HOST == "cdn.jsdelivr.net"
    assert path == "/gh/selfhst/icons/png/sonarr.png"
    assert headers["Host"] == iconcache.CDN_HOST


def test_serving_never_reaches_the_network(cache):
    """A page request must not be able to cause an outbound fetch: a slow or hostile CDN
    would otherwise be a slow or hostile dashboard, once per viewer."""
    assert cache.path_for("sonarr") is None
    assert cache.has("sonarr") is False
    assert cache.calls == []


@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "a/b", "sonarr.png", "..", "", "-x", "A", None, 7, "x" * 65,
])
def test_a_slug_that_is_not_a_slug_never_becomes_a_path(cache, slug):
    assert cache.path_for(slug) is None
    assert cache.warm([slug])["fetched"] == 0
    assert cache.calls == [], "a bad slug must not even be requested"


def test_a_symlink_out_of_the_cache_directory_is_not_served(cache, tmp_path):
    cache.dir.mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"not yours")
    (cache.dir / "sonarr.png").symlink_to(secret)
    assert cache.path_for("sonarr") is None


def test_a_body_that_is_not_a_png_is_refused(tmp_path):
    """The magic bytes decide, not Content-Type. An SVG here would be script served from our
    own origin; an HTML error page would be cached as an icon."""
    for body in (b"<svg onload=alert(1)>", b"<!DOCTYPE html><h1>404</h1>", b"", b"\x89PN"):
        calls = []
        c = IconCache(tmp_path / f"i{len(body)}",
                      connection_factory=FakeConnection(calls, body=body))
        assert c.warm(["sonarr"])["fetched"] == 0
        assert c.path_for("sonarr") is None


def test_an_oversized_body_is_refused_without_reading_it_all(tmp_path):
    body = PNG_MAGIC + b"x" * (iconcache.MAX_BYTES * 4)
    c = IconCache(tmp_path / "i", connection_factory=FakeConnection([], body=body))
    assert c.warm(["sonarr"])["fetched"] == 0
    assert c.path_for("sonarr") is None


def test_a_404_is_remembered_so_it_is_not_asked_again(tmp_path):
    calls = []
    c = IconCache(tmp_path / "i", connection_factory=FakeConnection(calls, status=404))
    assert c.warm(["nosuchapp"])["missing"] == 1
    assert c.warm(["nosuchapp"])["missing"] == 1
    assert len(calls) == 1, "26 of this host's containers have no icon; asking every scan is waste"


def test_a_remembered_miss_expires(tmp_path, monkeypatch):
    calls = []
    c = IconCache(tmp_path / "i", connection_factory=FakeConnection(calls, status=404))
    c.warm(["nosuchapp"])
    stale = time.time() - iconcache.MISS_TTL - 1
    import os
    os.utime(c._miss_path("nosuchapp"), (stale, stale))
    c.warm(["nosuchapp"])
    assert len(calls) == 2


def test_a_miss_that_later_succeeds_replaces_the_miss(tmp_path):
    c = IconCache(tmp_path / "i", connection_factory=FakeConnection([], status=404))
    c.warm(["sonarr"])
    assert c._miss_path("sonarr").exists()
    c._connect = FakeConnection([], status=200, body=PNG)
    c._miss_path("sonarr").unlink()
    assert c.warm(["sonarr"])["fetched"] == 1
    assert not c._miss_path("sonarr").exists()


def test_a_network_error_is_a_miss_not_a_crash(tmp_path):
    c = IconCache(tmp_path / "i",
                  connection_factory=FakeConnection([], raises=OSError("no route to host")))
    assert c.warm(["sonarr"])["missing"] == 1
    assert c.path_for("sonarr") is None


def test_no_partial_file_is_left_behind(cache):
    cache.warm(["sonarr"])
    assert [p.name for p in cache.dir.glob("*")] == ["sonarr.png"]


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(iconcache, "MAX_CACHED", 3)
    c = IconCache(tmp_path / "i", connection_factory=FakeConnection([]))
    out = c.warm([f"app{n}" for n in range(10)])
    assert out["fetched"] == 3 and out["skipped"] == 7
    assert len(list(c.dir.glob("*.png"))) == 3


def test_duplicate_slugs_are_fetched_once(cache):
    cache.warm(["sonarr", "sonarr", "sonarr"])
    assert len(cache.calls) == 1


def test_an_unwritable_directory_is_survivable(tmp_path):
    blocker = tmp_path / "icons"
    blocker.write_text("this is a file, not a directory")
    c = IconCache(blocker, connection_factory=FakeConnection([]))
    assert c.warm(["sonarr"]) == {"fetched": 0, "cached": 0, "missing": 0, "skipped": 0}
    assert c.path_for("sonarr") is None


def test_a_bad_slug_is_rejected_before_the_filesystem_is_touched(cache, monkeypatch):
    """There are two locks on this door -- valid_slug() and the resolve() parent check -- and
    the traversal test above passes with either one alone. This pins the first: a slug that is
    not a slug is refused without a path ever being built or stat()ed, so the refusal does not
    depend on how resolve() behaves for a directory that may not exist yet.
    """
    def explode(*args, **kwargs):
        raise AssertionError("the filesystem was touched for an invalid slug")

    monkeypatch.setattr(Path, "resolve", explode)
    monkeypatch.setattr(Path, "is_file", explode)
    for slug in ("../../etc/passwd", "a/b", "sonarr.png", "..", "", "-x", "A", None, 7):
        assert cache.path_for(slug) is None


# ── the warm budget, and the resolved map ───────────────────────────────────────

def test_warming_stops_at_its_budget(tmp_path, monkeypatch):
    """Warming runs inside the monitoring scan. Serially, a cold cache against a CDN that
    hangs is TIMEOUT x slugs -- minutes of held scan slot on this host's 59 icons, with every
    finding queued behind it. The scan matters more than any icon."""
    clock = [0.0]
    monkeypatch.setattr(iconcache.time, "monotonic", lambda: clock[0])

    class Slow(FakeConnection):
        def getresponse(self):
            clock[0] += 8          # each fetch costs a full timeout
            return FakeResponse(200, PNG)

    c = IconCache(tmp_path / "i", connection_factory=Slow([]))
    out = c.warm([f"app{n}" for n in range(20)], budget=20)
    # Two complete inside the budget; the third is abandoned while its bytes are arriving,
    # rather than allowed to finish past the deadline. The rest are never started.
    assert out == {"fetched": 2, "cached": 0, "missing": 0, "skipped": 18}, out
    assert len(list(c.dir.glob("*.png"))) == 2
    # None of them is remembered as missing: running out of budget says nothing about whether
    # an icon exists, and a .miss would suppress it for 24 hours.
    assert list(c.dir.glob("*.miss")) == []


def test_what_the_budget_skipped_is_fetched_by_the_next_scan(cache):
    cache.warm(["a", "b"], budget=0)
    assert cache.path_for("a") is None
    assert cache.warm(["a", "b"])["fetched"] == 2


def test_the_resolved_map_survives_a_round_trip(cache):
    cache.warm(["sonarr"])
    cache.write_index({"CASA_SONARR": "sonarr"})
    assert cache.read_index() == {"CASA_SONARR": "sonarr"}


def test_the_resolved_map_omits_icons_that_never_arrived(cache):
    """The map says what each container SHOULD have; the dashboard may only render what is on
    disk. An entry whose file is missing would be an <img> that 404s -- a broken-image glyph
    where a monogram belongs."""
    cache.warm(["sonarr"])
    cache.write_index({"CASA_SONARR": "sonarr", "CASA_GHOST": "nosuchapp"})
    assert cache.read_index() == {"CASA_SONARR": "sonarr"}


@pytest.mark.parametrize("mapping", [
    {"CASA_X": "../../etc/passwd"}, {"CASA_X": "a/b"}, {"CASA_X": ""}, {"CASA_X": 7},
    {"CASA_X": None}, {7: "sonarr"},
])
def test_an_unusable_entry_never_reaches_the_map(cache, mapping):
    cache.warm(["sonarr"])
    cache.write_index(mapping)
    assert cache.read_index() == {}


def test_a_missing_or_corrupt_map_is_empty_not_an_error(cache, tmp_path):
    assert cache.read_index() == {}
    cache.dir.mkdir(parents=True, exist_ok=True)
    (cache.dir / iconcache.INDEX_NAME).write_text("{not json")
    assert cache.read_index() == {}
    (cache.dir / iconcache.INDEX_NAME).write_text('["a list"]')
    assert cache.read_index() == {}


def test_writing_the_map_leaves_no_partial_file(cache):
    cache.warm(["sonarr"])
    cache.write_index({"CASA_SONARR": "sonarr"})
    assert sorted(p.name for p in cache.dir.glob("*")) == ["resolved.json", "sonarr.png"]


def test_an_updates_only_scan_leaves_the_resolved_map_alone(tmp_path, monkeypatch):
    """An updates-only scan omits containers, so the snapshot carries an empty list -- which
    means "not collected", not "this host has no containers". Rebuilding the map from it would
    blank every icon on the dashboard until the next full scan restored them."""
    import casa_farnsworth
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    cache = IconCache(tmp_path / "icons", connection_factory=FakeConnection([]))
    cache.warm(["sonarr"])
    cache.write_index({"CASA_SONARR": "sonarr"})

    casa_farnsworth._warm_container_icons({"containers": []})
    assert cache.read_index() == {"CASA_SONARR": "sonarr"}

    casa_farnsworth._warm_container_icons({})
    assert cache.read_index() == {"CASA_SONARR": "sonarr"}


def _farnsworth(tmp_path, monkeypatch, *, listing, owners):
    """casa_farnsworth._warm_container_icons with docker's two reads stubbed."""
    import casa_farnsworth
    import config
    from planet_express.execution import actions

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(actions, "list_containers", lambda **k: listing)
    monkeypatch.setattr(actions, "read_router_owners", lambda ids, **k: owners)
    # The IconCache _warm_container_icons builds would use the real default connection --
    # `from ... import IconCache` inside a function resolves through the module at call time,
    # so patching it here is what keeps this test off the network.
    real = iconcache.IconCache
    monkeypatch.setattr(iconcache, "IconCache",
                        lambda d, **k: real(d, connection_factory=FakeConnection([])))
    return casa_farnsworth


OK_LISTING = {"ok": True, "containers": [["a" * 64, "CASA_X", "running"]]}
OK_OWNERS = {"ok": True, "icons": {}, "containers": [["a" * 64, "CASA_X", "running"]]}
SNAPSHOT = {"containers": [{"name": "CASA_X", "image": "linuxserver/sonarr"}]}


def test_an_unreadable_label_set_leaves_the_previous_overrides_standing(tmp_path, monkeypatch):
    """A container recreated between the list and the inspect is ordinary churn. Treating the
    resulting empty label set as "nobody sets a label" would replace every override with its
    image-derived fallback -- and turn an explicit `=none` back into an icon."""
    cache = IconCache(tmp_path / "icons", connection_factory=FakeConnection([]))
    cache.warm(["plex"])
    cache.write_index({"CASA_X": "plex"})          # what the label chose, last good scan

    for owners in ({"ok": False, "error": "unavailable"}, {"ok": False, "race": True}):
        _farnsworth(tmp_path, monkeypatch, listing=OK_LISTING,
                    owners=owners)._warm_container_icons(SNAPSHOT)
        assert cache.read_index() == {"CASA_X": "plex"}, owners

    _farnsworth(tmp_path, monkeypatch, listing={"ok": False, "error": "timeout"},
                owners=OK_LISTING)._warm_container_icons(SNAPSHOT)
    assert cache.read_index() == {"CASA_X": "plex"}


def test_a_successful_read_with_no_labels_does_publish(tmp_path, monkeypatch):
    """The ordinary case on this host: nothing sets the label, and the image-derived slug is
    the right answer. Distinguished from the failure above only by the read having worked."""
    cache = IconCache(tmp_path / "icons", connection_factory=FakeConnection([]))
    cache.warm(["plex"])
    cache.write_index({"CASA_X": "plex"})

    _farnsworth(tmp_path, monkeypatch, listing=OK_LISTING,
                owners=OK_OWNERS)._warm_container_icons(SNAPSHOT)
    assert cache.read_index() == {"CASA_X": "sonarr"}


def test_the_docker_reads_are_bounded_by_the_warming_budget(tmp_path, monkeypatch):
    """The icon budget has to cover the docker reads, not just the CDN.

    DOCKER_TIMEOUT_SECONDS is 120. Two unbounded reads in front of a 20-second fetch budget
    meant a stalled daemon could hold the monitoring pipeline for four minutes before the
    advertised budget even began -- for a feature whose failure mode is a monogram.
    """
    import casa_farnsworth
    import config
    from planet_express.execution import actions

    seen = []
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(actions, "list_containers",
                        lambda **k: seen.append(k["timeout"]) or OK_LISTING)
    monkeypatch.setattr(actions, "read_router_owners",
                        lambda ids, **k: seen.append(k["timeout"]) or {"ok": True, "icons": {}})
    real = iconcache.IconCache
    monkeypatch.setattr(iconcache, "IconCache",
                        lambda d, **k: real(d, connection_factory=FakeConnection([])))

    casa_farnsworth._warm_container_icons(SNAPSHOT)
    assert seen, "the docker reads did not run"
    for timeout in seen:
        assert timeout <= actions.RPC_DOCKER_TIMEOUT_SECONDS, (
            f"a cache-warming read may wait {timeout}s while the scan is held")
    assert sum(seen) < casa_farnsworth.ICON_WARM_SECONDS


def test_a_trickling_response_cannot_outlast_the_budget(tmp_path, monkeypatch):
    """TIMEOUT is per socket operation, so it restarts on every chunk that arrives. A server
    sending one byte at a time resets it forever and holds the scan open -- which is why the
    deadline is checked between chunks, in wall-clock, and not only before the first one."""
    clock = [0.0]
    monkeypatch.setattr(iconcache.time, "monotonic", lambda: clock[0])

    class Trickle:
        status = 200

        def read(self, amount=None):
            clock[0] += 1          # a byte a second, forever
            return b"\x89"

    class Dripping(FakeConnection):
        def getresponse(self):
            return Trickle()

    c = IconCache(tmp_path / "i", connection_factory=Dripping([]))
    assert c.warm(["sonarr"], budget=10)["skipped"] == 1
    assert c.path_for("sonarr") is None
    assert clock[0] < 30, f"the download ran {clock[0]}s against a 10s budget"
    assert list(c.dir.glob("*.miss")) == [], "a timed-out icon must still be retried"


def test_a_real_404_is_still_remembered(cache):
    """The distinction that matters: a genuine miss is cached for MISS_TTL, a cancelled one
    is not. 26 of this host's containers have no icon; asking for them every scan is waste."""
    cache._connect = FakeConnection([], status=404)
    assert cache.warm(["nosuchapp"]) == {"fetched": 0, "cached": 0, "missing": 1, "skipped": 0}
    assert [p.name for p in cache.dir.glob("*.miss")] == ["nosuchapp.miss"]


def test_the_scan_does_not_wait_for_the_icon_cache(tmp_path, monkeypatch):
    """The whole reason warming moved off the scan: an optional icon must never hold the
    monitoring pipeline. A bounded HTTP exchange cannot be built from http.client's per-recv
    timeouts -- trickled headers stall getresponse() before any deadline check -- so the scan
    hands the work to a thread and returns.
    """
    import casa_farnsworth
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    started, release = threading.Event(), threading.Event()

    def never_returns(_data, token=None):
        started.set()
        release.wait(10)

    monkeypatch.setattr(casa_farnsworth, "_warm_container_icons", never_returns)
    try:
        begun = time.monotonic()
        casa_farnsworth._start_icon_warm(SNAPSHOT)
        elapsed = time.monotonic() - begun
        assert started.wait(5), "the warm never started"
        assert elapsed < 1, f"the scan waited {elapsed:.1f}s for an icon"

        # A second scan while one is in flight is skipped, not queued or run concurrently:
        # two warms would race to write resolved.json.
        casa_farnsworth._start_icon_warm(SNAPSHOT)
        assert sum(t.name == "icon-warm" for t in threading.enumerate()) == 1
    finally:
        release.set()
def test_a_container_the_inspect_could_not_see_keeps_its_icon(tmp_path, monkeypatch):
    """The label read happens after the snapshot, so a container can be gone by then. It is
    missing from `labels` for the same reason an unlabelled one is -- and resolving it would
    publish an image-derived icon over an override, or over an explicit `=none`, for a
    container the dashboard is still showing."""
    import casa_farnsworth
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    cache = IconCache(tmp_path / "icons", connection_factory=FakeConnection([]))
    cache.warm(["plex"])
    cache.write_index({"CASA_X": "plex"})          # what its label chose, while it was visible

    real = iconcache.IconCache
    monkeypatch.setattr(iconcache, "IconCache",
                        lambda d, **k: real(d, connection_factory=FakeConnection([])))
    # the inspect succeeded, but spoke only for some other container
    monkeypatch.setattr(casa_farnsworth.actions, "list_containers",
                        lambda **k: {"ok": True, "containers": [["b" * 64, "CASA_OTHER", "running"]]})
    monkeypatch.setattr(casa_farnsworth.actions, "read_router_owners",
                        lambda ids, **k: {"ok": True, "icons": {},
                                          "containers": [["b" * 64, "CASA_OTHER", "running"]]})

    casa_farnsworth._warm_container_icons(
        {"containers": [{"name": "CASA_X", "image": "linuxserver/sonarr"}]})
    assert cache.read_index() == {"CASA_X": "plex"}, "an unseen container lost its override"


def test_a_hanging_exchange_is_cut_by_the_watchdog(tmp_path):
    """The one thing that actually bounds a download.

    A socket timeout is an inactivity timeout, so a peer trickling a byte inside it holds the
    connection forever, and getresponse() parses headers before any deadline check of ours
    can run. Everything this feature used to carry -- an expiring marker, generation tokens --
    existed to survive that not being true.

    shutdown(), not close(): http.client hands the socket to the response the moment it reads
    one that will close the connection and drops its own reference, so close() on the
    connection can leave the read running. This fake unblocks only on shutdown, which is the
    behaviour that matters.
    """
    shut = threading.Event()

    class Sock:
        def shutdown(self, how):
            shut.set()

        def settimeout(self, value):
            pass

    class Hangs:
        sock = None

        def __call__(self, host, timeout=None):
            return self

        def connect(self):
            self.sock = Sock()

        def request(self, *a, **k):
            pass

        def getresponse(self):
            if not shut.wait(5):
                raise AssertionError("the watchdog never ended the blocked read")
            raise OSError("connection shut down by the watchdog")

        def close(self):
            pass                       # deliberately does NOT unblock the read

    c = IconCache(tmp_path / "i", connection_factory=Hangs())
    began = time.monotonic()
    body, out_of_time = c._download("sonarr", deadline=time.monotonic() + 0.3)
    assert body is None and out_of_time
    assert shut.is_set(), "the watchdog closed the connection instead of shutting the socket"
    assert time.monotonic() - began < 3, "the download outlasted its deadline"


def test_a_deadline_that_expires_during_connect_stops_the_exchange(tmp_path):
    """The watchdog can fire while connect() is still running -- a TLS peer trickling its
    handshake -- when there is no socket yet to shut down. It is not rearmed, so the request
    and response after it would be unbounded unless the expiry is noticed on the way past."""
    reached = []

    class SlowConnect:
        sock = None

        def __call__(self, host, timeout=None):
            return self

        def connect(self):
            time.sleep(0.4)            # outlasts the deadline below
            self.sock = type("S", (), {"shutdown": lambda self, how: None,
                                       "settimeout": lambda self, v: None})()

        def request(self, *a, **k):
            reached.append("request")

        def getresponse(self):
            reached.append("getresponse")
            raise AssertionError("the exchange continued past its deadline")

        def close(self):
            pass

    c = IconCache(tmp_path / "i", connection_factory=SlowConnect())
    body, out_of_time = c._download("sonarr", deadline=time.monotonic() + 0.2)
    assert body is None and out_of_time
    assert reached == [], f"kept going after the deadline: {reached}"


def test_a_handshake_that_never_finishes_is_cut(tmp_path):
    """connect() is where a TLS peer can trickle forever. The connection already holds the
    raw socket while the handshake negotiates on it, so the watchdog has something to shut
    down -- but only if it looks there rather than at a copy taken after connect() returns.

    Without this the warm thread never ends, and it holds the lock that every later scan's
    warm needs.
    """
    shut = threading.Event()

    class Handshaking:
        def __init__(self):
            self.sock = None

        def __call__(self, host, timeout=None):
            return self

        def connect(self):
            # what HTTPSConnection does: raw socket first, then negotiate on it
            self.sock = type("S", (), {
                "shutdown": lambda _self, how: shut.set(),
                "settimeout": lambda _self, v: None})()
            if not shut.wait(5):
                raise AssertionError("the watchdog never reached the handshaking socket")
            raise OSError("handshake socket shut down")

        def request(self, *a, **k):
            raise AssertionError("the exchange continued past a dead handshake")

        def getresponse(self):
            raise AssertionError("the exchange continued past a dead handshake")

        def close(self):
            pass

    c = IconCache(tmp_path / "i", connection_factory=Handshaking())
    began = time.monotonic()
    body, out_of_time = c._download("sonarr", deadline=time.monotonic() + 0.3)
    assert body is None and out_of_time
    assert shut.is_set()
    assert time.monotonic() - began < 3


def test_a_body_that_trickles_after_the_connection_let_go_is_cut(tmp_path):
    """The other half of the watchdog, and the reason a copy is kept at all.

    http.client drops conn.sock the moment it reads a response that will close the connection,
    while the response goes on reading the same descriptor. At that point the live attribute
    is None and only the socket captured at connect time can end the read.
    """
    shut = threading.Event()

    class Sock:
        def shutdown(self, how):
            shut.set()

        def settimeout(self, value):
            pass

    class Trickling:
        status = 200

        def read(self, amount=None):
            if not shut.wait(5):
                raise AssertionError("the watchdog never reached the response's socket")
            raise OSError("socket shut down under the response")

    class LetsGo:
        def __init__(self):
            self.sock = None

        def __call__(self, host, timeout=None):
            return self

        def connect(self):
            self.sock = Sock()

        def request(self, *a, **k):
            pass

        def getresponse(self):
            response = Trickling()
            self.sock = None           # http.client hands the descriptor to the response
            return response

        def close(self):
            pass

    c = IconCache(tmp_path / "i", connection_factory=LetsGo())
    began = time.monotonic()
    body, out_of_time = c._download("sonarr", deadline=time.monotonic() + 0.3)
    assert body is None and out_of_time
    assert shut.is_set(), "only the captured socket could have ended this read"
    assert time.monotonic() - began < 3
