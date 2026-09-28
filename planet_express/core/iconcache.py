"""Fetching app icons once, keeping them on disk, and serving them from our own origin.

This is the only outbound internet call the dashboard makes. Everything else it fetches is a
container on a docker bridge address. That makes this a new trust boundary, so it is narrow
on purpose:

  * one pinned host over HTTPS, and a path this module builds -- never a string from a label;
  * PNG only. selfh.st publishes SVG too and the spec preferred it, but an SVG served from our
    own origin can carry <script>, and the PNGs are 512px against a largest render of 40px. A
    scripting surface in exchange for nothing visible is a bad trade;
  * the magic bytes decide what a file is, not the Content-Type header;
  * a page request never causes a fetch. The dashboard warms the cache during a scan and
    serves only what is already on disk, so nothing a viewer does reaches the CDN, and a slow
    CDN cannot make the page slow.

A miss is remembered too. Without that, 26 of this host's 85 containers have no icon and
every scan would re-ask for all of them.
"""

import http.client
import logging
import os
import time
from pathlib import Path

from planet_express.core.icons import valid_slug

log = logging.getLogger("planetexpress.icons")

CDN_HOST = "cdn.jsdelivr.net"
CDN_PATH = "/gh/selfhst/icons/png/{slug}.png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MAX_BYTES = 512 * 1024        # the largest icon in the repo is ~40 KB
MAX_CACHED = 500              # bounded disk: this host has 85 containers
MISS_TTL = 24 * 60 * 60
TIMEOUT = 8


class IconCache:
    def __init__(self, directory, *, connection_factory=http.client.HTTPSConnection):
        self.dir = Path(directory)
        self._connect = connection_factory

    # ── reading ────────────────────────────────────────────────────────────────

    def path_for(self, slug: str):
        """The cached file for a slug, or None. The only lookup a request may reach.

        valid_slug() before touching the filesystem, so `..` or a separator never becomes a
        path at all; the resolve() check below is the second lock on the same door.
        """
        if not valid_slug(slug):
            return None
        path = self.dir / f"{slug}.png"
        try:
            if path.resolve().parent != self.dir.resolve():
                return None
        except OSError:
            return None
        return path if path.is_file() else None

    def has(self, slug: str) -> bool:
        return self.path_for(slug) is not None

    # ── filling ────────────────────────────────────────────────────────────────

    def warm(self, slugs) -> dict:
        """Fetch the icons we do not have. Called by the scan, never by a request."""
        wanted = [s for s in dict.fromkeys(slugs) if valid_slug(s)]
        result = {"fetched": 0, "cached": 0, "missing": 0, "skipped": 0}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("icon cache directory unusable: %s", e)
            return result
        existing = len(list(self.dir.glob("*.png")))
        for slug in wanted:
            if self.has(slug):
                result["cached"] += 1
                continue
            if self._miss_is_fresh(slug):
                result["missing"] += 1
                continue
            if existing >= MAX_CACHED:
                result["skipped"] += 1
                continue
            if self._fetch(slug):
                result["fetched"] += 1
                existing += 1
            else:
                result["missing"] += 1
        return result

    def _miss_path(self, slug: str) -> Path:
        return self.dir / f"{slug}.miss"

    def _miss_is_fresh(self, slug: str) -> bool:
        try:
            return (time.time() - self._miss_path(slug).stat().st_mtime) < MISS_TTL
        except OSError:
            return False

    def _record_miss(self, slug: str) -> None:
        try:
            self._miss_path(slug).write_bytes(b"")
        except OSError:
            pass

    def _fetch(self, slug: str) -> bool:
        body = self._download(slug)
        if body is None:
            self._record_miss(slug)
            return False
        target = self.dir / f"{slug}.png"
        temporary = self.dir / f".{slug}.{os.getpid()}.part"
        try:
            temporary.write_bytes(body)
            os.replace(temporary, target)          # atomic: a reader sees whole file or none
        except OSError as e:
            log.warning("could not cache icon %s: %s", slug, e)
            temporary.unlink(missing_ok=True)
            return False
        self._miss_path(slug).unlink(missing_ok=True)
        return True

    def _download(self, slug: str):
        """The bytes of an icon, or None. The one place a URL is built."""
        if not valid_slug(slug):                   # unreachable via warm(); cheap to keep
            return None
        conn = None
        try:
            conn = self._connect(CDN_HOST, timeout=TIMEOUT)
            conn.request("GET", CDN_PATH.format(slug=slug),
                         headers={"Host": CDN_HOST, "Accept": "image/png",
                                  "User-Agent": "planet-express"})
            response = conn.getresponse()
            if response.status != 200:
                return None
            # One byte past the cap tells us it was too big without reading the whole thing.
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES or not body.startswith(PNG_MAGIC):
                log.warning("icon %s refused: %d bytes, png=%s",
                            slug, len(body), body.startswith(PNG_MAGIC))
                return None
            return body
        except (OSError, http.client.HTTPException) as e:
            log.info("icon %s unavailable: %s", slug, e)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    log.debug("icon connection for %s would not close cleanly", slug)
