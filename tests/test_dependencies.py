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


# --- parsing --------------------------------------------------------------------------------


def test_load_compose_stack_rejects_non_yaml():
    with pytest.raises(deps.ComposeParseError):
        deps.load_compose_stack("s", "not: valid: yaml: at: all: : :")


def test_load_compose_stack_rejects_no_services():
    with pytest.raises(deps.ComposeParseError):
        deps.load_compose_stack("s", "name: s\n")
