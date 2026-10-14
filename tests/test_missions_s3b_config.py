"""MissionsConfig additions for the worker/containment/runtime (S3b)."""

import pytest
from pydantic import ValidationError

import config_schema
from config_schema import MissionsConfig

CGROUP2 = "25 1 0:22 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n30 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


@pytest.fixture(autouse=True)
def mountinfo(tmp_path, monkeypatch):
    path = tmp_path / "mountinfo"
    path.write_text(CGROUP2)
    monkeypatch.setattr(config_schema, "MOUNTINFO_PATH", str(path))
    return path


def test_defaults_are_off_and_safe():
    c = MissionsConfig()
    assert c.enabled is False
    assert c.worker_user == "pe-worker"
    assert c.worker_socket == "/run/planetexpress/worker.sock"
    assert c.cgroup_root == "/sys/fs/cgroup/system.slice/casa-planetexpress.service/sessions"
    assert c.workspace_root is None
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


@pytest.mark.parametrize("root", ["/", "/sys", "/sys/fs", "/sys/fs/cgroup"])
def test_roots_that_are_not_strictly_below_the_mount_are_refused(root):
    with pytest.raises(ValidationError):
        MissionsConfig(cgroup_root=root)


def test_enabled_root_must_be_below_a_cgroup2_mount(mountinfo):
    MissionsConfig(enabled=True, cgroup_root="/sys/fs/cgroup/pe")
    with pytest.raises(ValidationError, match="cgroup v2"):
        MissionsConfig(enabled=True, cgroup_root="/var/lib/pe")
    mountinfo.write_text("30 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
    with pytest.raises(ValidationError, match="cgroup v2"):
        MissionsConfig(enabled=True)
    MissionsConfig(enabled=False, cgroup_root="/var/lib/pe")        # not checked while missions are off


def test_enabled_root_equal_to_the_mount_point_is_refused(mountinfo):
    mountinfo.write_text("25 1 0:22 / /srv/cg rw - cgroup2 cgroup2 rw\n")
    with pytest.raises(ValidationError, match="subdirectory"):
        MissionsConfig(enabled=True, cgroup_root="/srv/cg")
    MissionsConfig(enabled=True, cgroup_root="/srv/cg/pe")


def test_unreadable_mount_table_fails_clearly_when_enabled(mountinfo, tmp_path):
    config_schema.MOUNTINFO_PATH = str(tmp_path / "missing")
    with pytest.raises(ValidationError, match="mount table"):
        MissionsConfig(enabled=True)
