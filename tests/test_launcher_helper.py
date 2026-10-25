"""pe_launch.py's validation, exercised without privilege (the root-only half is test_launcher_privileged.py).

Everything the helper decides is checked here against a throwaway tree: the config file and its ancestors, the uid
range, the cwd, and that no spec field can select another gid, root, the Core uid, or a path outside the roots."""
import json
import os
import subprocess
import sys

import pytest
from launcher_support import HELPER_PATH, IS_ROOT, good_conf, good_spec, load_helper

helper = load_helper()
ME = os.geteuid()


def write_conf(tmp_path, conf=None, mode=0o600, name="launch.conf"):
    path = tmp_path / name
    path.write_text(json.dumps(good_conf(tmp_path) if conf is None else conf))
    path.chmod(mode)
    tmp_path.chmod(0o755)
    return str(path)


# ── the config and its ancestors ─────────────────────────────────────────────────────────────────

def test_a_good_conf_loads(tmp_path):
    conf = helper.load_conf(write_conf(tmp_path), trusted_uid=ME, below=str(tmp_path))
    assert conf["uid_min"] == 61000 and conf["pids_max"] == 256


@pytest.mark.parametrize("mutation", [
    {"extra": 1}, {"version": 2}, {"uid_min": 0}, {"uid_max": 100}, {"uid_min": True}, {"core_uid": 61500},
    {"cgroup_root": "relative"}, {"cgroup_root": "/a/../b"}, {"workspace_root": "/"},
    {"sock_root": "/x\0y"}, {"pids_max": -1},
])
def test_a_bad_conf_is_refused(tmp_path, mutation):
    conf = good_conf(tmp_path)
    conf.update(mutation)
    with pytest.raises(helper.Refused):
        helper.load_conf(write_conf(tmp_path, conf), trusted_uid=ME, below=str(tmp_path))


def test_conf_with_a_duplicate_or_missing_key_is_refused(tmp_path):
    conf = good_conf(tmp_path)
    del conf["pids_max"]
    with pytest.raises(helper.Refused):
        helper.load_conf(write_conf(tmp_path, conf), trusted_uid=ME, below=str(tmp_path))
    path = tmp_path / "dup.conf"
    path.write_text('{"version": 1, "version": 1}')
    path.chmod(0o600)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(path), trusted_uid=ME, below=str(tmp_path))


def test_conf_readable_or_writable_by_others_is_refused(tmp_path):
    for mode in (0o644, 0o660, 0o606, 0o666):
        with pytest.raises(helper.Refused):
            helper.load_conf(write_conf(tmp_path, mode=mode), trusted_uid=ME, below=str(tmp_path))


def test_conf_owned_by_someone_else_is_refused(tmp_path):
    with pytest.raises(helper.Refused):
        helper.load_conf(write_conf(tmp_path), trusted_uid=ME + 1, below=str(tmp_path))


def test_a_symlinked_conf_is_refused(tmp_path):
    real = write_conf(tmp_path, name="real.conf")
    link = tmp_path / "launch.conf"
    link.symlink_to(real)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(link), trusted_uid=ME, below=str(tmp_path))


def test_a_symlinked_ancestor_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    write_conf(real)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(tmp_path / "link" / "launch.conf"), trusted_uid=ME, below=str(tmp_path))


@pytest.mark.parametrize("bits", [0o020, 0o002, 0o022])
def test_a_group_or_world_writable_ancestor_is_refused(tmp_path, bits):
    parent = tmp_path / "etc"
    parent.mkdir()
    write_conf(parent)
    (tmp_path / "etc").chmod(0o755 | bits)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(parent / "launch.conf"), trusted_uid=ME, below=str(tmp_path))


def test_a_writable_grandparent_is_refused_too(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    write_conf(inner)
    outer.chmod(0o777)
    with pytest.raises(helper.Refused):
        helper.load_conf(str(inner / "launch.conf"), trusted_uid=ME, below=str(tmp_path))


def test_a_directory_is_not_a_conf_file(tmp_path):
    (tmp_path / "launch.conf").mkdir()
    with pytest.raises(helper.Refused):
        helper.load_conf(str(tmp_path / "launch.conf"), trusted_uid=ME, below=str(tmp_path))


# ── the spec against the config: authority never comes from the spec ─────────────────────────────

@pytest.fixture
def conf(tmp_path):
    return good_conf(tmp_path)


@pytest.mark.parametrize("uid", [0, 1, 999, 60999, 62000, 100000, 2**31])
def test_a_uid_outside_the_configured_range_is_refused(conf, uid):
    with pytest.raises(helper.Refused):
        helper.validate(good_spec(worker_uid=uid), conf)


def test_the_core_uid_and_root_are_never_acceptable(tmp_path):
    for core in (61500,):
        conf = good_conf(tmp_path, uid_min=61000, uid_max=61999, core_uid=0)
        with pytest.raises(helper.Refused):
            helper.validate(good_spec(worker_uid=0), conf)
    conf = good_conf(tmp_path, core_uid=70000)
    assert helper.validate(good_spec(worker_uid=61001), conf) == (61001, 61001)


def test_the_gid_is_the_uid_and_cannot_be_chosen(conf):
    assert helper.validate(good_spec(worker_uid=61234), conf) == (61234, 61234)
    with pytest.raises(helper.Refused):
        helper.check_wire(good_spec(gid=0))
    with pytest.raises(helper.Refused):
        helper.check_wire(good_spec(group="root"))


def test_a_gid_equal_to_the_core_gid_is_refused(tmp_path):
    with pytest.raises(helper.Refused):
        helper.validate(good_spec(worker_uid=61001), good_conf(tmp_path, core_gid=61001))


def test_cwd_must_be_below_the_workspace_root(tmp_path, conf):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "S-1").mkdir()
    (tmp_path / "other").mkdir()
    fd = helper.open_cwd(good_spec(cwd=str(tmp_path / "ws" / "S-1")), conf, 61001)
    os.close(fd)
    for bad in (str(tmp_path / "ws"), str(tmp_path / "other"), "/", "/etc", str(tmp_path / "ws" / ".." / "other"),
                str(tmp_path / "ws") + "-evil/S-1", str(tmp_path / "ws" / "S-1") + "/"):
        with pytest.raises(helper.Refused):
            helper.open_cwd(good_spec(cwd=bad), conf, 61001)


def test_a_symlinked_cwd_is_refused(tmp_path, conf):
    (tmp_path / "ws").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "ws" / "S-1").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(helper.Refused):
        helper.open_cwd(good_spec(cwd=str(tmp_path / "ws" / "S-1")), conf, 61001)


def test_a_cwd_with_a_symlinked_middle_component_is_refused(tmp_path, conf):
    (tmp_path / "ws").mkdir()
    (tmp_path / "elsewhere" / "S-1").mkdir(parents=True)
    (tmp_path / "ws" / "mid").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(helper.Refused):
        helper.open_cwd(good_spec(cwd=str(tmp_path / "ws" / "mid" / "S-1")), conf, 61001)


def test_a_cwd_owned_by_someone_else_is_refused(tmp_path):
    (tmp_path / "ws" / "S-1").mkdir(parents=True)
    conf = good_conf(tmp_path, core_uid=ME + 7)          # the directory is mine, which is neither Core nor the uid
    with pytest.raises(helper.Refused):
        helper.open_cwd(good_spec(cwd=str(tmp_path / "ws" / "S-1")), conf, 61001)


def test_a_cwd_that_is_a_file_is_refused(tmp_path, conf):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "S-1").write_text("x")
    with pytest.raises(helper.Refused):
        helper.open_cwd(good_spec(cwd=str(tmp_path / "ws" / "S-1")), conf, 61001)


def test_argv0_must_be_an_existing_executable_regular_file(tmp_path):
    helper.check_argv0(sys.executable)
    plain = tmp_path / "data"
    plain.write_text("x")
    for bad in (str(plain), str(tmp_path), str(tmp_path / "missing")):
        with pytest.raises(helper.Refused):
            helper.check_argv0(bad)


def test_open_dir_nofollow_refuses_links_and_files(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("x")
    (tmp_path / "l").symlink_to(tmp_path / "d")
    os.close(helper.open_dir_nofollow(str(tmp_path / "d")))
    for bad in ("f", "l", "missing"):
        with pytest.raises(helper.Refused):
            helper.open_dir_nofollow(str(tmp_path / bad))
    for bad in ("relative", "/a/../b", "/a//b", "/a\0"):
        with pytest.raises(helper.Refused):
            helper.open_dir_nofollow(bad)


# ── running the real script ──────────────────────────────────────────────────────────────────────

def run_helper(*args, stdin=b"", env=None):
    return subprocess.run([sys.executable, "-IS", str(HELPER_PATH), *args], input=stdin, capture_output=True,
                          env=env if env is not None else {"PATH": "/usr/bin:/bin"}, timeout=30, check=False)


def status_of(result):
    line = result.stdout.decode().splitlines()[0]
    assert line.startswith("PE-LAUNCH-STATUS ")
    return json.loads(line[len("PE-LAUNCH-STATUS "):])


@pytest.mark.skipif(IS_ROOT, reason="the non-root refusal needs a non-root caller")
def test_the_script_refuses_to_run_without_root_and_says_so_in_the_framed_line():
    result = run_helper(stdin=json.dumps(good_spec()).encode())
    assert result.returncode == 1
    assert status_of(result) == {"ok": False, "error": "not root"}
    assert "t" * 64 not in result.stdout.decode() + result.stderr.decode()


def test_any_argument_is_refused_before_anything_is_read():
    for args in (["--x"], [""], ["a", "b"]):
        result = run_helper(*args, stdin=json.dumps(good_spec()).encode())
        assert result.returncode == 1
        assert status_of(result)["ok"] is False
        assert "t" * 64 not in result.stdout.decode()


def test_the_first_output_line_is_always_the_framed_status_even_for_a_garbage_spec():
    for stdin in (b"", b"not json", b"\xff" * 10, b"x" * 70000, json.dumps(good_spec(worker_uid=0)).encode()):
        result = run_helper(stdin=stdin)
        assert result.returncode == 1
        assert status_of(result)["ok"] is False


def test_the_script_does_not_import_planet_express_or_any_third_party_module():
    text = HELPER_PATH.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "planet_express" not in stripped and "pydantic" not in stripped, stripped
    assert text.startswith("#!/usr/bin/python3 -IS\n")


def test_the_script_never_uses_a_shell_or_a_thread():
    text = HELPER_PATH.read_text()
    for forbidden in ("shell=True", "os.system", "subprocess", "threading", "preexec_fn", "os.popen"):
        assert forbidden not in text, forbidden


# ── the per-session socket directory Core made (contract 22.8.1) ────────────────────────────────

def make_sock_dir(tmp_path, names=("worker.sock",), core_uid=ME):
    import socket
    root = tmp_path / "sock"
    (root / "S-1").mkdir(parents=True, exist_ok=True)
    servers = []
    for name in names:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(root / "S-1" / name))
        servers.append(server)
    return good_conf(tmp_path, core_uid=core_uid), servers


def test_a_well_formed_socket_directory_is_adopted_and_handed_to_the_session(tmp_path):
    conf, servers = make_sock_dir(tmp_path, ("worker.sock", "proxy.sock"))
    try:
        path = helper.adopt_sock_dir(conf, "S-1", ME, os.getegid())          # chown to oneself is the unprivileged case
        assert path == str(tmp_path / "sock" / "S-1")
        info = os.stat(path)
        assert oct(info.st_mode & 0o7777) == "0o750" and info.st_uid == ME
        for name in ("worker.sock", "proxy.sock"):                  # Core's RpcServer made them 0660: no group access
            assert oct(os.stat(f"{path}/{name}").st_mode & 0o7777) == "0o600"
    finally:
        for s in servers:
            s.close()


@pytest.mark.parametrize("names, why", [((), "socket dir contents"), (("proxy.sock",), "socket dir contents"),
                                       (("worker.sock", "evil.sock"), "socket dir contents")])
def test_a_socket_directory_with_the_wrong_contents_is_refused(tmp_path, names, why):
    conf, servers = make_sock_dir(tmp_path, names)
    try:
        with pytest.raises(helper.Refused, match=why):
            helper.adopt_sock_dir(conf, "S-1", ME, os.getegid())
    finally:
        for s in servers:
            s.close()


def test_a_missing_symlinked_or_foreign_socket_directory_is_refused(tmp_path):
    conf, servers = make_sock_dir(tmp_path)
    try:
        with pytest.raises(helper.Refused, match="socket dir"):
            helper.adopt_sock_dir(conf, "S-missing", ME, os.getegid())
        (tmp_path / "sock" / "S-link").symlink_to(tmp_path / "sock" / "S-1")
        with pytest.raises(helper.Refused, match="socket dir"):
            helper.adopt_sock_dir(conf, "S-link", ME, os.getegid())
        with pytest.raises(helper.Refused, match="owner"):
            helper.adopt_sock_dir({**conf, "core_uid": ME + 1}, "S-1", ME, os.getegid())
    finally:
        for s in servers:
            s.close()


def test_a_regular_file_or_a_link_where_a_socket_belongs_is_refused(tmp_path):
    conf, servers = make_sock_dir(tmp_path, ("proxy.sock",))
    (tmp_path / "sock" / "S-1" / "worker.sock").write_text("not a socket")
    try:
        with pytest.raises(helper.Refused, match="socket entry"):
            helper.adopt_sock_dir(conf, "S-1", ME, os.getegid())
        (tmp_path / "sock" / "S-1" / "worker.sock").unlink()
        (tmp_path / "sock" / "S-1" / "worker.sock").symlink_to("/etc/passwd")
        with pytest.raises(helper.Refused, match="socket entry"):
            helper.adopt_sock_dir(conf, "S-1", ME, os.getegid())
    finally:
        for s in servers:
            s.close()


def test_a_hardlinked_socket_is_refused(tmp_path):
    conf, servers = make_sock_dir(tmp_path)
    try:
        os.link(tmp_path / "sock" / "S-1" / "worker.sock", tmp_path / "sock" / "extra")
    except OSError:
        pytest.skip("this filesystem cannot hard-link a socket")
    try:
        with pytest.raises(helper.Refused, match="socket entry"):
            helper.adopt_sock_dir(conf, "S-1", ME, os.getegid())
    finally:
        for s in servers:
            s.close()


# ── the reserved range belongs to no account (Codex) ────────────────────────────────────────────

def fake_db(monkeypatch, users=(), groups=()):
    from types import SimpleNamespace as NS

    class Pwd:
        @staticmethod
        def getpwuid(uid):
            for u in users:
                if u.pw_uid == uid:
                    return u
            raise KeyError(uid)

        @staticmethod
        def getpwall():
            return list(users)

    class Grp:
        @staticmethod
        def getgrgid(gid):
            for g in groups:
                if g.gr_gid == gid:
                    return g
            raise KeyError(gid)
    monkeypatch.setattr(helper, "pwd", Pwd)
    monkeypatch.setattr(helper, "grp", Grp)
    return NS


def test_an_empty_database_accepts_the_uid(monkeypatch):
    fake_db(monkeypatch)
    helper.check_identity_unused(61005, 61005)


def test_a_normal_account_with_its_own_private_group_is_refused(monkeypatch):
    from types import SimpleNamespace as NS
    fake_db(monkeypatch, users=[NS(pw_name="bob", pw_uid=61005, pw_gid=61005)],
            groups=[NS(gr_name="bob", gr_gid=61005, gr_mem=[])])
    with pytest.raises(helper.Refused, match="belongs to an account"):
        helper.check_identity_unused(61005, 61005)


def test_another_accounts_primary_group_with_this_number_is_refused(monkeypatch):
    from types import SimpleNamespace as NS
    fake_db(monkeypatch, users=[NS(pw_name="svc", pw_uid=1001, pw_gid=61005)])
    with pytest.raises(helper.Refused, match="primary group"):
        helper.check_identity_unused(61005, 61005)


def test_a_group_with_members_is_refused_and_an_empty_one_is_not(monkeypatch):
    from types import SimpleNamespace as NS
    fake_db(monkeypatch, groups=[NS(gr_name="g", gr_gid=61005, gr_mem=["bob"])])
    with pytest.raises(helper.Refused, match="members"):
        helper.check_identity_unused(61005, 61005)
    fake_db(monkeypatch, groups=[NS(gr_name="g", gr_gid=61005, gr_mem=[])])
    helper.check_identity_unused(61005, 61005)


def test_the_helpers_own_umask_does_not_depend_on_the_caller(tmp_path):
    text = HELPER_PATH.read_text()
    assert "os.umask(0o022)" in text and "os.fchmod(dir_fd, 0o755)" in text      # cgroup directory modes are explicit


# ── the per-conversation state directory Core made beside the workspaces ─────────────────────────

def make_state_dir(tmp_path, name="C-conv1", mode=0o700):
    conf = good_conf(tmp_path)
    root = tmp_path / "ws"
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name).chmod(mode)
    (root / "S-cwd").mkdir(exist_ok=True)
    cwd_fd = os.open(root / "S-cwd", os.O_RDONLY | os.O_DIRECTORY)
    return conf, root, cwd_fd


def test_a_state_directory_is_adopted_by_handing_over_the_directory_itself_and_nothing_inside(tmp_path):
    conf, root, cwd_fd = make_state_dir(tmp_path)
    inside = root / "C-conv1" / "cfg"
    inside.mkdir()
    (inside / "settings.json").write_text("{}")
    before = os.stat(inside / "settings.json")
    try:
        path = helper.adopt_state_dir(conf, "C-conv1", ME, cwd_fd)
        info = os.stat(path)
        assert path == str(root / "C-conv1") and info.st_uid == ME and oct(info.st_mode & 0o7777) == "0o2770"
        after = os.stat(inside / "settings.json")
        assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (before.st_ino, before.st_mtime_ns, before.st_mode)   # untouched
    finally:
        os.close(cwd_fd)


def test_a_state_directory_adopted_before_is_adopted_again_by_the_same_uid(tmp_path):
    conf, root, cwd_fd = make_state_dir(tmp_path)
    try:
        helper.adopt_state_dir(conf, "C-conv1", ME, cwd_fd)
        os.chmod(root / "C-conv1", 0o700)
        helper.adopt_state_dir({**conf, "core_uid": ME + 1}, "C-conv1", ME, cwd_fd)      # owned by the session uid now, not Core
        assert oct(os.stat(root / "C-conv1").st_mode & 0o7777) == "0o2770"
    finally:
        os.close(cwd_fd)


def test_a_state_directory_owned_by_neither_core_nor_the_session_uid_is_refused(tmp_path):
    conf, _, cwd_fd = make_state_dir(tmp_path)
    try:
        with pytest.raises(helper.Refused, match="state dir owner"):
            helper.adopt_state_dir({**conf, "core_uid": ME + 1}, "C-conv1", ME + 2, cwd_fd)     # another conversation's uid
    finally:
        os.close(cwd_fd)


@pytest.mark.parametrize("make, why", [
    (lambda root: None, "state dir"),                                                       # missing
    (lambda root: (root / "C-link").symlink_to(root / "C-conv1"), "state dir"),             # a symlink
    (lambda root: (root / "C-file").write_text("x"), "state dir"),                          # not a directory
])
def test_a_missing_symlinked_or_non_directory_state_dir_is_refused(tmp_path, make, why):
    conf, root, cwd_fd = make_state_dir(tmp_path)
    make(root)
    try:
        for name in ("C-missing", "C-link", "C-file"):
            with pytest.raises(helper.Refused, match=why):
                helper.adopt_state_dir(conf, name, ME, cwd_fd)
    finally:
        os.close(cwd_fd)


def test_the_session_s_own_workspace_cannot_be_its_state_directory(tmp_path):
    conf, root, _ = make_state_dir(tmp_path)
    cwd_fd = os.open(root / "C-conv1", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(helper.Refused, match="state dir is the workspace"):
            helper.adopt_state_dir(conf, "C-conv1", ME, cwd_fd)
    finally:
        os.close(cwd_fd)


def test_a_symlinked_workspace_root_is_refused_for_the_state_dir_too(tmp_path):
    conf, root, cwd_fd = make_state_dir(tmp_path)
    (tmp_path / "wslink").symlink_to(root)
    try:
        with pytest.raises(helper.Refused):
            helper.adopt_state_dir({**conf, "workspace_root": str(tmp_path / "wslink")}, "C-conv1", ME, cwd_fd)
    finally:
        os.close(cwd_fd)
