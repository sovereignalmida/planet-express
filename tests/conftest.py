"""Host discovery (docker, mount table) must never run for real inside unrelated tests: the runner's default
discovery is replaced by a clean one. Tests of discovery itself call `discover_protected_inputs` with fakes or pass
`discovery=` explicitly."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_host_discovery(monkeypatch):
    from planet_express.missions import runner
    from planet_express.missions.discovery import DiscoveredInputs
    monkeypatch.setattr(runner, "discover_protected_inputs", lambda cfg, **kw: DiscoveredInputs())


@pytest.fixture(autouse=True)
def _host_is_not_just_rebooted(monkeypatch):
    """A test machine that booted a minute ago must not make a scheduler test wait for 'the stacks to settle'."""
    from planet_express.core import boot_settle
    monkeypatch.setattr(boot_settle, "uptime_seconds", lambda: float("inf"))
