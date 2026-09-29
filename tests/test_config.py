import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_io
from config_io import (
    ConfigError,
    load_config_file,
    validate_config_text,
    write_config_text,
)

ROOT = Path(__file__).resolve().parent.parent
DRAFT = '# Keep café and ordering\r\nstacks_root: /srv/stacks\r\n'


def test_example():
    model, errors = validate_config_text((ROOT / 'config.example.yaml').read_text())
    assert model is not None
    assert errors == []


@pytest.mark.parametrize(('text', 'loc'), [
    ('stacks_root: /srv\nforbidden_stack: []', 'forbidden_stack'),
    ('stacks_root: /srv\nsudo_allowlist:\n  units:\n  - unit: x\n    actions: [42]',
     'sudo_allowlist.units.0.actions.0'),
    ('stacks_root: 2026-99-99', ''),
    ('[', ''), ('[]', ''), ('false', ''), ('', 'stacks_root'),
])
def test_invalid(text, loc):
    model, errors = validate_config_text(text)
    assert model is None
    assert errors[0]['loc'] == loc
    assert errors[0]['msg']


def test_write_preserves_text_and_permissions(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('old')
    path.chmod(0o600)
    assert write_config_text(DRAFT, path).stacks_root == Path('/srv/stacks')
    assert path.read_bytes() == DRAFT.encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_new_file_permissions_and_fsync(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    calls = []
    real = os.fsync

    def spy(fd):
        calls.append(os.fstat(fd).st_mode)
        real(fd)

    monkeypatch.setattr(config_io.os, 'fsync', spy)
    write_config_text(DRAFT, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert len(calls) == 2
    assert stat.S_ISREG(calls[0])
    assert stat.S_ISDIR(calls[1])


def test_invalid_write_untouched(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_bytes(b'original')
    before = path.stat().st_mtime_ns
    with pytest.raises(ConfigError):
        write_config_text('[]', path)
    assert path.read_bytes() == b'original'
    assert path.stat().st_mtime_ns == before


def test_replace_failure_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_bytes(b'original')

    def fail(*args):
        raise OSError('replace failed')

    monkeypatch.setattr(config_io.os, 'replace', fail)
    with pytest.raises(OSError, match='replace failed'):
        write_config_text(DRAFT, path)
    assert path.read_bytes() == b'original'
    assert list(tmp_path.glob('.config.yaml.*')) == []


@pytest.mark.parametrize('content', [None, '[]'])
def test_load_errors(tmp_path, content):
    path = tmp_path / 'config.yaml'
    if content is not None:
        path.write_text(content)
    with pytest.raises(ConfigError) as caught:
        load_config_file(path)
    assert caught.value.errors[0]['loc'] == ''


@pytest.mark.parametrize(('content', 'message'), [
    (None, 'Config file not found:'), ('[]', 'Invalid config at'),
])
def test_runtime_load_errors(tmp_path, monkeypatch, content, message):
    import config
    path = tmp_path / 'config.yaml'
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(config, 'CONFIG_FILE', path)
    with pytest.raises(SystemExit, match=message):
        config._load_config()


def test_import_without_config(tmp_path):
    result = subprocess.run(
        [sys.executable, '-c',
         ('import sys, config_io; assert "config" not in sys.modules; '
         'assert config_io.validate_config_text("stacks_root: /srv")[1] == []')],
        cwd=ROOT, env={**os.environ, 'CASA_CONFIG': str(tmp_path / 'missing')},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_write_preserves_named_user_access_acl(tmp_path):
    import shutil

    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("setfacl/getfacl not installed")
    path = tmp_path / "config.yaml"
    path.write_text(DRAFT)
    path.chmod(0o640)
    if subprocess.run(["setfacl", "-m", "u:nobody:r", str(path)], check=False).returncode != 0:
        pytest.skip("filesystem does not support ACLs")

    config_io.write_config_text(DRAFT + "\n# edited\n", path)

    acl = subprocess.run(["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True).stdout
    assert "user:nobody:r--" in acl
    assert "# edited" in path.read_text()


def test_write_without_acl_support_is_a_no_op(tmp_path, monkeypatch):
    import errno as errno_mod

    path = tmp_path / "config.yaml"
    path.write_text(DRAFT)

    def no_acl(*args, **kwargs):
        raise OSError(errno_mod.ENOTSUP, "not supported")

    monkeypatch.setattr(config_io.os, "getxattr", no_acl)
    config_io.write_config_text(DRAFT + "\n# edited\n", path)
    assert "# edited" in path.read_text()


def test_failure_to_copy_an_existing_acl_leaves_the_config_intact(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(DRAFT)
    before = path.read_bytes()
    monkeypatch.setattr(config_io.os, "getxattr", lambda *a, **k: b"acl-bytes")

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(config_io.os, "setxattr", denied)
    with pytest.raises(PermissionError):
        config_io.write_config_text(DRAFT + "\n# edited\n", path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".config.yaml.*"))


def test_autonomy_defaults():
    from config_schema import PlanetExpressConfig

    autonomy = PlanetExpressConfig(stacks_root='/srv').autonomy
    assert autonomy.model_dump() == {
        'direct_request_risks': ['R1'], 'forbidden_risks': ['R4'],
        # Empty by default, so every existing config keeps the single shared ceiling and
        # nothing changes for either surface until an override is written down.
        'direct_request_risks_by_origin': {},
        'cooldown_seconds': 1800, 'max_attempts_per_day': 3,
    }
    # The dashboard's built-in default (T47): its stack controls need R2 to exist at all, and
    # what makes that safe is the elevated session, not a lower ceiling. Telegram keeps the
    # shared R1, because a chat message cannot prove a passphrase.
    assert autonomy.direct_risks_for('dashboard-direct') == ['R1', 'R2', 'R3']
    assert autonomy.direct_risks_for('telegram-direct') == ['R1']
    # The shared list governs the surfaces with no default of their own -- Telegram today.
    from config_schema import AutonomyConfig
    assert AutonomyConfig(direct_request_risks=[]).direct_risks_for('telegram-direct') == []
    # Changing what the dashboard may reach is written down per origin, so a config diff can
    # see it. See test_two_configs_that_dump_alike_grant_alike for why that matters.
    assert AutonomyConfig(
        direct_request_risks_by_origin={'dashboard-direct': ['R1']}
    ).direct_risks_for('dashboard-direct') == ['R1']


@pytest.mark.parametrize('settings', [
    {'forbidden_risks': []}, {'forbidden_risks': ['R0', 'R4']},
    {'direct_request_risks': ['R0']},
    {'forbidden_risks': ['R1', 'R4'], 'direct_request_risks': ['R1']},
    {'cooldown_seconds': -1}, {'max_attempts_per_day': 0},
    {'automatic_risks': ['R0']}, {'direct_request_risks': ['R9']},
])
def test_invalid_autonomy(settings):
    from pydantic import ValidationError

    from config_schema import PlanetExpressConfig

    with pytest.raises(ValidationError):
        PlanetExpressConfig(stacks_root='/srv', autonomy=settings)


def test_backup_jobs_defaults():
    from config_schema import PlanetExpressConfig

    assert PlanetExpressConfig(stacks_root='/srv').backup_jobs == ['daily', 'weekly']


def test_backup_jobs_weekly():
    model, errors = validate_config_text('stacks_root: /srv\nbackup_jobs: [weekly]')
    assert errors == []
    assert model.backup_jobs == ['weekly']


@pytest.mark.parametrize('jobs', ['[]', '[daily, daily]', '[monthly]'])
def test_invalid_backup_jobs(jobs):
    model, errors = validate_config_text(f'stacks_root: /srv\nbackup_jobs: {jobs}')
    assert model is None
    assert errors[0]['loc'].startswith('backup_jobs')


def test_config_sha256_is_the_loaded_bytes(tmp_path, monkeypatch):
    # T35: the running process reports which config it loaded; the file may be newer.
    import hashlib

    import config
    path = tmp_path / 'config.yaml'
    path.write_text('# a comment counts too\nstacks_root: /srv/stacks\n')
    monkeypatch.setattr(config, 'CONFIG_FILE', path)
    _cfg, digest = config._load_config()
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_expected_sha_rechecked_just_before_replace(tmp_path, monkeypatch):
    # Codex review round 3, T35: a host edit landing while the draft was validated and written
    # must not be overwritten by a caller holding the older sha.
    import hashlib
    path = tmp_path / 'config.yaml'
    path.write_text(DRAFT)
    loaded = hashlib.sha256(path.read_bytes()).hexdigest()
    host_edit = (DRAFT + '# edited on the host\n').encode()
    real_copy = config_io._copy_access_acl

    def host_editor_writes(source, target):
        path.write_bytes(host_edit)
        real_copy(source, target)

    monkeypatch.setattr(config_io, '_copy_access_acl', host_editor_writes)
    with pytest.raises(config_io.ConfigConflict):
        write_config_text(DRAFT + '# dashboard draft\n', path, expected_sha256=loaded)
    assert path.read_bytes() == host_edit
    assert [p.name for p in tmp_path.iterdir()] == ['config.yaml']  # temp file cleaned up


# ── links: the launch-link escape hatch (T46.1) ──────────────────────────────────

def test_a_declared_launch_link_validates():
    model, errors = validate_config_text(
        'stacks_root: /srv\n'
        'links:\n'
        '  - name: adventurelog\n'
        '    href: https://travel.casalan.com\n'
        '    zone: lan\n'
    )
    assert errors == []
    assert model.links[0].name == 'adventurelog'
    assert model.links[0].zone == 'lan'


def test_a_launch_link_defaults_to_the_lan_zone():
    model, _ = validate_config_text(
        'stacks_root: /srv\nlinks:\n  - name: x\n    href: http://192.168.1.20:8123\n')
    assert model.links[0].zone == 'lan'


@pytest.mark.parametrize('href', [
    'javascript:alert(1)', 'data:text/html,<script>', 'file:///etc/passwd', 'ftp://host/x',
])
def test_a_launch_link_must_be_http(href):
    """This value becomes an href the operator clicks. A config file is not a reason to put
    javascript: behind a button."""
    model, errors = validate_config_text(
        f'stacks_root: /srv\nlinks:\n  - name: x\n    href: {href}\n')
    assert model is None
    assert errors[0]['loc'] == 'links.0.href'


def test_an_unknown_launch_link_key_is_refused():
    model, errors = validate_config_text(
        'stacks_root: /srv\nlinks:\n  - name: x\n    href: https://a\n    targett: _blank\n')
    assert model is None
    assert 'links.0.targett' == errors[0]['loc']


def test_a_forbidden_risk_never_arrives_through_the_default_ceiling():
    """The validator refuses a hand-written ceiling that names a forbidden risk. The built-in
    dashboard default is not hand-written, so it gets the same rule applied to it rather than
    a free pass -- otherwise forbidding R3 would forbid it everywhere except the one place
    nobody had to type.
    """
    from config_schema import AutonomyConfig

    strict = AutonomyConfig(forbidden_risks=["R3", "R4"])
    assert strict.direct_risks_for("dashboard-direct") == ["R1", "R2"]

    # And a hand-written one still cannot say it at all.
    with pytest.raises(ValueError, match="both forbidden and directly requestable"):
        AutonomyConfig(forbidden_risks=["R3", "R4"],
                       direct_request_risks_by_origin={"dashboard-direct": ["R3"]})


def test_two_configs_that_dump_alike_grant_alike():
    """No policy may depend on whether a key was written down.

    Config changes are compared by dumped value, so a ceiling that consulted
    `model_fields_set` could be raised by DELETING a line: identical dumps, no changed field,
    no lock, no passphrase -- an escalation that shows up nowhere. Whatever governs the
    ceiling has to be visible in the values themselves.
    """
    import yaml

    from config_schema import AutonomyConfig

    written = AutonomyConfig(**yaml.safe_load("direct_request_risks: [R1]"))
    defaulted = AutonomyConfig()
    assert written.model_dump() == defaulted.model_dump()
    for origin in ("dashboard-direct", "telegram-direct"):
        assert written.direct_risks_for(origin) == defaulted.direct_risks_for(origin)


def test_lowering_the_dashboard_is_something_you_write_down():
    """The escape hatch, and it is value-visible: a config diff sees it, so it is lockable and
    needs the passphrase like any other autonomy change."""
    from config_schema import AutonomyConfig

    clamped = AutonomyConfig(direct_request_risks_by_origin={"dashboard-direct": ["R1"]})
    assert clamped.direct_risks_for("dashboard-direct") == ["R1"]
    assert clamped.model_dump() != AutonomyConfig().model_dump()
