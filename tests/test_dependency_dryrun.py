"""planet_express/execution/dependency_dryrun.py: casa_boot.py's dry-run logging, real files
in tmp_path -- no Docker, and no change to anything casa_boot.py actually does."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import dependency_dryrun


def write_stack(tmp_path: Path, name: str, content: str) -> Path:
    stack_dir = tmp_path / name
    stack_dir.mkdir()
    (stack_dir / "docker-compose.yml").write_text(content)
    return stack_dir


def test_unreadable_compose_file_is_a_line_not_an_exception(tmp_path):
    missing = tmp_path / "ghost"
    missing.mkdir()  # directory exists, docker-compose.yml inside it does not
    lines = dependency_dryrun.summarize([missing])
    assert any("cannot read" in line for line in lines)


def test_unparseable_compose_file_is_a_line_not_an_exception(tmp_path):
    stack_dir = write_stack(tmp_path, "bad", "not: valid: yaml: : :")
    lines = dependency_dryrun.summarize([stack_dir])
    assert any("cannot parse" in line for line in lines)


def test_invalid_utf8_compose_file_is_a_line_not_an_exception(tmp_path):
    """read_text() can raise UnicodeDecodeError, not OSError -- a codex-review regression."""
    stack_dir = tmp_path / "bad-encoding"
    stack_dir.mkdir()
    (stack_dir / "docker-compose.yml").write_bytes(b"services:\n  a:\n    image: \xff\xfe\n")
    lines = dependency_dryrun.summarize([stack_dir])
    assert any("cannot read" in line for line in lines)


def test_gluetun_incident_reproduced_from_real_files_with_correct_boot_order(tmp_path):
    """network is ordered first, same as casa_boot.py's real rule -- the cross-project edge
    should report satisfied, not a violation."""
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
    lines = dependency_dryrun.summarize([network, media])
    assert any("1 dependency edge(s) found (1 ordering" in line for line in lines)
    assert any("0 unresolved" in line for line in lines)
    assert any("ok:" in line and "media/qbittorrent" in line for line in lines)
    assert not any("WARNING" in line for line in lines)


def test_wrong_boot_order_is_flagged_as_a_violation(tmp_path):
    """The failure mode this exists to catch: if media were ever ordered before network,
    this must say so loudly -- never silently."""
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
    lines = dependency_dryrun.summarize([media, network])  # wrong order on purpose
    assert any("WARNING" in line and "media/qbittorrent" in line and "'network'" in line for line in lines)


def test_unresolved_dependency_is_logged(tmp_path):
    stack_dir = write_stack(tmp_path, "s", (
        "services:\n"
        "  a:\n"
        "    network_mode: container:GHOST\n"
    ))
    lines = dependency_dryrun.summarize([stack_dir])
    assert any("unresolved" in line and "GHOST" in line for line in lines)


def test_no_stacks_does_not_crash():
    assert dependency_dryrun.summarize([]) == ["[dry-run graph] 0 dependency edge(s) found (0 ordering, 0 informational), 0 unresolved"]
