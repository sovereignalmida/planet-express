"""The dashboard's only outbound internet call.

Everything else it fetches is a container on a bridge address, so these tests are mostly about
what this refuses: the wrong host, the wrong bytes, too many bytes, and a path that is not a
path. The happy case is one test; the rest is the boundary.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core import iconcache
from planet_express.core.iconcache import PNG_MAGIC, IconCache

PNG = PNG_MAGIC + b"rest of a perfectly good png"


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self, amount=None):
        return self._body[:amount] if amount is not None else self._body


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
