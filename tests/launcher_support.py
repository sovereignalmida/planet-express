"""Shared by the launcher tests: load the helper by path (it is a script, not a package member) and build specs."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "planet_express" / "missions" / "launcher" / "pe_launch.py"
IS_ROOT = os.geteuid() == 0


def load_helper():
    spec = importlib.util.spec_from_file_location("pe_launch_under_test", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def good_spec(**over):
    spec = {"session_id": "S-abc123", "worker_uid": 61001, "cwd": "/var/lib/pe/ws/S-abc123",
            "argv": ["/usr/bin/true"], "env": {"PATH": "/usr/bin"}, "token": "t" * 64}
    spec.update(over)
    return spec


def good_conf(tmp_path, **over):
    conf = {"version": 1, "uid_min": 61000, "uid_max": 61999, "core_uid": os.geteuid(), "core_gid": os.getegid(),
            "cgroup_root": str(tmp_path / "cg"), "workspace_root": str(tmp_path / "ws"),
            "sock_root": str(tmp_path / "sock"), "pids_max": 256}
    conf.update(over)
    return conf
