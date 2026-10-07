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


def test_zoidberg_compose_commands_follow_the_provider(monkeypatch):
    import casa_zoidberg

    seen = []
    monkeypatch.setattr(casa_zoidberg, "_run", lambda cmd, timeout=120: seen.append(cmd) or (0, "web", ""))
    monkeypatch.setattr(config, "HOST_CONTROL_PROVIDER", "mos")
    casa_zoidberg.stack_services(__import__("pathlib").Path("/stacks/media"))
    assert seen == ["docker-compose -f /stacks/media/docker-compose.yml config --services"]
