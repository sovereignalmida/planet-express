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
