"""Which app icon a container gets, and what to show when none fits.

Pure: no network, no filesystem. Deciding *which* icon is separate from fetching it, so the
decision is testable against the 85 images on this host without touching a CDN, and so a
fetch failure can never change which icon a container is supposed to have.

Icons come from selfh.st (repo `selfhst/icons`). A slug here is a filename in that repo, not
a URL: see `planet_express/core/iconcache.py` for the one place a URL is built.
"""

import re

from planet_express.core.images import normalise_image

# A selfh.st filename. Deliberately narrow: this string ends up in a URL path and in a
# filesystem path, and the only thing standing between a container label and both of those is
# this pattern. No dots (no traversal, no extension smuggling), no slashes, no uppercase.
_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_MAX_SLUG = 64

# Image name -> selfh.st slug, only where the two genuinely differ. Every entry below was
# checked against both this host's `docker ps` and the icon repo; guessing an alias produces a
# 404 and a monogram, which looks identical to having no entry at all and is harder to notice.
ALIASES = {
    "adguardhome": "adguard-home",
    "qbittorrent-nox": "qbittorrent",
    # Measured against this host's 85 containers and the icon repo, 2026-09-28. Each entry is
    # an image whose own name 404s and whose app has an icon under a different spelling.
    "immich-server": "immich",
    "immich-machine-learning": "immich",
    "actual-server": "actual-budget",
    "adventurelog-backend": "adventurelog",
    "adventurelog-frontend": "adventurelog",
    "beszel-agent": "beszel",
    "tubearchivist": "tube-archivist",
    "trilium": "trilium-notes",
    "bentopdf-simple": "bentopdf",
    "vikunja-mcp-vikunja-mcp": "vikunja",
    "postgres": "postgresql",
    "postgis": "postgresql",
}

# Containers that are infrastructure rather than an app someone opens. A monogram reads better
# than a wrong logo, and selfh.st has no icon for most of them anyway.
NO_ICON = frozenset({"recyclarr", "unpackerr"})

# A container can run an untagged image, and `docker ps` then prints an image ID where a name
# would go. Two on this host do. A hex ID is not an application name: fetching it would 404
# once per container, and the negative cache would hold a meaningless entry.
_IMAGE_ID = re.compile(r"[0-9a-f]{12}|[0-9a-f]{64}")

LABEL = "planetexpress.icon"


def valid_slug(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= _MAX_SLUG
            and set(value) <= _SLUG_CHARS and not value.startswith("-"))


def slug_for(image: str, labels=None) -> str | None:
    """The icon slug for a container, or None when it should show a monogram.

    A `planetexpress.icon` label chooses the slug; `=none` says there is no icon. The label is
    a slug and never a URL: the spec allowed either, but a URL in a label is the dashboard
    fetching whatever a compose file tells it to, which is the thing the widget-label decision
    already settled against (option A, 2026-09-27). A label that is not a usable slug falls
    back to the image rather than blanking the icon -- it is an override, not an assertion
    that no icon exists.
    """
    declared = (labels or {}).get(LABEL)
    if isinstance(declared, str):
        declared = declared.strip().lower()
        if declared == "none":
            return None
        if valid_slug(declared):
            return declared

    name = (normalise_image(image) or "").rsplit("/", 1)[-1].lower()
    if _IMAGE_ID.fullmatch(name):
        return None
    name = ALIASES.get(name, name)
    if not name or name in NO_ICON or not valid_slug(name):
        return None
    return name


def monogram(name: str) -> str:
    """Two letters for a container with no icon, so rows stay aligned."""
    letters = [c for c in (name or "") if c.isalnum()]
    return "".join(letters[:2]).upper() or "??"
