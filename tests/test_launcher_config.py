"""launch.conf (the root-owned file the helper loads) and the `missions.*` launcher settings Core reads."""
import json
import os

import pytest
from launcher_support import load_helper

from config_schema import MissionsConfig
from planet_express.missions.launcher.config import ConfError, LaunchConf, load

helper = load_helper()


def conf(**over):
    body = dict(uid_min=61000, uid_max=61999, core_uid=1000, core_gid=1000, cgroup_root="/sys/fs/cgroup/pe/sessions",
                workspace_root="/var/lib/pe/ws", sock_root="/run/pe-socks", pids_max=512)
    body.update(over)
    return LaunchConf(**body)


def test_what_core_side_setup_renders_is_what_the_helper_accepts(tmp_path):
    path = tmp_path / "launch.conf"
    path.write_text(conf().render())
    path.chmod(0o600)
    tmp_path.chmod(0o755)
    loaded = helper.load_conf(str(path), trusted_uid=os.geteuid(), below=str(tmp_path))
    assert loaded == json.loads(conf().render())


@pytest.mark.parametrize("bad", [
    dict(uid_min=0), dict(uid_max=500), dict(core_uid=61500), dict(uid_min=True), dict(pids_max=-1),
    dict(cgroup_root="rel"), dict(workspace_root="/a/../b"), dict(sock_root="/"), dict(sock_root="/var/lib/pe/ws"),
    dict(cgroup_root="/a\0b"), dict(uid_max=2**31),
])
def test_the_core_side_model_and_the_helper_refuse_the_same_bad_configs(tmp_path, bad):
    with pytest.raises(ConfError):
        conf(**bad)
    body = json.loads(conf().render())
    body.update(bad)
    path = tmp_path / "launch.conf"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(path), trusted_uid=os.geteuid(), below=str(tmp_path))


def test_parse_refuses_unknown_and_missing_keys_and_non_json():
    for text in ("", "[]", "{}", json.dumps({**json.loads(conf().render()), "extra": 1}), "{nope"):
        with pytest.raises(ConfError):
            LaunchConf.parse(text)
    assert LaunchConf.parse(conf().render()) == conf()


def test_the_top_uid_of_the_range_is_the_probe_uid():
    assert conf().probe_uid == 61999


def test_load_refuses_a_symlink_and_a_group_writable_file(tmp_path):
    real = tmp_path / "real.conf"
    real.write_text(conf().render())
    real.chmod(0o600)
    link = tmp_path / "link.conf"
    link.symlink_to(real)
    assert load(str(real)) == conf()
    with pytest.raises(ConfError):
        load(str(link))
    real.chmod(0o660)
    with pytest.raises(ConfError):
        load(str(real))
    with pytest.raises(ConfError):
        load(str(tmp_path / "missing.conf"))


# ── missions.* ──────────────────────────────────────────────────────────────

def test_launcher_settings_have_safe_defaults():
    cfg = MissionsConfig()
    assert (cfg.launcher, cfg.session_uid_min, cfg.session_uid_max) == ("auto", 200000, 200999)
    assert cfg.sock_root == "/run/planetexpress-sessions" and cfg.enabled is False


@pytest.mark.parametrize("bad", [
    {"launcher": "setuid"}, {"session_uid_min": 0}, {"session_uid_min": 61999, "session_uid_max": 61000},
    {"session_uid_min": 61000, "session_uid_max": 61001}, {"sock_root": "relative"}, {"sock_root": "/a/../b"},
    {"sock_root": "/"}, {"session_uid_min": 2**31},
])
def test_a_bad_launcher_setting_is_a_config_error(bad):
    with pytest.raises(ValueError):
        MissionsConfig(**bad)


def test_the_range_may_not_contain_the_core_users_uid():
    me = os.geteuid()
    with pytest.raises(ValueError, match="Core user"):
        MissionsConfig(session_uid_min=max(me - 1, 1), session_uid_max=me + 5)


def test_the_retired_worker_user_key_is_still_accepted_so_old_configs_load():
    assert MissionsConfig(worker_user="pe-worker").worker_user == "pe-worker"


# ── one source of truth for the paths Core and the helper must agree on (Codex review) ───────────

def test_setup_defaults_are_the_missions_defaults_so_core_and_the_helper_agree():
    from planet_express.missions.launcher.setup_steps import Paths
    defaults = MissionsConfig()
    paths = Paths(core_uid=1000, core_gid=1000, workspace_root="/var/lib/pe/mission-workspaces")
    conf = paths.launch_conf()
    assert (conf.cgroup_root, conf.sock_root) == (defaults.cgroup_root, defaults.sock_root)
    assert (conf.uid_min, conf.uid_max) == (defaults.session_uid_min, defaults.session_uid_max)


def test_setup_paths_can_be_built_straight_from_a_missions_config():
    from planet_express.missions.launcher.setup_steps import Paths
    cfg = MissionsConfig(cgroup_root="/sys/fs/cgroup/x/sessions", sock_root="/run/s", session_uid_min=300000,
                         session_uid_max=300100)
    paths = Paths.from_missions(cfg, state_dir="/var/lib/pe/state", core_uid=1000, core_gid=1000)
    conf = paths.launch_conf()
    assert (conf.cgroup_root, conf.sock_root, conf.uid_min, conf.uid_max) == ("/sys/fs/cgroup/x/sessions", "/run/s",
                                                                              300000, 300100)
    assert conf.workspace_root == "/var/lib/pe/state/mission-workspaces"
    explicit = Paths.from_missions(MissionsConfig(workspace_root="/srv/ws"), state_dir="/x", core_uid=1000,
                                   core_gid=1000)
    assert explicit.launch_conf().workspace_root == "/srv/ws"


def test_the_runner_gives_the_supervisor_the_configured_workspace_root(tmp_path):
    from missions_helpers import make_store

    from planet_express.missions.containment import FakeContainment
    from planet_express.missions.runner import build_mission_runner
    store, _ = make_store(tmp_path)
    cfg = MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]}, workspace_root=str(tmp_path / "ws-root"))
    runner = build_mission_runner(store, cfg, containment=FakeContainment())
    assert str(runner.supervisor.workspace_dir) == str(tmp_path / "ws-root")


def test_a_bad_workspace_root_is_a_config_error():
    for bad in ("relative", "/a/../b", "/"):
        with pytest.raises(ValueError):
            MissionsConfig(workspace_root=bad)


# ── log groups: what a session that reads logs is started with ───────────────────────────────────

def test_an_empty_log_group_list_is_not_written_so_an_older_helper_still_accepts_the_file():
    assert "log_groups" not in json.loads(conf().render())
    with_groups = conf(log_groups=("adm", "systemd-journal"))
    assert json.loads(with_groups.render())["log_groups"] == ["adm", "systemd-journal"]
    assert LaunchConf.parse(with_groups.render()) == with_groups


@pytest.mark.parametrize("bad", [("root",), ("docker",), ("sudo",), ("incus-admin",), ("podman",), ("Adm",), ("adm", "adm"),
                                 ("adm", "systemd-journal", "syslog"), ("",)])
def test_any_group_that_is_not_a_log_reader_is_refused_by_every_side(tmp_path, bad):
    from config_schema import MissionsConfig
    with pytest.raises(ConfError):
        conf(log_groups=bad)
    with pytest.raises(ValueError):
        MissionsConfig(read_log_groups=list(bad))
    raw = json.loads(conf().render())
    raw["log_groups"] = list(bad)
    path = tmp_path / "launch.conf"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    tmp_path.chmod(0o755)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(path), trusted_uid=os.geteuid(), below=str(tmp_path))


def test_the_three_allowlists_are_the_same():
    from config_schema import LOG_READER_GROUPS
    from planet_express.missions.launcher import config as core
    assert core.LOG_GROUPS_ALLOWED == helper.LOG_GROUPS_ALLOWED == LOG_READER_GROUPS == {"adm", "systemd-journal"}
    assert core.MAX_LOG_GROUPS == helper.MAX_LOG_GROUPS


def test_the_log_gids_are_the_conf_groups_that_exist_and_never_gid_0_core_or_the_session_range(monkeypatch):
    import grp
    table = {"adm": 4, "systemd-journal": 999, "rooty": 0, "coreg": 777, "insession": 61500}

    class G:
        def __init__(self, gid):
            self.gr_gid = gid

    def getgrnam(name):
        if name not in table:
            raise KeyError(name)
        return G(table[name])

    monkeypatch.setattr(grp, "getgrnam", getgrnam)
    base = {"core_gid": 777, "uid_min": 61000, "uid_max": 61999}
    assert helper.log_gids({**base, "log_groups": ["adm", "missing", "systemd-journal"]}) == [4, 999]
    assert helper.log_gids(base) == []
    for name in ("rooty", "coreg", "insession"):
        with pytest.raises(helper.Refused):
            helper.log_gids({**base, "log_groups": [name]})


def test_drop_privileges_keeps_only_the_asked_groups(monkeypatch):
    calls = {}
    state = {"groups": []}
    monkeypatch.setattr(helper.os, "setgroups", lambda g: (calls.__setitem__("groups", list(g)), state.__setitem__("groups", list(g))))
    monkeypatch.setattr(helper.os, "setresgid", lambda *a: None)
    monkeypatch.setattr(helper.os, "setresuid", lambda *a: None)
    monkeypatch.setattr(helper.os, "getresuid", lambda: (61001,) * 3)
    monkeypatch.setattr(helper.os, "getresgid", lambda: (61001,) * 3)
    monkeypatch.setattr(helper.os, "getgroups", lambda: state["groups"])

    def no_root(_):
        raise PermissionError

    class Libc:
        def prctl(self, *a):
            return 0

    monkeypatch.setattr(helper.os, "setuid", no_root)
    monkeypatch.setattr(helper.ctypes, "CDLL", lambda *a, **k: Libc())
    helper.drop_privileges(61001, 61001)
    assert calls["groups"] == []                         # the default is what it always was: no groups at all
    helper.drop_privileges(61001, 61001, [4, 999])
    assert calls["groups"] == [4, 999]
    monkeypatch.setattr(helper.os, "getgroups", lambda: [4, 999, 0])      # something extra survived: not dropped
    with pytest.raises(helper.Refused):
        helper.drop_privileges(61001, 61001, [4, 999])
