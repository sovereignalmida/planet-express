import hashlib
import json
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import state_snapshot as snapshot

# The `state` fixture replaces subprocess.run; ACL setup needs the real one.
_REAL_RUN = subprocess.run


@pytest.fixture
def state(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.yaml"
    config.write_bytes(b"newer_invalid_field: yes\r\n")
    config.chmod(0o640)
    monkeypatch.setenv("CASA_DATA_DIR", str(data))
    monkeypatch.setenv("CASA_CONFIG", str(config))

    def run(command, **kwargs):
        if command[0] == "git":
            assert command == ["git", "describe", "--tags", "--always", "--dirty"]
            assert kwargs["cwd"] == snapshot.REPO
            return SimpleNamespace(returncode=0, stdout="v2-test-dirty\n")
        assert command == ["systemctl", "show", "--property=ActiveState", "--value", "casa-planetexpress"]
        return SimpleNamespace(returncode=0, stdout="inactive\n")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    return data, config


def create(capsys, label=None):
    assert snapshot.main(["create", *(["--label", label] if label else [])]) == 0
    return Path(capsys.readouterr().out.strip().splitlines()[-1])


def database(path, value="original"):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES (?)", (value,))
        conn.execute("PRAGMA user_version = 123")


def test_create_live_wal(state, capsys):
    data, config = state
    db = data / "planetexpress.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('still in WAL')")
        conn.execute("PRAGMA user_version = 123")
        conn.commit()
        assert Path(str(db) + "-wal").stat().st_size > 0
        directory = create(capsys, "pre-upgrade")
        with sqlite3.connect(directory / db.name) as copied:
            assert copied.execute("SELECT * FROM sample").fetchall() == [("still in WAL",)]
        assert (directory / "config.yaml").read_bytes() == config.read_bytes()
        assert (directory / "config.yaml").stat().st_mode & 0o777 == 0o640
        assert directory.stat().st_mode & 0o777 == 0o700
        assert directory.parent.stat().st_mode & 0o777 == 0o700
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["schema_version"] == 123
        assert manifest["git_describe"] == "v2-test-dirty"
        assert manifest["label"] == "pre-upgrade" and manifest["created_at"]
        assert manifest["source_paths"] == {db.name: str(db), config.name: str(config)}
        for name, checksum in manifest["sha256"].items():
            assert checksum == hashlib.sha256((directory / name).read_bytes()).hexdigest()
    finally:
        conn.close()


@pytest.mark.parametrize("missing", ["db", "config", "both"])
def test_missing_files(state, capsys, missing):
    data, config = state
    if missing in ("config", "both"):
        config.unlink()
    if missing == "config":
        database(data / "planetexpress.db")
    directory = create(capsys)
    manifest = json.loads((directory / "manifest.json").read_text())
    for name in ("planetexpress.db", "config.yaml"):
        assert (manifest["sha256"][name] is None) == (not (directory / name).exists())
    assert manifest["schema_version"] == (123 if missing == "config" else None)


def test_list_newest_first_and_collision(state, capsys):
    first = create(capsys)
    second = create(capsys)
    assert first != second
    assert snapshot.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == [second.name, first.name]
    assert all("None\tv2-test-dirty" in line for line in lines)


@pytest.mark.parametrize("refusal", ["confirmation", "active", "missing-systemctl", "tampered"])
def test_restore_refuses_without_changes(state, capsys, monkeypatch, refusal):
    data, config = state
    database(data / "planetexpress.db")
    directory = create(capsys)
    config.write_bytes(b"current")
    before = {p: p.read_bytes() for p in (config, data / "planetexpress.db")}
    args = ["restore", str(directory), "--yes"]
    if refusal == "confirmation":
        args.pop()
    elif refusal == "active":
        monkeypatch.setattr(snapshot.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0, stdout="active\n"))
        args.append("--no-service-check")  # An active service cannot be bypassed.
    elif refusal == "missing-systemctl":
        def missing(*args, **kwargs):
            raise FileNotFoundError("systemctl")
        monkeypatch.setattr(snapshot.subprocess, "run", missing)
    else:
        (directory / "config.yaml").write_bytes(b"tampered")
    assert snapshot.main(args) == 1
    assert capsys.readouterr().err
    assert all(p.read_bytes() == content for p, content in before.items())
    assert list((data / "snapshots").iterdir()) == [directory]


@pytest.mark.parametrize("missing_systemctl", [False, True])
def test_restore_success(state, capsys, monkeypatch, missing_systemctl):
    data, config = state
    db = data / "planetexpress.db"
    database(db)
    directory = create(capsys)
    config.write_bytes(b"current")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sample SET value = 'current'")
    original_create = snapshot.create

    def pre_restore(label):
        result = original_create(label)
        # Emulate sidecars remaining after backup; restore must remove them.
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).write_bytes(b"stale")
        return result

    monkeypatch.setattr(snapshot, "create", pre_restore)
    if missing_systemctl:
        def missing(*args, **kwargs):
            raise FileNotFoundError("systemctl")
        monkeypatch.setattr(snapshot.subprocess, "run", missing)
    assert snapshot.main(["restore", directory.name, "--yes", "--no-service-check"]) == 0
    assert "Restored" in capsys.readouterr().out
    assert db.read_bytes() == (directory / db.name).read_bytes()
    assert config.read_bytes() == (directory / config.name).read_bytes()
    assert config.stat().st_mode & 0o777 == 0o640
    assert all(not Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm"))
    backups = list((data / "snapshots").glob("*-pre-restore"))
    assert len(backups) == 1
    assert (backups[0] / config.name).read_bytes() == b"current"
    with sqlite3.connect(backups[0] / db.name) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "current"


def test_replace_failure_keeps_target_intact(state, capsys, monkeypatch):
    data, config = state
    database(data / "planetexpress.db")
    directory = create(capsys)
    config.write_bytes(b"current config")
    replace = snapshot.os.replace

    def fail_config(source, target):
        if target == config:
            raise OSError("replacement failed")
        replace(source, target)

    monkeypatch.setattr(snapshot.os, "replace", fail_config)
    assert snapshot.main(["restore", str(directory), "--yes"]) == 1
    assert "replacement failed" in capsys.readouterr().err
    assert config.read_bytes() == b"current config"
    assert not list(config.parent.glob(".config.yaml.*"))
    assert list((data / "snapshots").glob("*-pre-restore"))


def test_git_failure_is_recorded_as_null(state, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(snapshot.subprocess, "run", fail)
    directory = create(capsys)
    assert json.loads((directory / "manifest.json").read_text())["git_describe"] is None


def test_deploy_snapshot_order_and_syntax():
    deploy = snapshot.REPO / "deploy.sh"
    script = deploy.read_text()
    command = "venv/bin/python scripts/state_snapshot.py create --label pre-deploy"
    assert script.index('export CASA_CONFIG="$config_path"') < script.index(command)
    assert script.index(command) < script.index("venv/bin/python scripts/setup_wizard.py")
    assert '|| error "Pre-deploy snapshot failed' in script
    subprocess.run(["bash", "-n", str(deploy)], check=True)


def test_list_labels_do_not_override_creation_order(state, capsys):
    first = create(capsys, "z-first")
    second = create(capsys, "a-second")
    assert snapshot.main(["list"]) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == [second.name, first.name]


def test_restore_keeps_the_live_config_access_acl(state, capsys):
    import shutil

    _, config = state
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("setfacl/getfacl not installed")
    directory = create(capsys)
    if _REAL_RUN(["setfacl", "-m", "u:nobody:r", str(config)], check=False).returncode:
        pytest.skip("filesystem does not support ACLs")

    assert snapshot.main(["restore", str(directory), "--yes"]) == 0

    acl = _REAL_RUN(["getfacl", "-cp", str(config)], capture_output=True, text=True, check=True).stdout
    assert "user:nobody:r--" in acl


@pytest.mark.parametrize(("result", "refused"), [
    (SimpleNamespace(returncode=0, stdout="deactivating\n"), True),
    (SimpleNamespace(returncode=0, stdout="activating\n"), True),
    (SimpleNamespace(returncode=1, stdout=""), True),        # e.g. no system bus
    (SimpleNamespace(returncode=0, stdout="failed\n"), False),
])
def test_restore_requires_a_confirmed_stopped_core(state, capsys, monkeypatch, result, refused):
    data, config = state
    database(data / "planetexpress.db")
    directory = create(capsys)
    config.write_bytes(b"current")
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *a, **k: result)
    assert snapshot.main(["restore", str(directory), "--yes"]) == (1 if refused else 0)
    assert (config.read_bytes() == b"current") is refused


def test_transitional_state_is_refused_even_with_no_service_check(state, capsys, monkeypatch):
    _, config = state
    directory = create(capsys)
    config.write_bytes(b"current")
    monkeypatch.setattr(snapshot.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="deactivating\n"))
    assert snapshot.main(["restore", str(directory), "--yes", "--no-service-check"]) == 1
    assert config.read_bytes() == b"current"


def test_restore_removes_a_database_the_snapshot_recorded_as_absent(state, capsys):
    data, _ = state
    directory = create(capsys)                      # no database yet
    db = data / "planetexpress.db"
    database(db)
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).write_bytes(b"stale")
    assert snapshot.main(["restore", str(directory), "--yes"]) == 0
    assert not db.exists()
    assert all(not Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm"))
    backups = list((data / "snapshots").glob("*-pre-restore"))
    assert len(backups) == 1 and (backups[0] / db.name).exists()


def test_restore_refuses_when_the_snapshot_has_no_config_but_one_exists(state, capsys):
    data, config = state
    config.unlink()
    database(data / "planetexpress.db")
    directory = create(capsys)
    config.write_bytes(b"current")
    before = (data / "planetexpress.db").read_bytes()
    assert snapshot.main(["restore", str(directory), "--yes"]) == 1
    assert "another path" in capsys.readouterr().err
    assert config.read_bytes() == b"current"
    assert (data / "planetexpress.db").read_bytes() == before


def test_script_imports_nothing_from_the_repository():
    import ast

    tree = ast.parse(Path(snapshot.__file__).read_text())
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    repo_modules = {p.stem for p in Path(snapshot.REPO).glob("*.py")} | {"planet_express", "scripts"}
    assert not modules & repo_modules, modules & repo_modules
    assert "sys.path.insert" not in Path(snapshot.__file__).read_text()


def test_unwritable_config_directory_refuses_before_changing_anything(state, capsys, monkeypatch):
    data, config = state
    database(data / "planetexpress.db")
    directory = create(capsys)
    config.write_bytes(b"current")
    before = (data / "planetexpress.db").read_bytes()
    real_access = snapshot.os.access
    monkeypatch.setattr(snapshot.os, "access",
                        lambda path, mode: False if Path(path) == config.parent else real_access(path, mode))
    assert snapshot.main(["restore", str(directory), "--yes"]) == 1
    err = capsys.readouterr().err
    assert "--skip-config" in err and f"sudo cp --no-preserve=all -- {shlex.quote(str(directory / 'config.yaml'))} {shlex.quote(str(config))}" in err
    assert config.read_bytes() == b"current"
    assert (data / "planetexpress.db").read_bytes() == before
    assert list((data / "snapshots").iterdir()) == [directory]      # no pre-restore taken


def test_skip_config_restores_only_the_database_and_prints_the_config_step(state, capsys, monkeypatch):
    data, config = state
    db = data / "planetexpress.db"
    database(db)
    directory = create(capsys)
    config.write_bytes(b"current")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sample SET value = 'current'")
    monkeypatch.setattr(snapshot.os, "access", lambda path, mode: False)
    assert snapshot.main(["restore", str(directory), "--yes", "--skip-config"]) == 0
    out = capsys.readouterr().out
    assert db.read_bytes() == (directory / db.name).read_bytes()
    assert config.read_bytes() == b"current"
    assert f"sudo cp --no-preserve=all -- {shlex.quote(str(directory / 'config.yaml'))} {shlex.quote(str(config))}" in out


def test_printed_config_command_is_shell_safe(tmp_path, capsys, monkeypatch):
    import shutil

    root = tmp_path / "odd & dir; $(echo x)"
    data = root / "data"
    data.mkdir(parents=True)
    config = root / "config.yaml"
    config.write_bytes(b"snapshot")
    monkeypatch.setenv("CASA_DATA_DIR", str(data))
    monkeypatch.setenv("CASA_CONFIG", str(config))
    monkeypatch.setattr(snapshot.subprocess, "run", lambda command, **k: SimpleNamespace(
        returncode=0, stdout="inactive\n" if command[0] == "systemctl" else "v\n"))
    directory = create(capsys)
    config.write_bytes(b"current")
    monkeypatch.setattr(snapshot.os, "access", lambda path, mode: False)
    assert snapshot.main(["restore", str(directory), "--yes", "--skip-config"]) == 0
    command = capsys.readouterr().out.strip().splitlines()[-1].strip()
    argv = shlex.split(command)
    assert argv[:4] == ["sudo", "cp", "--no-preserve=all", "--"]
    # Run the unprivileged part of the printed command exactly as a shell would parse it.
    monkeypatch.undo()          # restores os.access, which shutil.which and cp lookup rely on
    assert shutil.which("cp")
    assert _REAL_RUN(argv[1:], check=False).returncode == 0
    assert config.read_bytes() == b"snapshot"
