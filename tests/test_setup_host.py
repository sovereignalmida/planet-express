"""One contract, two hosts: RealHost (on a temp directory) and FakeHost must behave identically."""
import os

import pytest
from fake_host import Crash, FakeHost, FaultyHost

from planet_express.setup.host import TEMP_PREFIX, HostError, RealHost

UID, GID = os.getuid(), os.getgid()


class _Real:
    kind = "real"

    def __init__(self, tmp_path):
        self.host = RealHost()
        self.root = str(tmp_path / "root")
        os.makedirs(self.root, mode=0o755)
        os.chmod(self.root, 0o755)

    def dir(self, rel, mode=0o755):
        path = f"{self.root}/{rel}"
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
        return path

    def file(self, rel, data=b"x", mode=0o644):
        path = f"{self.root}/{rel}"
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
        return path

    def symlink(self, rel, target):
        os.symlink(target, f"{self.root}/{rel}")
        return f"{self.root}/{rel}"

    def listing(self, rel=""):
        return sorted(os.listdir(f"{self.root}/{rel}".rstrip("/")))


class _Fake:
    kind = "fake"

    def __init__(self, tmp_path):
        self.host = FakeHost(trusted_uids={0, UID}, euid=UID, users={"svc": (UID, GID)}, groups={"svc": GID})
        self.root = "/root"
        self.host.add_dir(self.root, uid=UID, gid=GID)

    def dir(self, rel, mode=0o755):
        path = f"{self.root}/{rel}"
        self.host.add_dir(path, mode=mode, uid=UID, gid=GID)
        return path

    def file(self, rel, data=b"x", mode=0o644):
        path = f"{self.root}/{rel}"
        self.host.add_file(path, data, mode=mode, uid=UID, gid=GID)
        return path

    def symlink(self, rel, target):
        self.host.add_symlink(f"{self.root}/{rel}", target)
        return f"{self.root}/{rel}"

    def listing(self, rel=""):
        return self.host.listdir(f"{self.root}/{rel}".rstrip("/"))


@pytest.fixture(params=["real", "fake"])
def env(request, tmp_path):
    return (_Real if request.param == "real" else _Fake)(tmp_path)


def test_mkdir_creates_a_directory_with_the_exact_mode_and_owner(env):
    path = f"{env.root}/new"
    made = env.host.mkdir(path, 0o750, UID, GID)
    seen = env.host.lstat(path)
    assert seen.kind == "dir" and seen.perms == 0o750 and (seen.uid, seen.gid) == (UID, GID)
    assert made.identity == seen.identity


def test_mkdir_refuses_a_name_that_exists(env):
    env.dir("here")
    with pytest.raises(HostError):
        env.host.mkdir(f"{env.root}/here", 0o755, UID, GID)


def test_a_staged_file_is_invisible_until_committed_and_commit_is_atomic_replace(env):
    target = env.file("conf", b"old")
    temp, staged = env.host.stage_file(env.root, b"new", 0o640, UID, GID)
    assert temp.startswith(TEMP_PREFIX) and staged.perms == 0o640 and staged.size == 3
    assert env.host.read_bytes(target) == b"old"                       # the live file is untouched
    env.host.commit_staged(env.root, temp, "conf")
    assert env.host.read_bytes(target) == b"new" and temp not in env.listing()
    assert env.host.lstat(target).identity == staged.identity          # the inode we staged is the inode now there


def test_commit_creates_a_new_file_and_refuses_to_replace_a_directory(env):
    temp, _ = env.host.stage_file(env.root, b"fresh", 0o644, UID, GID)
    env.host.commit_staged(env.root, temp, "created")
    assert env.host.read_bytes(f"{env.root}/created") == b"fresh"
    env.dir("adir")
    temp, _ = env.host.stage_file(env.root, b"x", 0o644, UID, GID)
    with pytest.raises(HostError, match="not a regular file"):
        env.host.commit_staged(env.root, temp, "adir")
    assert env.host.lstat(f"{env.root}/adir").kind == "dir"            # untouched


def test_discard_removes_only_this_tools_staged_files(env):
    temp, _ = env.host.stage_file(env.root, b"x", 0o600, UID, GID)
    env.file("precious")
    env.host.discard_staged(env.root, temp)
    assert temp not in env.listing()
    with pytest.raises(HostError, match="only this tool"):
        env.host.discard_staged(env.root, "precious")
    assert "precious" in env.listing()


def test_a_symlink_at_the_final_component_is_never_followed(env):
    env.file("real-target", b"secret")
    link = env.symlink("link", f"{env.root}/real-target")
    assert env.host.lstat(link).kind == "symlink"
    with pytest.raises(HostError):
        env.host.read_bytes(link)
    temp, _ = env.host.stage_file(env.root, b"overwrite?", 0o644, UID, GID)
    with pytest.raises(HostError, match="not a regular file"):
        env.host.commit_staged(env.root, temp, "link")
    assert env.host.read_bytes(f"{env.root}/real-target") == b"secret"


def test_a_symlinked_parent_is_resolved_once_and_writes_land_in_the_real_directory(env):
    env.dir("real")
    env.symlink("alias", f"{env.root}/real")
    temp, _ = env.host.stage_file(f"{env.root}/alias", b"x", 0o644, UID, GID)
    env.host.commit_staged(f"{env.root}/alias", temp, "f")
    assert env.listing("real") == ["f"]


def test_an_ancestor_writable_by_others_refuses_any_mutation_through_it(env):
    env.dir("open", mode=0o777)
    env.dir("open/inner")
    with pytest.raises(HostError, match="writable by group or other"):
        env.host.stage_file(f"{env.root}/open/inner", b"x", 0o644, UID, GID)
    with pytest.raises(HostError, match="writable by group or other"):
        env.host.mkdir(f"{env.root}/open/inner/d", 0o755, UID, GID)


def test_a_sticky_world_writable_ancestor_like_tmp_is_acceptable(env):
    env.dir("sticky", mode=0o1777)
    env.dir("sticky/mine")
    temp, _ = env.host.stage_file(f"{env.root}/sticky/mine", b"x", 0o644, UID, GID)
    env.host.commit_staged(f"{env.root}/sticky/mine", temp, "ok")
    assert env.listing("sticky/mine") == ["ok"]


def test_unlink_and_rmdir_do_the_safe_thing(env):
    path = env.file("gone")
    env.host.unlink(path)
    env.host.unlink(path)                                              # already gone: a no-op
    assert env.host.lstat(path) is None
    env.dir("full")
    env.file("full/x")
    with pytest.raises(HostError):
        env.host.unlink(f"{env.root}/full")                            # a directory is not unlinked
    with pytest.raises(HostError):
        env.host.rmdir(f"{env.root}/full")                             # not empty
    env.host.unlink(f"{env.root}/full/x")
    env.host.rmdir(f"{env.root}/full")
    assert env.host.lstat(f"{env.root}/full") is None


def test_copy_private_makes_a_0600_backup_and_will_not_overwrite_one(env):
    source = env.file("src", b"data", mode=0o644)
    env.dir("backups")
    copy = env.host.copy_private(source, f"{env.root}/backups", "src.bak", UID, GID)
    assert copy.perms == 0o600 and env.host.read_bytes(f"{env.root}/backups/src.bak") == b"data"
    with pytest.raises(HostError):
        env.host.copy_private(source, f"{env.root}/backups", "src.bak", UID, GID)


def test_read_refuses_what_is_not_a_regular_file_or_is_too_big(env):
    env.dir("d")
    with pytest.raises(HostError):
        env.host.read_bytes(f"{env.root}/d")
    big = env.file("big", b"x" * 100)
    with pytest.raises(HostError, match="larger than"):
        env.host.read_bytes(big, limit=10)


def test_paths_must_be_absolute_and_name_a_real_file(env):
    for bad in ("relative/path", "/", "/a/..", "/a/."):
        with pytest.raises(HostError):
            env.host.unlink(bad) if bad != "relative/path" else env.host.mkdir(bad, 0o755, UID, GID)


def test_a_command_is_a_list_of_strings(env):
    for bad in ([], [1, 2], ["ok", None]):
        with pytest.raises(HostError):
            env.host.run(bad)


def test_the_real_host_runs_argv_without_a_shell_and_with_a_minimal_environment(tmp_path):
    host = RealHost()
    assert host.run(["true"]).rc == 0 and host.run(["false"]).rc == 1
    assert host.run(["no-such-binary-xyz"]).rc == 127
    marker = tmp_path / "must-not-exist"
    host.run(["echo", f"; touch {marker}"])                            # metacharacters are inert
    assert not marker.exists()
    os.environ["PE_TEST_LEAK"] = "leaked"
    assert "PE_TEST_LEAK" not in host.run(["env"]).out
    assert host.run(["sleep", "5"], timeout=0.2).rc == 124


def test_faulty_host_crashes_before_or_after_the_chosen_operation(env):
    inner = env.host
    with pytest.raises(Crash):
        FaultyHost(inner, crash_at=1).mkdir(f"{env.root}/a", 0o755, UID, GID)
    assert inner.lstat(f"{env.root}/a") is None                        # crashed before: nothing done
    with pytest.raises(Crash):
        FaultyHost(inner, crash_at=1, after=True).mkdir(f"{env.root}/b", 0o755, UID, GID)
    assert inner.lstat(f"{env.root}/b").kind == "dir"                  # crashed after: it happened


def test_chmod_dir_changes_a_directory_and_refuses_a_file_or_a_symlink(env):
    env.dir("d", mode=0o755)
    stat = env.host.chmod_dir(f"{env.root}/d", 0o700)
    assert stat.perms == 0o700 and env.host.lstat(f"{env.root}/d").perms == 0o700
    path = env.file("f")
    with pytest.raises(HostError):
        env.host.chmod_dir(path, 0o700)
    with pytest.raises(HostError):
        env.host.chmod_dir(f"{env.root}/missing", 0o700)


def test_remove_tree_deletes_only_the_directory_it_was_told_about(env):
    env.dir("venv")
    env.dir("venv/lib")
    env.file("venv/lib/x")
    env.file("venv/y")
    identity = env.host.lstat(f"{env.root}/venv").identity
    with pytest.raises(HostError):
        env.host.remove_tree(f"{env.root}/venv", {"dev": identity["dev"], "ino": identity["ino"] + 1})   # not the one we made
    assert env.host.lstat(f"{env.root}/venv/lib/x") is not None
    env.host.remove_tree(f"{env.root}/venv", identity)
    assert env.host.lstat(f"{env.root}/venv") is None
    env.host.remove_tree(f"{env.root}/venv", identity)                  # already gone: fine


def test_remove_tree_refuses_a_file(env):
    path = env.file("plain")
    with pytest.raises(HostError):
        env.host.remove_tree(path, env.host.lstat(path).identity)
    assert env.host.lstat(path) is not None


def test_tree_digest_changes_when_the_tree_does_and_ignores_bytecode(env):
    env.dir("t")
    env.dir("t/lib")
    env.file("t/lib/a", b"1")
    first = env.host.tree_digest(f"{env.root}/t")
    assert env.host.tree_digest(f"{env.root}/t") == first
    env.dir("t/lib/__pycache__")
    env.file("t/lib/__pycache__/a.pyc", b"x")
    assert env.host.tree_digest(f"{env.root}/t") == first              # running the code makes these
    env.file("t/lib/mine", b"precious")
    assert env.host.tree_digest(f"{env.root}/t") != first
    with pytest.raises(HostError):
        env.host.tree_digest(f"{env.root}/missing")


def test_tree_digest_notices_a_same_size_edit_and_a_retargeted_link(env):
    env.dir("t")
    env.file("t/f", b"aaaa")
    link = env.symlink("t/l", "/one")
    first = env.host.tree_digest(f"{env.root}/t")
    env.host.unlink(link)
    env.symlink("t/l", "/two")                                         # retargeted
    second = env.host.tree_digest(f"{env.root}/t")
    assert second != first
    env.file("t/f", b"bbbb")                                           # same size, different bytes
    assert env.host.tree_digest(f"{env.root}/t") != second


def test_tree_digest_does_not_block_on_a_fifo(tmp_path):
    import os

    from planet_express.setup.host import RealHost
    (tmp_path / "t").mkdir()
    os.mkfifo(tmp_path / "t" / "pipe")
    assert RealHost({0, os.geteuid()}).tree_digest(str(tmp_path / "t"))    # returns instead of waiting for a writer


def test_a_group_writable_directory_is_fine_when_the_group_is_only_trusted_accounts(env):
    """A stock Ubuntu clone is 775 with a one-person private group."""
    env.dir("mine", mode=0o775)
    env.dir("mine/inner")
    env.host.mkdir(f"{env.root}/mine/inner/d", 0o755, UID, GID)
    assert env.host.lstat(f"{env.root}/mine/inner/d") is not None


def test_a_group_writable_directory_is_refused_when_an_untrusted_account_shares_the_group():
    from fake_host import FakeHost
    host = FakeHost(trusted_uids={0, 1000}, users={"svc": (1000, 1000), "stranger": (2000, 2000)}, groups={"shared": 1000})
    host.memberships["stranger"] = {1000}
    host.add_dir("/srv/app", mode=0o775, uid=1000, gid=1000)
    with pytest.raises(HostError, match="writable by group or other"):
        host.mkdir("/srv/app/d", 0o755, 1000, 1000)


def test_stable_inodes_is_true_for_an_ordinary_directory(tmp_path):
    import os

    from planet_express.setup.host import RealHost
    assert RealHost({0, os.geteuid()}).stable_inodes(str(tmp_path)) is True
