"""planet_express/core/dependencies.py (v3 Phase 1): the dependency graph and its detector
registry. Pure parsing + detection, no Docker, no filesystem beyond reading fixture text."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core import dependencies as deps

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compose"


def load(name: str) -> deps.ComposeStack:
    content = (FIXTURES / name / "docker-compose.yml").read_text()
    return deps.load_compose_stack(name, content)


def stack(name: str, services: dict) -> deps.ComposeStack:
    return deps.ComposeStack(name=name, services=services)


# --- the real incident, as data instead of prose ------------------------------------------


def test_gluetun_incident_both_edges_found():
    """CASA_GSP (same project as gluetun) and CASA_QBIT (a different project) both reference
    CASA_GLUETON's namespace. Both must be found; only one is cross-project."""
    network, media = load("network"), load("media")
    graph = deps.discover((network, media))

    assert not graph.unresolved

    gsp = next(d for d in graph.dependencies if d.source == "network/gsp")
    assert gsp.target == "network/gluetun"
    assert gsp.cross_project is False
    assert gsp.kind == "namespace"

    qbit = next(d for d in graph.dependencies if d.source == "media/qbittorrent")
    assert qbit.target == "network/gluetun"
    assert qbit.cross_project is True
    assert qbit.kind == "namespace"


def test_gluetun_incident_order_independent():
    """The detector must not assume the namespace owner's stack was parsed first -- the real
    boot order parses stacks in whatever order the directory glob returns."""
    media, network = load("media"), load("network")
    graph = deps.discover((media, network))
    qbit = next(d for d in graph.dependencies if d.source == "media/qbittorrent")
    assert qbit.cross_project is True


def test_what_must_be_healthy_before_qbittorrent_starts():
    """The hook casa_boot.py (or its successor) would call at boot time."""
    graph = deps.discover((load("network"), load("media")))
    must_be_healthy = graph.for_service("media", "qbittorrent")
    assert len(must_be_healthy) == 1
    assert must_be_healthy[0].target == "network/gluetun"


# --- ordering_violations() ------------------------------------------------------------------


def test_ordering_violations_correct_order_is_not_a_violation():
    graph = deps.discover((load("network"), load("media")))
    assert graph.ordering_violations({"network": 0, "media": 1}) == ()


def test_ordering_violations_wrong_order_is_a_violation():
    graph = deps.discover((load("network"), load("media")))
    violations = graph.ordering_violations({"network": 1, "media": 0})
    assert len(violations) == 1
    assert violations[0].source == "media/qbittorrent"


def test_ordering_violations_equal_position_is_a_violation():
    """Docstring says "at or after"; two stacks claiming the same position is not a guarantee
    the target comes first, so it must count as a violation too, not pass silently."""
    graph = deps.discover((load("network"), load("media")))
    violations = graph.ordering_violations({"network": 0, "media": 0})
    assert len(violations) == 1


def test_ordering_violations_unknown_stack_is_skipped_not_a_violation():
    graph = deps.discover((load("network"), load("media")))
    assert graph.ordering_violations({"network": 0}) == ()


# --- namespace detector: declines vs. unresolved ------------------------------------------


@pytest.mark.parametrize("mode", ["host", "bridge", "none", "default"])
def test_ordinary_network_mode_is_not_a_dependency(mode):
    s = stack("s", {"a": {"network_mode": mode}})
    graph = deps.discover((s,))
    assert not graph.dependencies
    assert not graph.unresolved


def test_service_reference_outside_its_own_project_is_unresolved():
    """`network_mode: service:x` cannot cross a compose project boundary -- compose itself
    would refuse this, so it must surface as unresolved, not as a found edge."""
    s = stack("s", {"a": {"network_mode": "service:nonexistent"}})
    graph = deps.discover((s,))
    assert not graph.dependencies
    assert len(graph.unresolved) == 1
    assert graph.unresolved[0].detector == "namespace_reference"
    assert "service: only resolves within one compose project" in graph.unresolved[0].reason


def test_container_reference_to_unknown_name_is_unresolved_not_dropped():
    """The core requirement: a reference nothing can resolve surfaces, it does not vanish."""
    s = stack("s", {"a": {"network_mode": "container:GHOST"}})
    graph = deps.discover((s,))
    assert not graph.dependencies
    assert len(graph.unresolved) == 1
    u = graph.unresolved[0]
    assert u.field == "network_mode"
    assert u.value == "container:GHOST"
    assert "no known service declares container_name" in u.reason


def test_unrecognized_network_mode_shape_is_unresolved():
    s = stack("s", {"a": {"network_mode": "weird-custom-thing"}})
    graph = deps.discover((s,))
    assert not graph.dependencies
    assert len(graph.unresolved) == 1
    assert "not a recognized network_mode value" in graph.unresolved[0].reason


# --- depends_on detector -------------------------------------------------------------------


def test_depends_on_list_form():
    s = stack("s", {"web": {"depends_on": ["db"]}, "db": {}})
    graph = deps.discover((s,))
    assert graph.dependencies == (
        deps.Dependency(
            kind="depends_on", source="s/web", target="s/db",
            cross_project=False, detector="depends_on",
        ),
    )


def test_depends_on_long_form_dict():
    s = stack("s", {"web": {"depends_on": {"db": {"condition": "service_healthy"}}}, "db": {}})
    graph = deps.discover((s,))
    assert graph.dependencies[0].target == "s/db"


def test_depends_on_unknown_target_is_unresolved():
    s = stack("s", {"web": {"depends_on": ["nonexistent"]}})
    graph = deps.discover((s,))
    assert not graph.dependencies
    assert graph.unresolved[0].reason == "no service 'nonexistent' in stack 's'"


# --- shared mount detector ------------------------------------------------------------------


def test_shared_bind_mount_cross_project():
    a = stack("media", {"tubearchivist": {"volumes": ["/mnt/main/youtube:/youtube"]}})
    b = stack("backup", {"borg": {"volumes": [{"type": "bind", "source": "/mnt/main/youtube", "target": "/data"}]}})
    graph = deps.discover((a, b))
    assert graph.dependencies == (
        deps.Dependency(
            kind="shared_mount", source="backup/borg", target="media/tubearchivist",
            cross_project=True, detector="shared_mount", detail="/mnt/main/youtube",
        ),
    )
    assert not graph.unresolved


def test_shared_bind_mount_same_project_not_cross_project():
    a = stack("media", {
        "a": {"volumes": ["/mnt/main/media:/data"]},
        "b": {"volumes": ["/mnt/main/media:/data:ro"]},
    })
    graph = deps.discover((a,))
    assert len(graph.dependencies) == 1
    assert graph.dependencies[0].cross_project is False


def test_named_volume_is_not_indexed_as_a_bind_mount():
    a = stack("media", {"a": {"volumes": ["config:/config"]}})
    b = stack("backup", {"b": {"volumes": ["config:/config"]}})
    graph = deps.discover((a, b))
    assert not graph.dependencies
    assert not graph.unresolved


def test_unshared_mount_produces_no_edge():
    a = stack("media", {"a": {"volumes": ["/mnt/main/media:/data"]}})
    graph = deps.discover((a,))
    assert not graph.dependencies


# --- traefik router -> service detector -----------------------------------------------------


def test_router_service_label_cross_project():
    """qbit's labels have to live on gluetun -- the compose-time version of the exact case
    actions.py's parse_router_owners() docstring describes."""
    network = stack("network", {
        "gluetun": {
            "labels": [
                "traefik.http.routers.qbit.service=qbit",
                "traefik.http.services.gluetun.loadbalancer.server.port=8080",
            ],
        },
    })
    media = stack("media", {
        "qbittorrent": {
            "labels": {"traefik.http.services.qbit.loadbalancer.server.port": "8081"},
        },
    })
    graph = deps.discover((network, media))
    router_edges = [d for d in graph.dependencies if d.kind == "traefik_router"]
    assert router_edges == [
        deps.Dependency(
            kind="traefik_router", source="network/gluetun", target="media/qbittorrent",
            cross_project=True, detector="traefik_router",
            detail="router qbit -> service qbit",
        ),
    ]
    assert not graph.unresolved


def test_router_with_no_service_label_produces_no_edge():
    s = stack("s", {"web": {"labels": ["traefik.http.routers.web.rule=Host(`x`)"]}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "traefik_router"]
    assert not graph.unresolved


def test_router_service_label_naming_unknown_service_is_unresolved():
    s = stack("s", {"web": {"labels": {"traefik.http.routers.web.service": "ghost"}}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "traefik_router"]
    unresolved = [u for u in graph.unresolved if u.detector == "traefik_router"]
    assert len(unresolved) == 1
    assert "ghost" in unresolved[0].value
    assert "no known service's labels declare" in unresolved[0].reason


def test_ambiguous_service_name_is_unresolved_not_arbitrarily_picked():
    """Two services both declaring traefik.http.services.shared...: resolving to whichever was
    iterated last would make the graph depend on argument order. Must surface as ambiguous."""
    a = stack("a", {"first": {"labels": {"traefik.http.services.shared.loadbalancer.server.port": "80"}}})
    b = stack("b", {"second": {"labels": {"traefik.http.services.shared.loadbalancer.server.port": "81"}}})
    r = stack("r", {"router": {"labels": {"traefik.http.routers.r.service": "shared"}}})

    forward = deps.discover((a, b, r))
    backward = deps.discover((b, a, r))
    for graph in (forward, backward):
        assert not [d for d in graph.dependencies if d.kind == "traefik_router"]
        unresolved = [u for u in graph.unresolved if u.detector == "traefik_router"]
        assert len(unresolved) == 1
        assert "2 different services declare" in unresolved[0].reason


def test_multiple_loadbalancer_labels_on_one_service_is_one_declarer_not_two():
    """A real service commonly carries several .loadbalancer.* labels (port, passhostheader,
    ...) -- must not be misread as two different services declaring the same name."""
    s = stack("s", {"web": {"labels": {
        "traefik.http.routers.web.service": "web",
        "traefik.http.services.web.loadbalancer.server.port": "80",
        "traefik.http.services.web.loadbalancer.passhostheader": "true",
    }}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "traefik_router"]
    assert not [u for u in graph.unresolved if u.detector == "traefik_router"]


def test_router_naming_its_own_container_service_is_not_an_edge():
    """Valid Traefik config, not an inter-service dependency -- must not appear as a self-edge."""
    s = stack("s", {"web": {"labels": {
        "traefik.http.routers.web.service": "web",
        "traefik.http.services.web.loadbalancer.server.port": "80",
    }}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "traefik_router"]
    assert not [u for u in graph.unresolved if u.detector == "traefik_router"]


# --- env-var reference detector (Immich/redis/database -- the real gap with no depends_on) --


def test_bare_hostname_env_var_match():
    """REDIS_HOST: redis -- the simplest real case, and the one triggering this detector."""
    s = stack("media", {
        "immich": {"environment": {"REDIS_HOSTNAME": "redis"}},
        "redis": {},
        "database": {},
    })
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert edges == [
        deps.Dependency(
            kind="env_reference", source="media/immich", target="media/redis",
            cross_project=False, detector="env_reference", detail="REDIS_HOSTNAME='redis'",
        ),
    ]


def test_url_embedded_host_env_var_match():
    s = stack("media", {
        "immich": {"environment": ["DB_URL=postgres://user:pass@database:5432/immich"]},
        "database": {},
    })
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert edges[0].target == "media/database"


def test_non_connection_key_is_ignored():
    """CACHE_DRIVER: redis names a backend type, not a hostname -- the key doesn't look
    connection-shaped, so this must not be flagged."""
    s = stack("media", {"immich": {"environment": {"CACHE_DRIVER": "redis"}}, "redis": {}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_external_hostname_is_not_flagged_and_not_unresolved():
    """A value naming no known service is an external host, not ambiguous -- no edge, and
    (unlike namespace/traefik) nothing to surface as unresolved either."""
    s = stack("media", {"immich": {"environment": {"REDIS_HOST": "redis.example.com"}}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]
    assert not [u for u in graph.unresolved if u.detector == "env_reference"]


def test_substring_false_positives_are_rejected():
    """CURL contains URL, SERVERLESS contains SERVER -- neither is a real connection-shaped
    key, and a plain substring search used to match both."""
    s = stack("media", {
        "a": {"environment": {"CURL_CA_BUNDLE": "redis", "SERVERLESS_MODE": "redis"}},
        "redis": {},
    })
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_host_token_as_whole_word_still_matches():
    """The token-boundary fix must not overcorrect: HOST as its own token (surrounded by
    underscores) still counts, even inside a longer key."""
    s = stack("media", {"a": {"environment": {"POSTGRES_HOST_AUTH_METHOD": "database"}}, "database": {}})
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1


def test_bare_double_slash_in_a_path_is_not_a_url_authority():
    """No scheme before the "//" -- this is a path, not a URL, and must not match."""
    s = stack("media", {"a": {"environment": {"CONFIG_URI": "/config//redis/file"}}, "redis": {}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_bare_host_port_form_matches():
    s = stack("media", {"a": {"environment": {"REDIS_ADDR": "redis:6379"}}, "redis": {}})
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert edges[0].target == "media/redis"


def test_multi_host_uri_matches_every_host():
    s = stack("media", {
        "a": {"environment": {"MONGO_URL": "mongodb://mongo1:27017,mongo2:27017/db"}},
        "mongo1": {}, "mongo2": {},
    })
    graph = deps.discover((s,))
    targets = {d.target for d in graph.dependencies if d.kind == "env_reference"}
    assert targets == {"media/mongo1", "media/mongo2"}


def test_hostname_match_is_case_insensitive():
    s = stack("media", {"a": {"environment": {"REDIS_HOST": "REDIS"}}, "redis": {}})
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1


def test_multiple_env_vars_agreeing_on_one_target_produce_one_edge():
    s = stack("media", {
        "a": {"environment": {
            "REDIS_HOST": "redis",
            "REDIS_URL": "redis://redis:6379",
        }},
        "redis": {},
    })
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert "REDIS_HOST=" in edges[0].detail
    assert "REDIS_URL=" in edges[0].detail


def test_same_stack_service_wins_over_duplicate_name_elsewhere():
    """Two unrelated stacks each have a service called redis -- a/app's local redis wins;
    it must not also (or instead) point at b/redis."""
    a = stack("a", {"app": {"environment": {"REDIS_HOST": "redis"}}, "redis": {}})
    b = stack("b", {"redis": {}})
    graph = deps.discover((a, b))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert edges[0].target == "a/redis"
    assert edges[0].cross_project is False


def test_duplicate_name_across_stacks_with_no_local_match_is_ambiguous_and_skipped():
    """No local redis in the referencing stack, and two unrelated stacks each have one --
    genuinely ambiguous, must not guess either one."""
    app = stack("app", {"web": {"environment": {"REDIS_HOST": "redis"}}})
    a = stack("a", {"redis": {}})
    b = stack("b", {"redis": {}})
    graph = deps.discover((app, a, b))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_same_stack_case_variant_duplicate_is_also_ambiguous_not_order_dependent():
    """Two distinct service names in the SAME stack that both normalize to the same lowercase
    name ('redis' and 'REDIS') must not pick one arbitrarily by insertion order either."""
    s = stack("media", {
        "app": {"environment": {"REDIS_HOST": "redis"}},
        "redis": {}, "REDIS": {},
    })
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_url_authority_stops_at_query_string():
    s = stack("media", {"a": {"environment": {"REDIS_URL": "redis://redis:6379?db=1"}}, "redis": {}})
    graph = deps.discover((s,))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert edges[0].target == "media/redis"


def test_multi_host_uri_with_query_string_matches_all_hosts():
    s = stack("media", {
        "a": {"environment": {"MONGO_URL": "mongodb://mongo1:27017,mongo2:27017?replicaSet=rs0"}},
        "mongo1": {}, "mongo2": {},
    })
    graph = deps.discover((s,))
    targets = {d.target for d in graph.dependencies if d.kind == "env_reference"}
    assert targets == {"media/mongo1", "media/mongo2"}


def test_self_reference_is_not_an_edge():
    s = stack("media", {"redis": {"environment": {"REDIS_HOSTNAME": "redis"}}})
    graph = deps.discover((s,))
    assert not [d for d in graph.dependencies if d.kind == "env_reference"]


def test_cross_project_env_reference():
    app = stack("app", {"web": {"environment": {"DATABASE_HOSTNAME": "database"}}})
    data = stack("data", {"database": {}})
    graph = deps.discover((app, data))
    edges = [d for d in graph.dependencies if d.kind == "env_reference"]
    assert len(edges) == 1
    assert edges[0].cross_project is True
    assert edges[0].target == "data/database"


def test_env_reference_is_never_an_ordering_edge():
    """Informational only -- must never feed ordering_violations(), since it's a heuristic
    match, not a Docker-native guarantee."""
    app = stack("app", {"web": {"environment": {"DATABASE_HOSTNAME": "database"}}})
    data = stack("data", {"database": {}})
    graph = deps.discover((app, data))
    assert graph.ordering_violations({"app": 0, "data": 1}) == ()
    assert graph.ordering_violations({"app": 1, "data": 0}) == ()


# --- parsing --------------------------------------------------------------------------------


def test_load_compose_stack_rejects_non_yaml():
    with pytest.raises(deps.ComposeParseError):
        deps.load_compose_stack("s", "not: valid: yaml: at: all: : :")


def test_load_compose_stack_rejects_no_services():
    with pytest.raises(deps.ComposeParseError):
        deps.load_compose_stack("s", "name: s\n")
