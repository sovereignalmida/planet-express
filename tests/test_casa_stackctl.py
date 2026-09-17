"""Backup reporting respects the configured jobs."""
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import casa_farnsworth
import casa_stackctl
import config


@pytest.mark.parametrize('jobs', [['weekly'], ['weekly', 'daily'], ['daily', 'weekly']])
def test_enabled_borg_jobs(monkeypatch, jobs):
    monkeypatch.setattr(config, 'BACKUP_JOBS', jobs)
    enabled = casa_stackctl.enabled_borg_jobs()
    assert list(enabled) == jobs
    assert all(enabled[name] == casa_stackctl.BORG_JOBS[name] for name in jobs)


def test_backup_reports_only_enabled_jobs(monkeypatch, capsys):
    monkeypatch.setattr(config, 'BACKUP_JOBS', ['weekly'])
    show = Mock(return_value={'Result': 'success', 'ExecMainStatus': '0'})
    monkeypatch.setattr(casa_stackctl, '_systemctl_show', show)
    notifier = Mock()
    casa_farnsworth._run_backups_check(notifier)
    message = notifier.notify.call_args.args[0]
    assert 'weekly' in message
    assert 'daily' not in message
    assert casa_stackctl.cmd_backups() == 0
    output = capsys.readouterr().out
    assert 'weekly' in output
    assert 'daily' not in output
    assert {call.args[0] for call in show.call_args_list} == {
        'weekly-borg-backup.service', 'weekly-borg-backup.timer',
    }
