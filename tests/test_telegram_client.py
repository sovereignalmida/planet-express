import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram_client import TelegramClient


def test_confirm_zero_offset(monkeypatch):
    tg = TelegramClient('token', '42')
    calls = []
    monkeypatch.setattr(tg, '_call', lambda *a, **kw: calls.append((a, kw)))
    tg.confirm_updates()
    assert calls == []


def test_confirm_current_offset(monkeypatch):
    tg = TelegramClient('token', '42')
    tg._offset = 123
    calls = []

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        return [{'update_id': 999}]

    monkeypatch.setattr(tg, '_call', call)
    tg.confirm_updates()
    assert calls == [(('getUpdates',), {'offset': 123, 'timeout': 0, 'limit': 1})]
    assert tg._offset == 123


def test_confirm_swallows_error(monkeypatch, caplog):
    tg = TelegramClient('token', '42')
    tg._offset = 123

    def fail(*args, **kwargs):
        raise RuntimeError('secret token')

    monkeypatch.setattr(tg, '_call', fail)
    tg.confirm_updates()
    assert 'Failed to confirm' in caplog.text
    assert 'secret token' not in caplog.text
