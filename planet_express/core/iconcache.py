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
import json
import logging
import os
import threading
import time
from pathlib import Path

from planet_express.core.icons import valid_slug
from planet_express.core.sockets import shutdown_sock

log = logging.getLogger("planetexpress.icons")

CDN_HOST = "cdn.jsdelivr.net"
CDN_PATH = "/gh/selfhst/icons/png/{slug}.png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
INDEX_NAME = "resolved.json"

MAX_BYTES = 512 * 1024        # the largest icon in the repo is ~40 KB
# One budget for a whole warm, because warming happens inside the monitoring scan. Serially,
# a cold cache against an unreachable CDN is TIMEOUT x slugs -- minutes of held scan slot on
# this host's 59 icons, delaying every finding behind it. Whatever is left over is simply
# fetched by the next scan.
WARM_BUDGET = 20
MAX_CACHED = 500              # bounded disk: this host has 85 containers
MISS_TTL = 24 * 60 * 60
TIMEOUT = 8
_CHUNK = 16 * 1024


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

    # ── the resolved map ───────────────────────────────────────────────────────

    def write_index(self, mapping: dict) -> None:
        """Record which slug each container resolved to, for the dashboard to read.

        Core resolves and writes; the dashboard only reads. That keeps the label override
        working on every surface without the dashboard asking core anything -- including the
        container detail page, which renders without a single RPC, and during a Traefik
        outage, which has nothing to do with what an app's icon is.
        """
        usable = {str(k): str(v) for k, v in (mapping or {}).items()
                  if isinstance(k, str) and valid_slug(v)}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            temporary = self.dir / f".index.{os.getpid()}.part"
            temporary.write_text(json.dumps(usable, indent=2, sort_keys=True))
            os.replace(temporary, self.dir / INDEX_NAME)
        except OSError as e:
            log.warning("could not record resolved icons: %s", e)

    def read_index(self) -> dict:
        """{container: slug} from the last scan, for slugs actually on disk."""
        try:
            raw = json.loads((self.dir / INDEX_NAME).read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, str) and self.has(v)}

    # ── filling ────────────────────────────────────────────────────────────────

    def warm(self, slugs, *, budget=WARM_BUDGET) -> dict:
        """Fetch the icons we do not have. Called by the scan, never by a request.

        Returns when `budget` seconds are gone, however many are left: the scan it runs inside
        matters more than any icon, and the next scan picks up where this one stopped.
        """
        wanted = [s for s in dict.fromkeys(slugs) if valid_slug(s)]
        result = {"fetched": 0, "cached": 0, "missing": 0, "skipped": 0}
        deadline = time.monotonic() + budget
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
            if existing >= MAX_CACHED or time.monotonic() >= deadline:
                result["skipped"] += 1
                continue
            outcome = self._fetch(slug, deadline)
            result[outcome] += 1
            if outcome == "fetched":
                existing += 1
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

    def _fetch(self, slug: str, deadline=None) -> str:
        """"fetched", "missing" (remembered) or "skipped" (ran out of budget, not remembered)."""
        body, out_of_time = self._download(slug, deadline)
        if body is None:
            # A miss is remembered for MISS_TTL. Running out of budget is not a miss: this
            # icon may well exist, and recording one would suppress it for 24 hours because
            # an earlier docker read happened to be slow.
            if out_of_time:
                return "skipped"
            self._record_miss(slug)
            return "missing"
        target = self.dir / f"{slug}.png"
        temporary = self.dir / f".{slug}.{os.getpid()}.part"
        try:
            temporary.write_bytes(body)
            os.replace(temporary, target)          # atomic: a reader sees whole file or none
        except OSError as e:
            log.warning("could not cache icon %s: %s", slug, e)
            temporary.unlink(missing_ok=True)
            return "missing"
        self._miss_path(slug).unlink(missing_ok=True)
        return "fetched"

    def _download(self, slug: str, deadline=None):
        """(bytes, out_of_time). The one place a URL is built.

        `deadline` is wall-clock and covers the whole exchange. A socket timeout alone does
        not: it is an INACTIVITY timeout, restarted by every byte that arrives, so a server
        trickling one byte just inside it holds the connection -- and the scan this runs
        inside -- open forever. So the remaining budget is pushed onto the socket before every
        receive, and it only shrinks.
        """
        if not valid_slug(slug):                   # unreachable via warm(); cheap to keep
            return None, False

        def left():
            return TIMEOUT if deadline is None else deadline - time.monotonic()

        if left() <= 0:
            return None, True
        conn = None
        watchdog = None
        held = {}
        fired = threading.Event()
        try:
            conn = self._connect(CDN_HOST, timeout=min(TIMEOUT, left()))
            # The only thing that actually bounds this exchange, and the same shape the widget
            # fetcher uses. shutdown(), not close(): http.client hands the socket to the
            # response the moment it reads one that will close the connection and drops its
            # own reference, so a close() here can leave the read running. Started before
            # connect() so the connect is inside the deadline too.
            def expire():
                fired.set()
                # Both, because neither alone covers the whole exchange. During connect() the
                # connection already holds the raw socket while TLS negotiates on it, and the
                # copy below is not set yet. After a response that closes the connection,
                # http.client drops conn.sock and only the copy is left. Whichever exists.
                shutdown_sock(getattr(conn, "sock", None))
                shutdown_sock(held.get("sock"))

            watchdog = threading.Timer(max(0.0, left()), expire)
            watchdog.daemon = True
            watchdog.start()
            if hasattr(conn, "connect"):
                conn.connect()
                held["sock"] = getattr(conn, "sock", None)
                if fired.is_set():
                    shutdown_sock(getattr(conn, "sock", None))
                    return None, True
                # The timer can have fired while connect() was still running -- a TLS peer
                # trickling its handshake -- when there was no socket yet to shut down. It is
                # not rearmed, so without this the request and the response that follow would
                # be unbounded. Same check the widget fetcher makes for the same reason.
            conn.request("GET", CDN_PATH.format(slug=slug),
                         headers={"Host": CDN_HOST, "Accept": "image/png",
                                  "User-Agent": "planet-express"})
            response = conn.getresponse()
            if response.status != 200:
                return None, False
            # Chunked, so the size cap is applied as the bytes arrive rather than after.
            body = bytearray()
            while len(body) <= MAX_BYTES:
                if left() <= 0:
                    log.info("icon %s abandoned: out of time mid-download", slug)
                    return None, True
                chunk = response.read(min(_CHUNK, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            body = bytes(body)
            if len(body) > MAX_BYTES or not body.startswith(PNG_MAGIC):
                log.warning("icon %s refused: %d bytes, png=%s",
                            slug, len(body), body.startswith(PNG_MAGIC))
                return None, False
            return body, False
        except (OSError, http.client.HTTPException) as e:
            log.info("icon %s unavailable: %s", slug, e)
            return None, left() <= 0
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    log.debug("icon connection for %s would not close cleanly", slug)
