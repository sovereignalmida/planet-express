"""The widget matcher: which widget a container gets, and which it must not.

Every image string below was taken from `docker ps -a` on the live host. The routing-matrix
work shipped with a suite that passed against fixtures tidier than the host, and a reviewer
found three bugs the tests could not see; these are the real ones.
"""
import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.widgets import fetcher, registry

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
        # One widget may list a repo under several registries (lscr.io, ghcr.io, Docker Hub).
        for repo in {registry.normalise_image(candidate) for candidate in widget["match"]}:
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
        [{"type": "warning", "message": "x"}],
    ])
    assert [stat["k"] for stat in out["stats"]] == ["QUEUE", "WANTED", "HEALTH"]
    assert out["rows"] == [{"title": "Severance · S02E08", "pct": 84}]
    assert out["rows_label"] == "DOWNLOADING"


def test_sonarr_summarise_survives_an_api_that_answers_with_nothing():
    """A reachable API returning empty or null bodies must not take the panel down with it."""
    widget = registry.load_widgets()["sonarr"]
    out = widget["summarise"]([None, None, None])
    assert [stat["v"] for stat in out["stats"]] == [0, 0, "ok"]
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


# ── the contract is checked at load, not discovered at request time ─────────────

SUMMARISE = "\ndef summarise(r): return {}\n"


@pytest.mark.parametrize(("name", "body"), [
    ("zz_match_none", "WIDGET = {'name': 'x', 'match': None, 'port': 1, 'get': ['/a']}" + SUMMARISE),
    ("zz_match_empty", "WIDGET = {'name': 'x', 'match': [], 'port': 1, 'get': ['/a']}" + SUMMARISE),
    ("zz_no_port", "WIDGET = {'name': 'x', 'match': ['a/b'], 'get': ['/a']}" + SUMMARISE),
    ("zz_bool_port", "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': True, 'get': ['/a']}" + SUMMARISE),
    ("zz_escaping_path", "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': 1, 'get': ['/../etc']}" + SUMMARISE),
    ("zz_absolute_url", "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': 1, 'get': ['http://evil/x']}" + SUMMARISE),
    ("zz_no_summarise", "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': 1, 'get': ['/a']}\n"),
])
def test_a_malformed_contract_is_skipped_at_load(name, body):
    """Importing cleanly is not the same as being usable. match=None iterates fine at import
    and raises TypeError inside match_widget(), which runs once per container in the
    dashboard's request path — so one bad declaration would take the whole page down."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / f"{name}.py"
    path.write_text(body)
    try:
        loaded = registry.load_widgets()
        assert name not in loaded
        assert "x" not in loaded
        assert "sonarr" in loaded          # the healthy ones still load
        # And the matcher still answers for every container, which is the point.
        assert registry.match_widget("a/b", widgets=loaded) is None
        assert registry.match_widget("lscr.io/linuxserver/sonarr", widgets=loaded)["name"] == "sonarr"
    finally:
        path.unlink()


def test_every_shipped_widget_says_how_the_fetcher_authenticates():
    """`key: "SOME_VAR"` could not express the difference between a header and basic auth,
    so it said nothing useful to the fetcher that has to make the request.

    Absent auth is allowed and is not a gap: tracearr's /health takes no credential, and a
    widget that needs none can never leak one. What is checked is that a DECLARED auth is one
    the fetcher can act on."""
    keyless = []
    for name, widget in registry.load_widgets().items():
        auth = widget.get("auth")
        if auth is None:
            keyless.append(name)
            continue
        assert isinstance(auth, dict), f"{name}: auth is neither absent nor a dict"
        if auth["type"] == "header":
            assert auth["header"] and auth["env"], name
            # A prefix is a scheme word, never a template with the key spliced into it.
            assert "{" not in auth.get("prefix", ""), name
        elif auth["type"] == "basic":
            assert auth["username_env"] and auth["password_env"], name
        else:
            raise AssertionError(f"{name}: unknown auth type {auth['type']!r}")
    assert keyless == ["tracearr"], f"a widget stopped declaring auth: {keyless}"


def test_adguard_asks_for_the_credentials_this_host_actually_has():
    """config.adguard_credentials() reads ADGUARD_USERNAME and ADGUARD_PASSWORD from
    /etc/planetexpress-dashboard.env. A widget naming anything else reads as "not
    configured" on a host that is configured."""
    import config

    auth = registry.load_widgets()["adguard"]["auth"]
    assert (auth["username_env"], auth["password_env"]) == ("ADGUARD_USERNAME", "ADGUARD_PASSWORD")
    assert config.adguard_credentials() == (
        os.environ.get("ADGUARD_USERNAME", ""), os.environ.get("ADGUARD_PASSWORD", ""))


def test_adguard_is_declared_on_the_port_a_configured_install_answers_on():
    """80, not 3000: 3000 is AdGuard's first-run setup port. Traefik routes this host's
    AdGuard to 172.20.0.4:80."""
    assert registry.load_widgets()["adguard"]["port"] == 80


@pytest.mark.parametrize(("name", "auth"), [
    ("zz_auth_not_a_dict", "'auth': 'X-Api-Key'"),
    ("zz_auth_unknown_type", "'auth': {'type': 'oauth', 'env': 'X'}"),
    ("zz_auth_no_type", "'auth': {'env': 'X'}"),
    ("zz_auth_header_no_env", "'auth': {'type': 'header', 'header': 'X-Api-Key'}"),
    ("zz_auth_header_blank_env", "'auth': {'type': 'header', 'header': 'X-Api-Key', 'env': '  '}"),
    ("zz_auth_basic_half", "'auth': {'type': 'basic', 'username_env': 'U'}"),
    ("zz_auth_basic_misspelt", "'auth': {'type': 'basic', 'username_env': 'U', 'passwrd_env': 'P'}"),
])
def test_an_auth_block_the_fetcher_could_not_act_on_is_skipped(name, auth):
    """auth is part of the contract now, so it is validated with the rest of it. A widget
    whose credentials cannot be turned into a request is broken, not unauthenticated."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / f"{name}.py"
    path.write_text(
        "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': 1, 'get': ['/a'], " + auth + "}" + SUMMARISE)
    try:
        loaded = registry.load_widgets()
        assert "x" not in loaded and name not in loaded
        assert "sonarr" in loaded
    finally:
        path.unlink()


def test_a_widget_needing_no_credentials_is_allowed():
    """Plenty of APIs need no auth. Absent is fine; malformed is not."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / "zz_no_auth.py"
    path.write_text("WIDGET = {'name': 'zz_no_auth', 'match': ['a/b'], 'port': 1, 'get': ['/a']}"
                    + SUMMARISE)
    try:
        assert "zz_no_auth" in registry.load_widgets()
    finally:
        path.unlink()


@pytest.mark.parametrize("auth", [
    "'auth': {'type': []}",                      # unhashable: _AUTH_FIELDS.get() raises on it
    "'auth': {'type': {'a': 1}}",
    "'auth': {'type': 0}",
])
def test_an_auth_type_that_is_not_even_a_string_is_skipped(auth):
    """The validator runs in the dashboard's request path, so it must not be the thing that
    raises. A dict key lookup on an unhashable value is a TypeError, not a None."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / "zz_auth_unhashable.py"
    path.write_text(
        "WIDGET = {'name': 'x', 'match': ['a/b'], 'port': 1, 'get': ['/a'], " + auth + "}" + SUMMARISE)
    try:
        loaded = registry.load_widgets()
        assert "x" not in loaded
        assert "sonarr" in loaded
    finally:
        path.unlink()


def test_a_contract_broken_in_a_way_the_validator_never_anticipated_costs_one_panel():
    """Belt and braces: the isolation guarantee must not rest on the validator being
    exhaustive, so _validated() itself is wrapped the same way the import is."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / "zz_hostile.py"
    path.write_text(
        "class Hostile(dict):\n"
        "    def get(self, *a, **k): raise RuntimeError('boom')\n"
        "WIDGET = Hostile(name='x', match=['a/b'], port=1, get=['/a'])\n" + SUMMARISE)
    try:
        loaded = registry.load_widgets()
        assert "x" not in loaded
        assert "sonarr" in loaded and "adguard" in loaded
    finally:
        path.unlink()


# ── radarr, prowlarr, immich ────────────────────────────────────────────────────

def test_the_common_radarr_prowlarr_and_immich_images_match_their_widgets():
    assert registry.match_widget("lscr.io/linuxserver/radarr:latest")["name"] == "radarr"
    assert registry.match_widget("ghcr.io/hotio/prowlarr:release")["name"] == "prowlarr"
    assert registry.match_widget("ghcr.io/immich-app/immich-server:release")["name"] == "immich"
    # The machine-learning container has no API to read.
    assert registry.match_widget("ghcr.io/immich-app/immich-machine-learning:release") is None


def test_radarr_line_names_a_download_in_progress_not_a_stuck_import():
    widget = registry.load_widgets()["radarr"]
    out = widget["summarise"]([
        {"totalRecords": 2, "records": [
            {"title": "Dune.Part.Two.2024.2160p", "status": "completed", "size": 200, "sizeleft": 0},
            {"title": "Arrival.2016.1080p", "status": "downloading", "size": 200, "sizeleft": 50,
             "movie": {"title": "Arrival", "year": 2016}}]},
        {"totalRecords": 4},
        [],
    ])
    assert [(s["k"], s["v"]) for s in out["stats"]] == [("QUEUE", 2), ("WANTED", 4), ("HEALTH", "ok")]
    # Announced movies count as wanted: a number is not a warning by itself.
    assert "level" not in out["stats"][1]
    assert out["line"] == "downloading: Arrival (2016) · 75%"


def test_radarr_progress_survives_non_finite_sizes():
    """1e999 parses to inf; the widget must still answer, at 0%."""
    widget = registry.load_widgets()["radarr"]
    out = widget["summarise"]([
        {"records": [{"title": "x", "status": "downloading", "size": float("inf"), "sizeleft": 1}]},
        {}, []])
    assert out["line"] == "downloading: x · 0%"


def test_prowlarr_summarise_counts_blocked_indexers_and_lists_health():
    widget = registry.load_widgets()["prowlarr"]
    out = widget["summarise"]([
        [{"indexerId": 2, "disabledTill": "2026-09-27T13:00:00Z"}, {"indexerId": 9}],
        [{"type": "warning", "message": "Indexers unavailable due to failures: 1337x"},
         {"type": "notice", "message": "ignored"}],
    ])
    assert [(s["k"], s["v"]) for s in out["stats"]] == [("FAILING", 2), ("HEALTH", "1")]
    assert out["rows"] == [{"title": "Indexers unavailable due to failures: 1337x", "meta": "warning"}]
    assert out["rows_label"] == "HEALTH"


def test_prowlarr_never_asks_for_the_unbounded_indexer_list():
    """/api/v1/indexer carries every indexer's schema and can spend the whole budget."""
    widget = registry.load_widgets()["prowlarr"]
    assert "/api/v1/indexer" not in widget["get"]
    assert "max_bytes" not in widget


def test_immich_summarise_formats_counts_and_usage():
    widget = registry.load_widgets()["immich"]
    out = widget["summarise"]([{"photos": 48213, "videos": 1207, "usage": 612_400_000_000}])
    assert [(s["k"], s["v"]) for s in out["stats"]] == [
        ("PHOTOS", "48,213"), ("VIDEOS", "1,207"), ("USED", "570.3 GiB")]
    assert widget["summarise"]([{"usage": float("inf")}])["stats"][2]["v"] == "—"


@pytest.mark.parametrize("name", ["radarr", "prowlarr", "immich"])
@pytest.mark.parametrize("junk", [None, "text", 7, [], {}])
def test_new_widgets_survive_an_api_that_answers_with_junk(name, junk):
    """Every value is the app's own JSON: a wrong shape must still produce the contract."""
    widget = registry.load_widgets()[name]
    out = fetcher.normalise_summary(widget["summarise"]([junk] * len(widget["get"])))
    assert len(out["stats"]) == {"radarr": 3, "prowlarr": 2, "immich": 3}[name]



@pytest.mark.parametrize(("name", "accepted", "refused"), [
    ("radarr", ["lscr.io/linuxserver/radarr", "docker.io/linuxserver/radarr", "ghcr.io/hotio/radarr",
                "docker.io/hotio/radarr"],
     ["evil.example/linuxserver/radarr", "ghcr.io/linuxserver/radar", "docker.io/radarr/radarr"]),
    ("prowlarr", ["ghcr.io/linuxserver/prowlarr", "docker.io/linuxserver/prowlarr", "ghcr.io/hotio/prowlarr"],
     ["evil.example/hotio/prowlarr", "lscr.io/hotio/prowlarr"]),
    ("immich", ["ghcr.io/immich-app/immich-server"],
     ["docker.io/immich-app/immich-server", "ghcr.io/immich-app/immich-machine-learning",
      "evil.example/immich-app/immich-server"]),
])
def test_new_widgets_send_keys_only_to_their_publishers(name, accepted, refused):
    widget = registry.load_widgets()[name]
    for repo in accepted:
        assert registry.trusted_provenance(widget, [repo]), repo
    for repo in refused:
        assert not registry.trusted_provenance(widget, [repo]), repo


@pytest.mark.parametrize("name", ["sonarr", "radarr", "prowlarr", "immich"])
def test_widgets_survive_mixed_wrong_shapes(name):
    """Each answer in a different wrong shape: a dict where a list goes, and the reverse."""
    widget = registry.load_widgets()[name]
    count = len(widget["get"])
    for answers in ([[], {}, "x"][:count], [{}, [], 1][:count], [{"records": "x"}, {"totalRecords": "3"}, [1]][:count]):
        answers += [None] * (count - len(answers))
        fetcher.normalise_summary(widget["summarise"](answers))


@pytest.mark.parametrize("prefix", ["Bearer", "Token", "Basic", "Bot"])
def test_an_allowlisted_auth_prefix_is_accepted(prefix, monkeypatch):
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / "zz_prefixed.py"
    path.write_text(
        "WIDGET = {'name': 'zz_prefixed', 'match': ['a/b'], 'port': 1, 'get': ['/a'],\n"
        "          'auth': {'type': 'header', 'header': 'Authorization',\n"
        f"                   'prefix': '{prefix}', 'env': 'ZZ_PREFIXED_TOKEN'}}}}" + SUMMARISE)
    try:
        assert "zz_prefixed" in registry.load_widgets()
    finally:
        path.unlink()


@pytest.mark.parametrize("prefix", ["Bearer ", "bearer", "X", "", "Bearer {key}", 7])
def test_an_auth_prefix_outside_the_allowlist_is_refused(prefix, monkeypatch):
    """A fixed scheme word, never a template: a widget says WHICH scheme, not how the header
    is built, so nothing it declares can put the key somewhere else in the value."""
    from planet_express import widgets as package

    path = Path(package.__path__[0]) / "zz_badprefix.py"
    path.write_text(
        "WIDGET = {'name': 'zz_badprefix', 'match': ['a/b'], 'port': 1, 'get': ['/a'],\n"
        "          'auth': {'type': 'header', 'header': 'Authorization',\n"
        f"                   'prefix': {prefix!r}, 'env': 'ZZ_BADPREFIX_TOKEN'}}}}" + SUMMARISE)
    try:
        assert "zz_badprefix" not in registry.load_widgets()
    finally:
        path.unlink()


# ── the widgets added for T46.6, each against the shape its real app answered ────
# Every fixture below was measured on the live host by requesting the declared path with the
# app's own key and printing the JSON STRUCTURE (names and types, never values). Written from
# what the apps actually said, not from their documentation.

def _summarise(name, responses):
    return registry.load_widgets()[name]["summarise"](responses)


def test_lidarr_reads_the_arr_shape_a_version_behind():
    out = _summarise("lidarr", [
        {"totalRecords": 2, "records": [{"status": "downloading", "size": 100, "sizeleft": 25,
                                         "artist": {"artistName": "Talk Talk"}}]},
        {"totalRecords": 7},
        [{"source": "x", "type": "warning", "message": "m", "wikiUrl": "u"}],
    ])
    assert [s["k"] for s in out["stats"]] == ["QUEUE", "WANTED", "HEALTH"]
    assert [s["v"] for s in out["stats"]] == [2, 7, "1"]
    assert out["line"] == "downloading: Talk Talk · 75%"


def test_bazarr_counts_provider_health_from_the_endpoint_that_states_it():
    """badges' own `providers` number is not a count of healthy ones: the live CASA_BAZARR
    listed 8 providers, every one "Good", while badges said 3. A tile reading that would have
    been confidently wrong, so PROVIDERS comes from /api/providers."""
    out = _summarise("bazarr", [
        {"episodes": 12, "movies": 3, "providers": 3, "status": 0},
        [{"name": n, "status": "Good", "retry": "-"} for n in "abcdefgh"],
    ])
    values = dict(zip([s["k"] for s in out["stats"]], [s["v"] for s in out["stats"]]))
    assert values == {"EPISODES": 12, "MOVIES": 3, "PROVIDERS": "8/8"}
    assert out["stats"][2]["level"] == "ok"
    assert out["line"] == "15 wanting subtitles"


def test_bazarr_names_the_providers_that_are_struggling():
    out = _summarise("bazarr", [
        {"episodes": 0, "movies": 0},
        [{"name": "opensubtitlescom", "status": "Throttled"},
         {"name": "subf2m", "status": "Good"}],
    ])
    assert out["stats"][2]["v"] == "1/2" and out["stats"][2]["level"] == "warn"
    assert out["line"] == "providers struggling: opensubtitlescom"


def test_bazarr_with_no_providers_listed_at_all_is_amber():
    out = _summarise("bazarr", [{"episodes": 0, "movies": 0}, []])
    assert out["stats"][2]["v"] == "—" and out["stats"][2]["level"] == "warn"


def test_seerr_colours_only_what_somebody_has_to_act_on():
    out = _summarise("seerr", [{"total": 40, "movie": 20, "tv": 20, "pending": 2,
                                "approved": 30, "declined": 1, "processing": 3,
                                "available": 25, "completed": 25}])
    levels = dict(zip([s["k"] for s in out["stats"]], [s.get("level") for s in out["stats"]]))
    assert levels["PENDING"] == "warn" and levels["APPROVED"] is None
    assert out["line"] == "2 waiting on you"


def test_miniflux_sums_the_unread_counters():
    """{'reads': {...}, 'unreads': {feed_id: n}} -- CASA_MINIFLUX, /v1/feeds/counters."""
    out = _summarise("miniflux", [{"reads": {}, "unreads": {"1": 4, "10": 0, "22": 11}}])
    assert [s["v"] for s in out["stats"]] == ["15", 2]


def test_mealie_reads_the_household_statistics():
    out = _summarise("mealie", [{"totalRecipes": 412, "totalUsers": 2, "totalCategories": 9,
                                 "totalTags": 31, "totalTools": 4}])
    assert [s["v"] for s in out["stats"]] == [412, 31, 9]


def test_karakeep_reads_one_call_that_carries_every_count():
    out = _summarise("karakeep", [{"numBookmarks": 2140, "numFavorites": 12, "numArchived": 300,
                                   "numTags": 44, "numLists": 6, "numHighlights": 2}])
    assert [s["v"] for s in out["stats"]] == ["2,140", "300", 44, 6]


def test_audiobookshelf_turns_listening_seconds_into_something_readable():
    out = _summarise("audiobookshelf", [
        {"totalTime": 360000.0, "items": {"a": {}, "b": {}}},
        {"libraries": [{"id": "1"}, {"id": "2"}]},
    ])
    assert [s["v"] for s in out["stats"]] == ["100h", 2, 2]


def test_audiobookshelf_with_nothing_listened_to_shows_a_dash_not_a_zero():
    out = _summarise("audiobookshelf", [{"totalTime": 0, "items": {}}, {"libraries": []}])
    assert out["stats"][0]["v"] == "—"


def test_speedtest_shows_a_dash_for_a_non_finite_ping():
    """JSON `1e999` decodes to inf, and round(inf) raises -- a malformed metric must cost a
    tile, not the panel."""
    out = _summarise("speedtest", [{"data": {"ping": float("inf"),
                                             "download_bits_human": "1 Mbps"}}])
    assert out["stats"][2]["v"] == "—"


def test_speedtest_uses_the_apps_own_human_formatting():
    out = _summarise("speedtest", [{"data": {"ping": 12.4, "download_bits_human": "512 Mbps",
                                             "upload_bits_human": "48 Mbps",
                                             "service": "Ookla"}, "message": "ok"}])
    assert [s["v"] for s in out["stats"]] == ["512 Mbps", "48 Mbps", "12ms"]
    assert out["line"] == "via Ookla"


def test_tracearr_needs_no_key_at_all():
    """Its /health is unauthenticated. A widget that needs no credential cannot leak one."""
    assert registry.load_widgets()["tracearr"].get("auth") is None
    out = _summarise("tracearr", [{"status": "ok", "mode": "supervised", "db": True,
                                   "redis": True, "geoip": True}])
    assert [s["v"] for s in out["stats"]] == ["ok", "up", "up", "up"]
    assert out["line"] is None


def test_tracearr_names_the_piece_that_is_down():
    out = _summarise("tracearr", [{"status": "degraded", "db": True, "redis": False,
                                   "geoip": True}])
    assert out["line"] == "redis down"
    assert out["stats"][0]["level"] == "warn"


@pytest.mark.parametrize("name", ["lidarr", "bazarr", "seerr", "miniflux", "mealie", "karakeep",
                                  "audiobookshelf", "speedtest", "tracearr"])
def test_a_new_widget_survives_an_api_that_answers_with_nothing(name):
    """A reachable API returning null bodies must not take the panel down with it."""
    widget = registry.load_widgets()[name]
    out = widget["summarise"]([None] * len(widget["get"]))
    assert out["stats"] and all("k" in s and "v" in s for s in out["stats"])


def test_the_overseerr_repo_is_spelled_the_way_docker_hub_spells_it():
    """The repo is matched whole, so `sct/overseerr` would match nothing at all."""
    assert registry.match_widget("sctx/overseerr:latest")["name"] == "seerr"
    assert registry.match_widget("ghcr.io/seerr-team/seerr:latest")["name"] == "seerr"


def test_legacy_mealie_gets_no_widget_rather_than_a_broken_one():
    """hkotel/mealie is v0: port 80, and no /api/households/statistics. A panel that can only
    be unreachable or 404 is worse than no panel."""
    assert registry.match_widget("hkotel/mealie:latest") is None
    assert registry.match_widget("ghcr.io/mealie-recipes/mealie:latest")["name"] == "mealie"


# ── no widget may render a number that is not one ───────────────────────────────

# One fixture per shipped widget: the smallest response set that reaches every numeric path.
_POISON_SHAPES = {
        "adguard": [{"num_dns_queries": 1, "num_blocked_filtering": 1,
                     "avg_processing_time": 0.04, "top_blocked_domains": [{"a.example": 1}]}],
        "audiobookshelf": [{"totalTime": 1.0, "items": {"a": {}}}, {"libraries": [{"id": "1"}]}],
        "bazarr": [{"episodes": 1, "movies": 1}, [{"name": "p", "status": "Good"}]],
        "immich": [{"photos": 1, "videos": 1, "usage": 1}],
        "karakeep": [{"numBookmarks": 1, "numArchived": 1, "numTags": 1, "numLists": 1}],
        "lidarr": [{"totalRecords": 1, "records": [{"status": "downloading", "size": 2,
                                                    "sizeleft": 1}]}, {"totalRecords": 1}, []],
        "mealie": [{"totalRecipes": 1, "totalTags": 1, "totalCategories": 1}],
        "miniflux": [{"reads": {}, "unreads": {"1": 1}}],
        "prowlarr": [[{"id": 1, "status": "x"}], []],
        "radarr": [{"totalRecords": 1, "records": [{"status": "downloading", "size": 2,
                                                    "sizeleft": 1}]}, {"totalRecords": 1}, []],
        "seerr": [{"total": 1, "pending": 1, "approved": 1, "available": 1}],
        "sonarr": [{"totalRecords": 1, "records": [{"title": "t", "size": 2, "sizeleft": 1}]},
                   {"totalRecords": 1}, []],
        "speedtest": [{"data": {"ping": 1.0, "download_bits_human": "1 Mbps",
                                "upload_bits_human": "1 Mbps"}}],
        "tracearr": [{"status": "ok", "db": True, "redis": True, "geoip": True}],
}


def _poison_every_widget(value_for):
    """Replace every number in every widget's fixture, then check what reaches the page.

    normalise_summary is the real boundary: it clamps a raw inf to a dash, but a widget that
    has already FORMATTED one lands "infh" or "inf%" in a tile, which the boundary cannot
    undo -- and round(inf) raises, costing the whole panel rather than one tile.
    """
    from planet_express.widgets.fetcher import normalise_summary

    counter = itertools.count()

    def poisoned(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value_for(next(counter))
        if isinstance(value, dict):
            return {k: poisoned(v) for k, v in value.items()}
        if isinstance(value, list):
            return [poisoned(v) for v in value]
        return value

    for name, widget in registry.load_widgets().items():
        assert name in _POISON_SHAPES, f"{name} has no poison fixture -- add one"
        assert len(_POISON_SHAPES[name]) == len(widget["get"]), (
            f"{name}: fixture has {len(_POISON_SHAPES[name])} responses, the widget asks for "
            f"{len(widget['get'])}")
        out = normalise_summary(widget["summarise"](poisoned(_POISON_SHAPES[name])))
        rendered = [str(stat["v"]) for stat in out["stats"]] + [str(out.get("line") or "")]
        for text in rendered:
            assert not any(bad in text.lower() for bad in ("inf", "nan")), f"{name}: {text}"


@pytest.mark.parametrize("poison", [float("inf"), float("-inf"), float("nan"), 1e308 * 10])
def test_no_shipped_widget_renders_a_non_finite_number(poison):
    """JSON's `1e999` decodes to inf, straight into arithmetic no one guarded."""
    _poison_every_widget(lambda _: poison)


def test_no_shipped_widget_breaks_on_an_oversized_json_integer():
    """`10**309` decodes to an arbitrary-precision int, not a float, so it is not inf and
    math.isfinite raises OverflowError on it while converting -- the guard being the thing
    that breaks. A widget must still show a dash, not lose its panel."""
    _poison_every_widget(lambda _: 10 ** 309)


def test_ratio_of_two_finite_numbers_that_is_not_finite():
    """Both operands pass every guard and the answer is still inf: a denominator small enough
    makes the division overflow. The result has to be checked, not only the inputs."""
    from planet_express.core import numbers

    assert numbers.finite(numbers.MAX) and numbers.finite(1e-320)   # both pass every guard
    assert numbers.ratio(numbers.MAX, 1e-320) is None                # and the answer is inf
    assert numbers.ratio(1, 4) == 25


@pytest.mark.parametrize("big", [1e308, None])
def test_no_shipped_widget_overflows_while_computing(big):
    """Operands that each pass every guard can still compute to something that does not.

    Two magnitudes, because they fail differently: 1e308 is rejected outright by finite(),
    while +/-MAX is *accepted*, so the widget really does the arithmetic -- `size - sizeleft`
    doubles, `avg * 1000` lands past MAX -- and the guard has to hold on the way out.
    """
    from planet_express.core.numbers import MAX

    value = big if big is not None else MAX
    _poison_every_widget(lambda i: value if i % 2 == 0 else -value)


@pytest.mark.parametrize("image", [
    "lscr.io/linuxserver/overseerr:latest",
    "ghcr.io/linuxserver/overseerr:latest",
    "linuxserver/overseerr:latest",
    "sctx/overseerr:latest",
    "ghcr.io/seerr-team/seerr:release",
])
def test_every_seerr_source_both_matches_and_passes_provenance(image):
    """Matching normalises the registry away, but provenance checks the digest's registry
    against the match list -- so a source missing there matches and then gets no key, which
    reads as "no widget" rather than as a misconfiguration."""
    widget = registry.match_widget(image)
    assert widget is not None and widget["name"] == "seerr", image
    repo = image.split(":")[0]
    assert registry.trusted_provenance(widget, [f"{repo}@sha256:{'a' * 64}"]), image


# ── two bugs this round's shared numeric helper introduced ──────────────────────

@pytest.mark.parametrize("usage,expected", [
    (5 * 1024 ** 4, "5.0 TiB"),      # a real photo library, far past the count bound
    (2 * 1024 ** 3, "2.0 GiB"),
    (10 ** 309, "—"),                # still an int too large to become a float
])
def test_immich_reports_a_library_larger_than_the_count_bound(usage, expected):
    """Bytes are not counts. MAX is 1e12, which is 931 GiB -- a bound that is generous for a
    number of photos and absurd for their size, and blanked the USED tile on any library
    past it."""
    widget = registry.load_widgets()["immich"]
    out = widget["summarise"]([{"photos": 1, "videos": 1, "usage": usage}])
    assert [s["v"] for s in out["stats"] if s["k"] == "USED"] == [expected]


def test_the_widgets_directory_holds_only_widgets():
    """load_widgets() imports every non-underscore .py file here and treats it as a candidate
    widget, so a shared helper dropped in this package gets imported twice by two paths: once
    normally by the widgets that use it, once by the scan. Shared code goes in core/."""
    directory = Path(__file__).resolve().parent.parent / "planet_express" / "widgets"
    modules = {p.stem for p in directory.glob("*.py")
               if not p.stem.startswith("_") and p.stem not in ("registry", "fetcher")}
    assert modules == set(registry.load_widgets()), (
        "every module in planet_express/widgets must define a WIDGET; "
        f"strays: {sorted(modules - set(registry.load_widgets()))}")


@pytest.mark.parametrize("name", ["sonarr", "radarr", "lidarr"])
def test_arr_progress_survives_a_queue_item_larger_than_a_terabyte(name):
    """Same bug as Immich's USED tile, in the widget that measures a download: size and
    sizeleft are bytes, and a 4K season pack clears the 1e12 count bound. Under the count
    bound a half-finished 2 TB item reported 0%, which reads as stalled."""
    item = {"title": "t", "status": "downloading", "size": 2 * 10 ** 12, "sizeleft": 10 ** 12}
    widget = registry.load_widgets()[name]
    out = widget["summarise"]([{"totalRecords": 1, "records": [item]}, {"totalRecords": 1}, []])
    rendered = str(out.get("line") or "") + "".join(str(r.get("pct")) for r in out["rows"])
    assert "50" in rendered, f"{name}: half-done 2 TB item rendered as {rendered!r}"
