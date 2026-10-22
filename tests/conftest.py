"""Host discovery (docker, mount table) must never run for real inside unrelated tests: the runner's default
discovery is replaced by a clean one. Tests of discovery itself call `discover_protected_inputs` with fakes or pass
`discovery=` explicitly."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_host_discovery(monkeypatch):
    from planet_express.missions import runner
    from planet_express.missions.discovery import DiscoveredInputs
    monkeypatch.setattr(runner, "discover_protected_inputs", lambda cfg, **kw: DiscoveredInputs())
