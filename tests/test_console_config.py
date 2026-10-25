import pytest
from config_schema import MissionConsoleConfig, MissionsConfig

HOOKS = {"install_claude_code": True, "command": ["/usr/bin/python3", "/x/pe_hook.py"], "root": "/var/lib/pe/hooks"}
BROKER = {"enabled": True, "upstream_host": "openrouter.ai", "models": ["m"]}


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
