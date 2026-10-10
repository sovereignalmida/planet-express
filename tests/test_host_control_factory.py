"""Tests for HostControlProvider factory and config wiring (Phase 3b-1)."""

import pytest

from planet_express.core.host_control import get_host_control_provider
from planet_express.execution.host_control_mos import MosHostControlProvider
from planet_express.execution.host_control_systemd import SystemdHostControlProvider


class TestHostControlProviderFactory:
    """Test get_host_control_provider() factory function."""

    def test_factory_returns_systemd_provider(self):
        """Factory returns SystemdHostControlProvider for 'systemd'."""
        provider = get_host_control_provider("systemd")
        assert isinstance(provider, SystemdHostControlProvider)

    def test_factory_returns_mos_provider(self):
        """Factory returns MosHostControlProvider for 'mos'."""
        provider = get_host_control_provider("mos")
        assert isinstance(provider, MosHostControlProvider)

    def test_factory_rejects_unknown_provider(self):
        """Factory raises ValueError for unknown provider type."""
        with pytest.raises(ValueError, match="Unknown host control provider: unknown"):
            get_host_control_provider("unknown")

    def test_factory_rejects_none(self):
        """Factory raises ValueError for None provider type."""
        with pytest.raises(ValueError, match="Unknown host control provider"):
            get_host_control_provider(None)  # type: ignore

    def test_factory_case_sensitive(self):
        """Factory is case-sensitive (rejects 'Systemd', 'MOS', etc.)."""
        with pytest.raises(ValueError, match="Unknown host control provider: Systemd"):
            get_host_control_provider("Systemd")
        with pytest.raises(ValueError, match="Unknown host control provider: MOS"):
            get_host_control_provider("MOS")


class TestConfigWiring:
    """Test that config.get_host_control() is properly wired (integration test)."""

    def test_config_imports_without_error(self):
        """config module imports without error, and get_host_control() is available."""
        import config
        # This will raise if config has import errors
        assert hasattr(config, "get_host_control")
        assert callable(config.get_host_control)

    def test_config_get_host_control_returns_provider(self):
        """config.get_host_control() returns a valid HostControlProvider instance."""
        import config
        provider = config.get_host_control()
        # Check that it has the expected methods from the Protocol
        assert hasattr(provider, "is_service_running")
        assert hasattr(provider, "start_service")
        assert hasattr(provider, "stop_service")
        assert hasattr(provider, "restart_service")
        assert hasattr(provider, "get_host_logs")
        assert hasattr(provider, "get_uptime_seconds")
        assert hasattr(provider, "get_metrics")
        assert hasattr(provider, "reboot")
        assert hasattr(provider, "shutdown")

    def test_config_provider_type_matches_setting(self):
        """config.get_host_control() type matches config.HOST_CONTROL_PROVIDER setting."""
        import config
        provider = config.get_host_control()
        if config.HOST_CONTROL_PROVIDER == "systemd":
            assert isinstance(provider, SystemdHostControlProvider)
        elif config.HOST_CONTROL_PROVIDER == "mos":
            assert isinstance(provider, MosHostControlProvider)
        else:
            pytest.fail(f"Unknown HOST_CONTROL_PROVIDER: {config.HOST_CONTROL_PROVIDER}")

    def test_config_get_host_control_caches_instance(self):
        """config.get_host_control() returns the same cached instance on multiple calls."""
        import config
        provider1 = config.get_host_control()
        provider2 = config.get_host_control()
        # Should be the exact same object (cached)
        assert provider1 is provider2
