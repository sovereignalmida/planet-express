"""Fixtures every test gets."""
import pytest


@pytest.fixture(autouse=True)
def _host_is_not_just_rebooted(monkeypatch):
    """A test machine that booted a minute ago must not make a scheduler test wait for 'the stacks to settle'."""
    from planet_express.core import boot_settle
    monkeypatch.setattr(boot_settle, "uptime_seconds", lambda: float("inf"))
