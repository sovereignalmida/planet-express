"""planet_express/execution/boot_order.py: real files in tmp_path, no Docker. The actual
decision casa_boot.py now acts on, not just logs."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core import dependencies as deps
from planet_express.execution import boot_order


def write_stack(tmp_path: Path, name: str, content: str) -> Path:
    stack_dir = tmp_path / name
    stack_dir.mkdir()
    (stack_dir / "docker-compose.yml").write_text(content)
    return stack_dir


def test_no_cross_project_dependency_leaves_baseline_untouched(tmp_path):
    a = write_stack(tmp_path, "a", "services:\n  web:\n    image: nginx\n")
    b = write_stack(tmp_path, "b", "services:\n  app:\n    image: nginx\n")
    order, lines = boot_order.order_stacks([a, b])
    assert order == [a, b]
    assert not any("reordered" in line for line in lines)


def test_wrong_baseline_order_is_corrected_the_historical_gluetun_case(tmp_path):
    """The actual incident this whole effort traces back to, reproduced end to end: if media
    (qbittorrent) were ever ordered before network (gluetun) -- exactly backwards -- this must
    fix it, not just log a warning about it."""
    network = write_stack(tmp_path, "network", (
        "services:\n"
        "  gluetun:\n"
        "    container_name: CASA_GLUETON\n"
    ))
    media = write_stack(tmp_path, "media", (
        "services:\n"
        "  qbittorrent:\n"
        "    container_name: CASA_QBIT\n"
        "    network_mode: container:CASA_GLUETON\n"
    ))
    order, lines = boot_order.order_stacks([media, network])  # wrong order on purpose
    assert order == [network, media]
    assert any("reordered" in line and "media" in line and "network" in line for line in lines)


def test_correct_baseline_order_is_left_alone(tmp_path):
    network = write_stack(tmp_path, "network", "services:\n  gluetun:\n    container_name: CASA_GLUETON\n")
    media = write_stack(tmp_path, "media", (
        "services:\n  qbittorrent:\n    container_name: CASA_QBIT\n"
        "    network_mode: container:CASA_GLUETON\n"
    ))
    order, lines = boot_order.order_stacks([network, media])  # already correct
    assert order == [network, media]
    assert not any("reordered" in line for line in lines)


def test_unreadable_compose_file_falls_back_to_baseline(tmp_path):
    missing = tmp_path / "ghost"
    missing.mkdir()
    other = write_stack(tmp_path, "other", "services:\n  a:\n    image: nginx\n")
    order, lines = boot_order.order_stacks([missing, other])
    assert order == [missing, other]
    assert any("cannot read" in line and "using baseline order" in line for line in lines)


def test_unparseable_compose_file_falls_back_to_baseline(tmp_path):
    bad = write_stack(tmp_path, "bad", "not: valid: yaml: : :")
    other = write_stack(tmp_path, "other", "services:\n  a:\n    image: nginx\n")
    order, lines = boot_order.order_stacks([bad, other])
    assert order == [bad, other]
    assert any("cannot parse" in line and "using baseline order" in line for line in lines)


def test_cycle_falls_back_to_baseline(tmp_path):
    """A contradicts B contradicts A -- can't happen from one detector today (namespace/
    depends_on edges are one-directional per reference), but the fallback must hold regardless
    of how a future detector might produce one."""
    a = write_stack(tmp_path, "a", (
        "services:\n  web:\n    container_name: CASA_A\n    network_mode: container:CASA_B\n"
    ))
    b = write_stack(tmp_path, "b", (
        "services:\n  web:\n    container_name: CASA_B\n    network_mode: container:CASA_A\n"
    ))
    order, lines = boot_order.order_stacks([a, b])
    assert order == [a, b]
    assert any("cycle" in line and "using baseline order" in line for line in lines)


def test_no_stacks_does_not_crash():
    assert boot_order.order_stacks([]) == ([], [])


def test_basename_collision_falls_back_safely_instead_of_dropping_a_stack(tmp_path, monkeypatch):
    """codex review: two stack directories under different parents can share a bare `.name`
    (config.active_stack_dirs() shouldn't produce this -- one stacks_root -- but this function
    doesn't trust that). Must never silently return one path twice and drop the other.

    A real name collision actually makes the two stacks indistinguishable to the graph itself
    (edges are keyed by name, so an edge between them wouldn't even read as cross-project) --
    this is a safety net for a scenario the system's own invariants already prevent, so it's
    tested by forcing the exact shape directly rather than by a fragile real-detector fixture.
    """
    root_a = tmp_path / "a"
    root_a.mkdir()
    root_b = tmp_path / "b"
    root_b.mkdir()
    dir1 = write_stack(root_a, "network", "services:\n  one:\n    image: nginx\n")
    dir2 = write_stack(root_b, "network", "services:\n  two:\n    image: nginx\n")  # same basename
    dir3 = write_stack(tmp_path, "media", "services:\n  three:\n    image: nginx\n")

    monkeypatch.setattr(
        deps.DependencyGraph, "stack_precedence", lambda self, names: {"media": {"network"}},
    )
    monkeypatch.setattr(
        deps, "stable_topological_order", lambda baseline, must_precede: ["media", "network", "network"],
    )

    order, lines = boot_order.order_stacks([dir1, dir2, dir3])
    assert sorted(order, key=str) == sorted([dir1, dir2, dir3], key=str)
    assert order == [dir1, dir2, dir3]  # baseline, unchanged
    assert any("did not produce exactly the original stack set" in line for line in lines)


def test_internal_exception_during_graph_computation_falls_back_to_baseline(tmp_path, monkeypatch):
    """codex review: order_stacks() must catch a failure in graph construction/reasoning
    itself, not only in reading/parsing compose files."""
    a = write_stack(tmp_path, "a", "services:\n  web:\n    image: nginx\n")
    b = write_stack(tmp_path, "b", "services:\n  app:\n    image: nginx\n")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated detector bug")

    monkeypatch.setattr(deps, "discover", boom)
    order, lines = boot_order.order_stacks([a, b])
    assert order == [a, b]
    assert any("could not compute an order" in line and "using baseline order" in line for line in lines)
