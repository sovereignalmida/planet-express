import config
from planet_express.execution import actions


def test_compose_argv_systemd_uses_the_plugin(monkeypatch):
    monkeypatch.setattr(config, "HOST_CONTROL_PROVIDER", "systemd")
    assert config.compose_argv() == ["docker", "compose"]


def test_compose_argv_mos_uses_the_standalone_binary(monkeypatch):
    monkeypatch.setattr(config, "HOST_CONTROL_PROVIDER", "mos")
    assert config.compose_argv() == ["docker-compose"]


def test_step_argv_follows_the_provider(monkeypatch):
    monkeypatch.setattr(config, "HOST_CONTROL_PROVIDER", "mos")
    assert actions.compose_pull_argv("media", "web")[0] == "docker-compose"
