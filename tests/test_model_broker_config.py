"""missions.model_broker in the config schema."""
import pytest
from pydantic import ValidationError

from config_schema import MissionsConfig, ModelBrokerConfig


def test_off_by_default_and_a_config_without_it_still_loads():
    assert MissionsConfig().model_broker.enabled is False
    assert ModelBrokerConfig().upstream_host == "api.anthropic.com"


def test_enabled_needs_a_model_list():
    with pytest.raises(ValidationError, match="models"):
        ModelBrokerConfig(enabled=True)
    assert ModelBrokerConfig(enabled=True, models=["claude-sonnet-4-5"]).enabled


@pytest.mark.parametrize("host", ["api.example.com:0", "api.example.com:99999", "api.example.com:65536", "https://api.anthropic.com", "api.anthropic.com/v1", "user@host", "", "Host With Space", "-bad.example"])
def test_the_upstream_is_a_bare_host(host):
    with pytest.raises(ValidationError):
        ModelBrokerConfig(upstream_host=host)


def test_the_key_is_named_not_given():
    assert ModelBrokerConfig(key_env="MY_VENDOR_KEY").key_env == "MY_VENDOR_KEY"
    for bad in ("sk-ant-api03-realvalue-123", "PE_KEY", "has space", ""):
        with pytest.raises(ValidationError):
            ModelBrokerConfig(key_env=bad)


def test_model_ids_are_plain_and_unknown_keys_are_refused():
    with pytest.raises(ValidationError):
        ModelBrokerConfig(models=["ok", "bad model"])
    with pytest.raises(ValidationError):
        ModelBrokerConfig(api_key="sk-ant-literal")


def test_an_upstream_port_in_range_is_accepted():
    assert ModelBrokerConfig(upstream_host="gateway.example.com:8443").upstream_host == "gateway.example.com:8443"


def test_the_real_key_cannot_be_passed_through_env_allow():
    with pytest.raises(ValidationError, match="key_env"):
        MissionsConfig(model_broker={"enabled": True, "models": ["m"], "key_env": "MODEL_ACCESS"},
                       runtime={"argv": ["/usr/bin/agent"], "env_allow": ["MODEL_ACCESS"]})
    assert MissionsConfig(model_broker={"enabled": True, "models": ["m"]}, runtime={"env_allow": ["LANG"]}).model_broker.enabled


def test_an_openrouter_style_setup_is_accepted():
    cfg = ModelBrokerConfig(enabled=True, upstream_host="openrouter.ai", upstream_path_prefix="/api", auth_scheme="bearer",
                            key_env="OPENROUTER_API_KEY", models=["~anthropic/claude-sonnet-latest", "~anthropic/claude-haiku-latest"],
                            session_env={"ANTHROPIC_DEFAULT_SONNET_MODEL": "~anthropic/claude-sonnet-latest",
                                         "ANTHROPIC_DEFAULT_HAIKU_MODEL": "~anthropic/claude-haiku-latest"})
    assert cfg.auth_scheme == "bearer" and cfg.upstream_path_prefix == "/api"


@pytest.mark.parametrize("prefix", ["api", "/api/", "/a b", "/../x", "/api?x=1", "//api"])
def test_a_path_prefix_is_a_plain_path(prefix):
    with pytest.raises(ValidationError):
        ModelBrokerConfig(upstream_path_prefix=prefix)


@pytest.mark.parametrize("env", [{"PATH": "/tmp"}, {"ANTHROPIC_API_KEY": "x"}, {"ANTHROPIC_BASE_URL": "http://x"},
                                 {"ANTHROPIC_DEFAULT_SONNET_MODEL": "bad value"}, {"LD_PRELOAD": "x"}])
def test_session_env_takes_only_model_settings_with_model_shaped_values(env):
    with pytest.raises(ValidationError):
        ModelBrokerConfig(session_env=env)


def test_auth_scheme_is_one_of_two():
    with pytest.raises(ValidationError):
        ModelBrokerConfig(auth_scheme="basic")


def test_session_env_must_name_models_the_broker_will_allow():
    with pytest.raises(ValidationError, match="not in missions.model_broker.models"):
        ModelBrokerConfig(enabled=True, models=["a-model"], session_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "another-model"})
    assert ModelBrokerConfig(enabled=True, models=["a-model"], session_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "a-model"}).enabled


@pytest.mark.parametrize("model", ["a//b", "a/../b", "x[", "../x", "a/b/c/d", "-lead", "a" * 200])
def test_a_model_id_is_a_few_plain_segments(model):
    with pytest.raises(ValidationError):
        ModelBrokerConfig(models=[model])


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "anthropic/claude-sonnet-4.5", "~anthropic/claude-sonnet-latest",
                                   "~anthropic/claude-sonnet-latest[1m]"])
def test_the_documented_model_ids_are_accepted(model):
    assert ModelBrokerConfig(models=[model]).models == [model]


def test_the_session_environment_stays_inside_the_launchers_cap():
    names = [f"V{i}" for i in range(21)]
    with pytest.raises(ValidationError, match="20 variables"):
        MissionsConfig(model_broker={"enabled": True, "models": ["m"]}, runtime={"argv": ["/usr/bin/a"], "env_allow": names})
