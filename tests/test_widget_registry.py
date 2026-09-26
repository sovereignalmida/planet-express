"""The widget matcher: which widget a container gets, and which it must not.

Every image string below was taken from `docker ps -a` on the live host. The routing-matrix
work shipped with a suite that passed against fixtures tidier than the host, and a reviewer
found three bugs the tests could not see; these are the real ones.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.widgets import registry

FAKE = {
    "sonarr": {"name": "sonarr", "match": ["linuxserver/sonarr", "hotio/sonarr"]},
    "immich": {"name": "immich", "match": ["immich-app/immich-server"]},
    "jellyfin": {"name": "jellyfin", "match": ["jellyfin/jellyfin"]},
}


@pytest.mark.parametrize(("image", "expected"), [
    # The same application, three registries, all live on this host right now.
    ("lscr.io/linuxserver/sonarr:latest", "linuxserver/sonarr"),
    ("linuxserver/radarr:latest", "linuxserver/radarr"),
    ("ghcr.io/linuxserver/qbittorrent", "linuxserver/qbittorrent"),
    # Tags, digests and both together.
    ("ghcr.io/immich-app/immich-server:release", "immich-app/immich-server"),
    ("jellyfin/jellyfin", "jellyfin/jellyfin"),
    ("postgres", "postgres"),
    ("adguard/adguardhome:latest", "adguard/adguardhome"),
    ("nginx@sha256:abc123", "nginx"),
    ("lscr.io/linuxserver/sonarr:4.0@sha256:def", "linuxserver/sonarr"),
    # Locally built images have no registry and no slash.
    ("vikunja-mcp-vikunja-mcp", "vikunja-mcp-vikunja-mcp"),
    ("services-porta", "services-porta"),
    # A registry with a port must not be mistaken for a tag.
    ("registry.local:5000/team/app:v2", "team/app"),
    ("localhost:5000/app", "app"),
    ("", ""),
    (None, ""),
])
def test_normalise_image(image, expected):
    assert registry.normalise_image(image) == expected


def test_one_match_entry_covers_every_registry_that_serves_it():
    """The match list says linuxserver/sonarr; the host runs lscr.io/linuxserver/sonarr."""
    assert registry.match_widget("lscr.io/linuxserver/sonarr:latest", widgets=FAKE)["name"] == "sonarr"
    assert registry.match_widget("ghcr.io/linuxserver/sonarr", widgets=FAKE)["name"] == "sonarr"
    assert registry.match_widget("linuxserver/sonarr", widgets=FAKE)["name"] == "sonarr"


def test_a_near_miss_image_gets_no_widget():
    """This host runs immich-app/immich-server AND varun-raj/immich-power-tools. A substring
    match on "immich" would hang the immich widget on a tool with no such API."""
    assert registry.match_widget("ghcr.io/immich-app/immich-server:release", widgets=FAKE)["name"] == "immich"
    assert registry.match_widget("ghcr.io/varun-raj/immich-power-tools:latest", widgets=FAKE) is None


def test_an_unmatched_image_gets_no_widget():
    for image in ("postgres:16", "redis", "traefik", "qmcgaw/gluetun", "willfarrell/autoheal"):
        assert registry.match_widget(image, widgets=FAKE) is None, image


def test_a_label_names_a_widget_the_image_would_not_have_matched():
    assert registry.match_widget("some/private-build", label="sonarr", widgets=FAKE)["name"] == "sonarr"


def test_the_label_none_refuses_a_widget_the_image_would_have_matched():
    assert registry.match_widget("lscr.io/linuxserver/sonarr", label="none", widgets=FAKE) is None


@pytest.mark.parametrize("label", ["NONE", " none ", "None"])
def test_the_disable_label_is_not_case_or_space_sensitive(label):
    assert registry.match_widget("lscr.io/linuxserver/sonarr", label=label, widgets=FAKE) is None


def test_a_label_naming_a_widget_that_does_not_exist_yields_none():
    """Not a fallback to the image match: the operator said which widget they wanted, and
    quietly substituting a different one is worse than showing none."""
    assert registry.match_widget("lscr.io/linuxserver/sonarr", label="nosuchwidget", widgets=FAKE) is None


def test_an_empty_label_falls_through_to_the_image():
    assert registry.match_widget("lscr.io/linuxserver/sonarr", label="", widgets=FAKE)["name"] == "sonarr"


# ── the shipped widget files ────────────────────────────────────────────────────

def test_every_shipped_widget_declares_the_whole_contract():
    widgets = registry.load_widgets()
    assert widgets, "no widgets loaded"
    for name, widget in widgets.items():
        assert widget["name"] == name
        assert widget["match"], f"{name} matches nothing"
        assert isinstance(widget["port"], int), f"{name} has no port"
        assert widget["get"], f"{name} fetches nothing"
        assert callable(widget["summarise"]), f"{name} cannot summarise"


def test_every_declared_path_is_a_get_path_under_root():
    """The fetcher refuses anything that is not a GET, and these are the only paths it will
    ever be handed. A path that escapes the host is not something to discover at runtime."""
    for name, widget in registry.load_widgets().items():
        for path in widget["get"]:
            assert path.startswith("/"), f"{name}: {path}"
            assert "://" not in path and ".." not in path, f"{name}: {path}"


def test_no_two_widgets_claim_the_same_image():
    seen = {}
    for name, widget in registry.load_widgets().items():
        for candidate in widget["match"]:
            repo = registry.normalise_image(candidate)
            assert repo not in seen, f"{name} and {seen.get(repo)} both claim {repo}"
            seen[repo] = name


def test_the_live_hosts_sonarr_and_adguard_images_match_their_widgets():
    """Straight from `docker ps -a` on the live host."""
    assert registry.match_widget("lscr.io/linuxserver/sonarr:latest")["name"] == "sonarr"
    assert registry.match_widget("adguard/adguardhome:latest")["name"] == "adguard"


def test_sonarr_summarise_shapes_the_contract():
    widget = registry.load_widgets()["sonarr"]
    out = widget["summarise"]([
        {"totalRecords": 3, "records": [{"title": "Severance · S02E08", "size": 100, "sizeleft": 16}]},
        {"totalRecords": 12},
        [{"id": 1}, {"id": 2}],
        [{"type": "warning", "message": "x"}],
    ])
    assert [stat["k"] for stat in out["stats"]] == ["QUEUE", "WANTED", "SERIES", "HEALTH"]
    assert out["rows"] == [{"title": "Severance · S02E08", "pct": 84}]
    assert out["rows_label"] == "DOWNLOADING"


def test_sonarr_summarise_survives_an_api_that_answers_with_nothing():
    """A reachable API returning empty or null bodies must not take the panel down with it."""
    widget = registry.load_widgets()["sonarr"]
    out = widget["summarise"]([None, None, None, None])
    assert [stat["v"] for stat in out["stats"]] == [0, 0, 0, "ok"]
    assert out["rows"] == []


def test_adguard_percentage_is_guarded_against_a_fresh_install():
    """A freshly started AdGuard reports zero queries. The dashboard has already been bitten
    once by a percentage computed without that guard."""
    widget = registry.load_widgets()["adguard"]
    out = widget["summarise"]([{"num_dns_queries": 0, "num_blocked_filtering": 0}])
    assert dict(zip([s["k"] for s in out["stats"]], [s["v"] for s in out["stats"]]))["BLOCKED"] == "—"


def test_adguard_reports_real_numbers_when_it_has_them():
    widget = registry.load_widgets()["adguard"]
    out = widget["summarise"]([{
        "num_dns_queries": 35591, "num_blocked_filtering": 7982,
        "avg_processing_time": 0.041, "top_blocked_domains": [{"ads.example.com": 42}],
    }])
    values = dict(zip([s["k"] for s in out["stats"]], [s["v"] for s in out["stats"]]))
    assert values == {"QUERIES": "35,591", "BLOCKED": "22.4%", "AVG": "41ms"}
    assert out["line"] == "most blocked: ads.example.com"


def test_a_broken_widget_file_costs_its_own_panel_and_nothing_else(tmp_path, monkeypatch):
    """A widget file is a plugin, not a dependency. load_widgets() is called from the
    dashboard's request path, where an import error would be a 500 on a page whose whole
    contract is that it always renders."""
    from planet_express import widgets as package

    broken = Path(package.__path__[0]) / "zz_broken_fixture.py"
    broken.write_text("raise RuntimeError('this widget is broken')\n")
    try:
        loaded = registry.load_widgets()
        assert "sonarr" in loaded          # the healthy ones still load
        assert "zz_broken_fixture" not in loaded
    finally:
        broken.unlink()


def test_a_widget_file_without_the_contract_is_skipped_quietly(tmp_path):
    from planet_express import widgets as package

    partial = Path(package.__path__[0]) / "zz_partial_fixture.py"
    partial.write_text("WIDGET = {'match': ['x/y']}\n")   # no name
    try:
        assert "zz_partial_fixture" not in registry.load_widgets()
    finally:
        partial.unlink()
