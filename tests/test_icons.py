"""Which icon a container gets.

The image strings below were taken from `docker ps -a` on the live host, the same way the
widget matcher's were. A fixture tidier than the host is how the routing matrix shipped three
bugs its tests could not see.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.icons import (
    ALIASES,
    LABEL,
    NO_ICON,
    monogram,
    slug_for,
    valid_slug,
)


@pytest.mark.parametrize("image,expected", [
    ("lscr.io/linuxserver/sonarr:4.0.9", "sonarr"),
    ("linuxserver/radarr:latest", "radarr"),
    ("ghcr.io/linuxserver/prowlarr", "prowlarr"),
    ("adguard/adguardhome:latest", "adguard-home"),
    ("ghcr.io/hotio/qbittorrent-nox", "qbittorrent"),
    ("ghcr.io/immich-app/immich-server:release", "immich"),
    ("ghcr.io/mealie-recipes/mealie:latest", "mealie"),
    ("miniflux/miniflux:latest", "miniflux"),
    ("ghcr.io/karakeep-app/karakeep:release", "karakeep"),
    # a digest, no tag
    ("linuxserver/sonarr@sha256:" + "a" * 64, "sonarr"),
])
def test_the_slug_is_the_application_not_the_registry(image, expected):
    assert slug_for(image) == expected


@pytest.mark.parametrize("image", [
    "691ae3ba95dd", "c99d953bce6f", "0123456789ab", "f" * 64,
])
def test_an_untagged_image_shows_its_id_and_must_not_become_a_slug(image):
    """`docker ps` prints an image ID where a name would go for an untagged image; two
    containers on this host do. A hex ID is not an app: fetching it 404s once per container
    and parks a meaningless entry in the negative cache."""
    assert slug_for(image) is None


@pytest.mark.parametrize("declared,expected", [
    ("plex", "plex"),                    # a plain slug wins
    ("PLEX", "plex"),                    # case folded
    ("  plex  ", "plex"),
    ("none", None),                      # explicit "this app has no icon"
    ("NONE", None),
])
def test_the_label_chooses_the_icon(declared, expected):
    assert slug_for("linuxserver/sonarr", {LABEL: declared}) == expected


@pytest.mark.parametrize("declared", [
    "https://evil.example/x.png",        # the spec allowed a URL; we do not
    "http://169.254.169.254/latest/meta-data/",
    "../../../etc/passwd",
    "..",
    "a/b",
    "sonarr.png",                        # a dot is an extension or a traversal, never a slug
    "-leading-dash",
    "x" * 65,
    "",
    "   ",
    7,
    None,
    ["sonarr"],
])
def test_a_label_that_is_not_a_slug_cannot_steer_the_fetch(declared):
    """A label may choose among things the code sanctions, never point at something new --
    the same rule the widget label settled on (option A, 2026-09-27). A URL in a label is the
    dashboard fetching whatever a compose file says.

    It falls back to the image rather than blanking: the label is an override, not a claim
    that no icon exists.
    """
    assert slug_for("linuxserver/sonarr", {LABEL: declared}) == "sonarr"


def test_infrastructure_containers_get_a_monogram():
    for name in NO_ICON:
        assert slug_for(f"linuxserver/{name}") is None


@pytest.mark.parametrize("value", ["sonarr", "adguard-home", "a", "0", "a" * 64])
def test_valid_slugs(value):
    assert valid_slug(value)


@pytest.mark.parametrize("value", [
    "Sonarr", "son arr", "sonarr/", "son.arr", "-x", "", "a" * 65, None, 7, "a\nb", "a\x00b",
])
def test_invalid_slugs(value):
    assert not valid_slug(value)


def test_every_alias_target_is_a_usable_slug():
    """An alias is a string this code puts into a URL path and a filename. A typo here is the
    one way a bad path reaches either without passing through slug_for's checks."""
    for source, target in ALIASES.items():
        assert valid_slug(target), f"{source} -> {target!r} is not a usable slug"
        assert source == source.lower().strip(), f"{source!r} would never be looked up"
        assert target not in NO_ICON, f"{source} aliases to a slug we then refuse"


def test_an_alias_is_only_worth_having_if_it_changes_something():
    assert not [s for s, t in ALIASES.items() if s == t]


@pytest.mark.parametrize("name,expected", [
    ("CASA_TRAEFIKMAN", "CA"), ("sonarr", "SO"), ("9lives", "9L"), ("", "??"), ("_-_", "??"),
])
def test_monogram(name, expected):
    assert monogram(name) == expected


def test_slug_never_produces_a_path_or_a_scheme():
    """Belt and braces over the whole surface: whatever comes out of slug_for is safe to put
    in a filename and a URL path, no matter what went in."""
    hostile = ["../x", "a/b", "http://x", "x?y", "x#y", "x%2e%2e", "x\\y", ".", "..",
               "CON", "x:y", "x;y", "x y", "\x00"]
    for value in hostile:
        for image in (value, f"linuxserver/{value}"):
            out = slug_for(image, {LABEL: value})
            assert out is None or valid_slug(out), f"{value!r} -> {out!r}"
