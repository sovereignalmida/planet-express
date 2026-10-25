import pytest

from config_schema import MissionConsoleConfig, MissionsConfig

HOOKS = {"install_claude_code": True, "command": ["/usr/bin/python3", "/x/pe_hook.py"], "root": "/var/lib/pe/hooks"}
BROKER = {"enabled": True, "upstream_host": "openrouter.ai", "models": ["~anthropic/claude-sonnet-latest"]}


def test_the_defaults_are_the_phase_zero_numbers_and_the_console_is_off():
    cfg = MissionConsoleConfig()
    assert not cfg.enabled and cfg.token_budget == 150_000 and cfg.max_open == 10 and cfg.archive_after_days == 3
    assert "Edit" in cfg.disallowed_tools and "Write" in cfg.tools and "Bash" in cfg.tools
    assert MissionsConfig().console.enabled is False


def test_enabling_it_needs_the_broker_and_the_forced_settings_hooks():
    with pytest.raises(ValueError, match="model_broker"):
        MissionsConfig(enabled=True, console={"enabled": True}, hooks=HOOKS)
    with pytest.raises(ValueError, match="install_claude_code"):
        MissionsConfig(enabled=True, console={"enabled": True}, model_broker=BROKER)
    assert MissionsConfig(enabled=True, console={"enabled": True}, model_broker=BROKER, hooks=HOOKS).console.enabled


@pytest.mark.parametrize("claude", ["claude", "/usr/bin/env", "/bin/sh", "/usr/local/bin/claude.sh", "/a/../b/claude", "/x\0/claude"])
def test_the_claude_path_must_be_the_binary_itself(claude):
    with pytest.raises(ValueError):
        MissionConsoleConfig(claude=claude)


@pytest.mark.parametrize("kwargs", [{"token_budget": 5}, {"max_open": 0}, {"max_open": 500}, {"archive_after_days": 0}, {"max_turns": 0},
                                    {"max_output_tokens": 10}, {"model": "bad model!"}, {"tools": ["Bash(rm -rf *)"]},
                                    {"disallowed_tools": [""]}, {"extra": 1}])
def test_out_of_bounds_values_and_unknown_keys_are_refused(kwargs):
    with pytest.raises(ValueError):
        MissionConsoleConfig(**kwargs)


def test_a_tool_cannot_be_both_allowed_and_disallowed():
    with pytest.raises(ValueError, match="overlap"):
        MissionsConfig(enabled=True, console={"enabled": True, "tools": ["Bash", "Edit"]}, model_broker=BROKER, hooks=HOOKS)


def test_the_runner_wires_the_console_only_when_it_is_enabled_and_a_console_mission_is_refused_when_it_is_off(tmp_path):
    from missions_helpers import make_store

    from planet_express.missions.containment import FakeContainment
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    base = {"enabled": True, "runtime": {"argv": ["/usr/local/bin/claude", "-p", "x"]}, "model_broker": BROKER, "hooks": {**HOOKS, "root": str(tmp_path / "h")}}
    off = build_mission_runner(store, MissionsConfig(**base), containment=FakeContainment())
    assert off.console is None and off.supervisor.console_tick is None and off.supervisor.turn_ended is None

    class Mission:
        runtime = "console"
    with pytest.raises(ValueError, match="Console is off"):
        off.supervisor.runtime_factory(Mission())
    on = build_mission_runner(store, MissionsConfig(**base, console={"enabled": True}), containment=FakeContainment())
    assert on.console is not None and on.supervisor.console_tick == on.console.tick and on.supervisor.turn_ended == on.console.turn_ended
    assert type(on.supervisor.runtime_factory(Mission())).__name__ == "ConsoleRuntime"

    class Plain:
        runtime = "command"
    assert type(on.supervisor.runtime_factory(Plain())).__name__ == "CommandRuntime"


def test_the_console_model_must_be_one_the_broker_allows_and_a_turn_must_be_able_to_start():
    with pytest.raises(ValueError, match="model_broker.models"):
        MissionsConfig(enabled=True, console={"enabled": True, "model": "other-model"}, model_broker=BROKER, hooks=HOOKS)
    with pytest.raises(ValueError, match="min_turn_tokens"):
        MissionsConfig(enabled=True, console={"enabled": True, "token_budget": 10_000, "min_turn_tokens": 20_000}, model_broker=BROKER, hooks=HOOKS)


def test_a_console_only_host_needs_no_mission_command(tmp_path):
    from missions_helpers import make_store

    from planet_express.missions.containment import FakeContainment
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    cfg = MissionsConfig(enabled=True, console={"enabled": True}, model_broker=BROKER, hooks={**HOOKS, "root": str(tmp_path / "h")})
    runner = build_mission_runner(store, cfg, containment=FakeContainment())
    assert runner is not None and runner.console is not None

    class Plain:
        runtime = "command"
    with pytest.raises(ValueError, match="no mission runtime"):
        runner.supervisor.runtime_factory(Plain())
    assert build_mission_runner(store, MissionsConfig(enabled=True), containment=FakeContainment()) is None     # no command, no Console: nothing
