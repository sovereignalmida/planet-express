"""`missions.verify.*` configuration (slice 8): safe defaults, validated."""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_schema import MissionsConfig, VerifyConfig


def test_defaults_are_safe():
    cfg = MissionsConfig()
    assert cfg.enabled is False
    v = cfg.verify
    assert v.exec_allowlist == [] and v.check_timeout_cap_s == 60 and v.scan_timeout_s == 120
    assert v.max_retries == 2 and v.max_output_bytes == 64 * 1024


@pytest.mark.parametrize("bad", [
    {"check_timeout_cap_s": 61}, {"check_timeout_cap_s": 0}, {"scan_timeout_s": 5}, {"max_retries": 4},
    {"max_output_bytes": 10}, {"exec_allowlist": ["/bin/sh"]}, {"exec_allowlist": ["bash"]},
    {"exec_allowlist": ["env"]}, {"exec_allowlist": ["curl -s"]}, {"exec_allowlist": ["-x"]}, {"exec_allowlist": [""]},
    {"exec_allowlist": ["a\0b"]}, {"unknown_key": 1},
])
def test_bad_values_are_refused(bad):
    with pytest.raises(ValidationError):
        VerifyConfig(**bad)


def test_the_verifier_is_built_from_the_config_and_follows_the_missions_switch():
    from planet_express.missions.verify.runner import verifier_from_config
    cfg = MissionsConfig(verify={"exec_allowlist": ["/usr/bin/curl"], "check_timeout_cap_s": 20, "max_retries": 1})
    v = verifier_from_config(cfg, missions=None, checkpoint=None, scan=lambda: {})
    assert v.env.exec_allowlist == frozenset({"/usr/bin/curl"}) and v.env.timeout_cap_s == 20 and v.env.max_retries == 1
    assert v.enabled is False                                    # missions are off by default: nothing runs
    empty = verifier_from_config(MissionsConfig(), missions=None, checkpoint=None, scan=lambda: {})
    assert empty.env.exec_allowlist == frozenset()


def test_an_allowlist_of_plain_binaries_is_accepted():
    assert VerifyConfig(exec_allowlist=["/usr/bin/curl", "wget", "pg_isready"]).exec_allowlist[0] == "/usr/bin/curl"
