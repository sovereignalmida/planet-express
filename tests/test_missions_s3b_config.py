"""MissionsConfig additions for the worker/containment/runtime (S3b)."""

import pytest
from pydantic import ValidationError

from config_schema import MissionsConfig


def test_defaults_are_off_and_safe():
    c = MissionsConfig()
    assert c.enabled is False
    assert c.worker_user == "pe-worker"
    assert c.worker_socket == "/run/planetexpress/worker.sock"
    assert c.cgroup_root == "/sys/fs/cgroup/planetexpress-missions"
    assert c.runtime.argv == [] and c.runtime.env_allow == []
    assert c.budgets.wall_seconds > 0 and c.budgets.tool_calls > 0
    assert c.budgets.output_bytes > 0 and c.budgets.retries >= 0


def test_configured_values_round_trip():
    c = MissionsConfig(enabled=True, worker_user="agent", cgroup_root="/sys/fs/cgroup/x",
                       worker_socket="/run/pe/w.sock",
                       runtime={"argv": ["/usr/bin/agent", "--headless"], "env_allow": ["LANG"]},
                       budgets={"wall_seconds": 60, "tool_calls": 5, "output_bytes": 1000, "retries": 0})
    assert c.runtime.argv == ["/usr/bin/agent", "--headless"] and c.budgets.retries == 0


@pytest.mark.parametrize("kw", [
    {"runtime": {"argv": ["relative/agent"]}},
    {"runtime": {"argv": [""]}},
    {"runtime": {"argv": ["/bin/sh", "-c", "x\0y"]}},
    {"runtime": {"env_allow": ["PE_SESSION_TOKEN_FD"]}},
    {"runtime": {"env_allow": ["ANTHROPIC_API_KEY"]}},
    {"runtime": {"shell": "yes"}},
    {"worker_user": "root"},
    {"worker_user": "bad user"},
    {"worker_socket": "relative.sock"},
    {"cgroup_root": "relative/cg"},
    {"cgroup_root": "/sys/fs/cgroup"},
    {"budgets": {"wall_seconds": 0}},
    {"budgets": {"retries": -1}},
    {"mystery": 1},
])
def test_invalid_values_are_refused(kw):
    with pytest.raises(ValidationError):
        MissionsConfig(**kw)
