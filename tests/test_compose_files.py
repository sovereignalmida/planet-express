"""planet_express/execution/compose_files.py (slice 5b-4): compare-and-swap, atomic replacement,
symlink refusals and the inverse. Real files in a tmp_path, no Docker."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import compose_files as cf

OLD = "services:\n  web:\n    image: nginx:1.27\n"
NEW = "services:\n  web:\n    image: nginx:1.28\n"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "stacks").mkdir()
    return tmp_path / "stacks"


def stack(root, name="media", content=OLD):
    directory = root / name
    directory.mkdir(exist_ok=True)
    path = directory / cf.COMPOSE_FILENAME
    if content is not None:
        path.write_text(content)
    return path


def write(root, path, content, **kwargs):
    kwargs.setdefault("expected_sha256", None)
    kwargs.setdefault("expect_absent", False)
    kwargs.setdefault("backup_suffix", ".bak.test")
    return cf.write_compose(path, content, stacks_root=root, **kwargs)


def test_an_edit_replaces_the_file_and_keeps_a_backup(root):
    path = stack(root)
    record = write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    assert path.read_text() == NEW
    assert Path(record.backup_path).read_text() == OLD
    assert record.previous_sha256 == cf.sha256_text(OLD)
    assert not record.created_file and not record.created_directory


def test_a_file_that_changed_since_approval_is_never_overwritten(root):
    path = stack(root)
    path.write_text("services:\n  web:\n    image: someone-else-was-here\n")
    with pytest.raises(cf.ComposeWriteError, match="changed since"):
        write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    assert "someone-else-was-here" in path.read_text()


def test_a_new_stack_creates_the_directory_and_the_file(root):
    path = root / "brand-new" / cf.COMPOSE_FILENAME
    record = write(root, path, NEW, expect_absent=True)
    assert path.read_text() == NEW
    assert record.created_file and record.created_directory
    assert record.backup_path is None
    assert path.stat().st_mode & 0o777 == 0o644


def test_a_new_stack_that_appeared_in_the_meantime_is_refused(root):
    path = stack(root, "brand-new", content="someone got here first\n")
    with pytest.raises(cf.ComposeWriteError, match="exists now"):
        write(root, path, NEW, expect_absent=True)
    assert path.read_text() == "someone got here first\n"


def test_an_edit_of_a_file_that_vanished_is_refused(root):
    path = stack(root, "media", content=None)
    with pytest.raises(cf.ComposeWriteError, match="no longer exists"):
        write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    assert not path.exists()


def test_the_replacement_keeps_the_mode_of_the_file_it_replaces(root):
    path = stack(root)
    os.chmod(path, 0o640)
    write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    assert path.stat().st_mode & 0o777 == 0o640


def test_no_temporary_file_is_left_behind(root):
    path = stack(root)
    write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    assert sorted(p.name for p in path.parent.iterdir()) == [
        cf.COMPOSE_FILENAME, cf.COMPOSE_FILENAME + ".bak.test"]


@pytest.mark.parametrize("what", ["directory", "file", "root"])
def test_a_symlink_anywhere_on_the_path_refuses_the_write(root, tmp_path, what):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if what == "directory":
        (root / "media").symlink_to(elsewhere)
        path = root / "media" / cf.COMPOSE_FILENAME
    elif what == "file":
        path = stack(root, content=None)
        path.symlink_to(elsewhere / cf.COMPOSE_FILENAME)
    else:
        other_root = tmp_path / "linked-stacks"
        other_root.symlink_to(root)
        path = other_root / "media" / cf.COMPOSE_FILENAME
        (root / "media").mkdir()
        return _refuses(other_root, path)
    _refuses(root, path)


def _refuses(root, path):
    with pytest.raises(cf.ComposeWriteError, match="symlink"):
        write(root, path, NEW, expect_absent=True)


def test_a_path_outside_a_stack_directory_is_refused(root):
    with pytest.raises(cf.ComposeWriteError, match="not a stack directory"):
        write(root, root / "media" / "deeper" / cf.COMPOSE_FILENAME, NEW, expect_absent=True)
    with pytest.raises(cf.ComposeWriteError, match="not docker-compose.yml"):
        write(root, root / "media" / ".env", NEW, expect_absent=True)


def test_content_larger_than_a_compose_file_is_refused(root):
    path = stack(root)
    with pytest.raises(cf.ComposeWriteError, match="larger than"):
        write(root, path, "x" * (cf.MAX_COMPOSE_BYTES + 1), expected_sha256=cf.sha256_text(OLD))
    assert path.read_text() == OLD


# ── the inverse ─────────────────────────────────────────────────────────────────
def test_restoring_an_edit_puts_the_previous_content_back(root):
    path = stack(root)
    record = write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    message = cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert path.read_text() == OLD and "restored" in message


def test_restoring_a_created_file_removes_it_and_its_directory(root):
    path = root / "brand-new" / cf.COMPOSE_FILENAME
    record = write(root, path, NEW, expect_absent=True)
    message = cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert not path.exists() and not path.parent.exists()
    assert "removed" in message and "directory" in message


def test_a_directory_that_is_not_empty_is_kept(root):
    path = root / "brand-new" / cf.COMPOSE_FILENAME
    record = write(root, path, NEW, expect_absent=True)
    (path.parent / ".env").write_text("SECRET=1\n")
    cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert not path.exists() and path.parent.exists()      # someone else's file lives there


def test_a_directory_that_already_existed_is_never_removed(root):
    (root / "brand-new").mkdir()
    path = root / "brand-new" / cf.COMPOSE_FILENAME
    record = write(root, path, NEW, expect_absent=True)
    assert not record.created_directory
    cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert path.parent.exists()


def test_a_file_edited_since_the_step_wrote_it_is_never_reverted(root):
    path = stack(root)
    record = write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    path.write_text(NEW + "# a human has been here\n")
    with pytest.raises(cf.ComposeWriteError, match="changed since the step wrote it"):
        cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert "a human has been here" in path.read_text()


def test_a_missing_backup_refuses_rather_than_guessing(root):
    path = stack(root)
    record = write(root, path, NEW, expected_sha256=cf.sha256_text(OLD))
    Path(record.backup_path).unlink()
    with pytest.raises(cf.ComposeWriteError, match="backup"):
        cf.restore(path, record, stacks_root=root, written_sha256=cf.sha256_text(NEW))
    assert path.read_text() == NEW
