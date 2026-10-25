"""A dead session's workspace that Core cannot remove is emptied by a cleanup session as the same uid; only then is the uid released.

The real thing (a 0700 directory made by the session uid, deleted under the real launcher) is `tests/test_p0_console_privileged.py`.
Here the supervisor's side is checked against the fake containment: what is launched, with what, and when the uid is let go.
"""
import os
import stat

import pytest
from supervisor_fakes import Rig, update

from planet_express.missions.supervisor import (
    CLEANUP_FIND,
    CLEANUP_RETRY_S,
    CLEANUP_WALL_S,
)


def _finished_rig_with_stuck_workspace(tmp_path):
    """A mission whose session finished, leaving a directory its owner could delete and Core cannot."""
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    session_id = rig.session_ids()[0]
    stuck = rig.sup.workspace_dir / session_id / ".cache"
    stuck.mkdir()
    (stuck / "blob").write_text("x")
    stuck.chmod(0o500)                       # no write on the directory: rmtree cannot unlink what is inside
    rig.sup.poll(rig.mid)
    return rig, session_id, stuck


def _let_it_be_deleted(stuck):
    """What the cleanup session's `find -delete` does as the owning uid."""
    stuck.chmod(0o700)


@pytest.fixture(autouse=True)
def _restore_modes(tmp_path):
    yield
    for base, dirs, _ in os.walk(tmp_path):
        for d in dirs:
            try:
                os.chmod(os.path.join(base, d), stat.S_IRWXU)
            except OSError:
                pass


def test_a_workspace_core_cannot_remove_keeps_the_uid_held_and_starts_a_cleanup_session(tmp_path):
    rig, sid, _ = _finished_rig_with_stuck_workspace(tmp_path)
    row = rig.ms.get_session(sid)
    assert row["uid_released_at"] is None                              # the reap could not remove it
    assert rig.sup.sweep_uids() == 0
    spawn = rig.cont.spawns("X-" + sid)[0]
    workspace = rig.sup.workspace_dir / sid
    assert spawn["argv"] == [CLEANUP_FIND, str(workspace), "-xdev", "-mindepth", "1", "-delete"]
    assert spawn["uid"] == row["session_uid"] and spawn["sock_dir"] is False
    assert spawn["cwd"] == str(rig.sup.workspace_dir / ("X-" + sid))    # an empty scratch directory, never the old workspace
    assert os.listdir(spawn["cwd"]) == []
    assert spawn["env"] == {"PATH": "/usr/bin:/bin"} and len(spawn["token"]) >= 16
    assert rig.ms.get_session(sid)["uid_released_at"] is None


def test_the_uid_is_released_only_after_the_cleanup_session_has_gone_and_the_workspace_is_empty(tmp_path):
    rig, sid, stuck = _finished_rig_with_stuck_workspace(tmp_path)
    rig.sup.sweep_uids()
    assert rig.cont.exists("X-" + sid)
    assert rig.sup.sweep_uids() == 0                                   # still running: nothing happens, no second launch
    assert len(rig.cont.spawns("X-" + sid)) == 1
    _let_it_be_deleted(stuck)
    rig.cont.crash("X-" + sid)                                          # the cleanup session ends
    assert rig.sup.sweep_uids() == 1
    assert rig.ms.get_session(sid)["uid_released_at"] is not None
    workspace = rig.sup.workspace_dir / sid
    assert not workspace.exists() and not (rig.sup.workspace_dir / ("X-" + sid)).exists()
    assert not rig.cont.exists("X-" + sid)


def test_a_cleanup_session_that_fails_leaves_the_uid_held_and_is_not_relaunched_in_a_loop(tmp_path):
    rig, sid, _ = _finished_rig_with_stuck_workspace(tmp_path)
    launches = []
    real = rig.cont.launch
    rig.cont.launch = lambda session_id, spec: (launches.append(session_id), real(session_id, spec))[1]
    rig.sup.sweep_uids()
    rig.cont.crash("X-" + sid)                                          # it ended and the workspace is still there
    assert rig.sup.sweep_uids() == 0
    assert rig.ms.get_session(sid)["uid_released_at"] is None
    for _ in range(3):
        assert rig.sup.sweep_uids() == 0
    assert launches == ["X-" + sid]                                     # backed off, not relaunched on every tick
    assert not rig.cont.exists("X-" + sid)
    rig.clock.t += CLEANUP_RETRY_S + 1
    rig.sup.sweep_uids()
    assert launches == ["X-" + sid] * 2                                 # a fresh attempt after the back-off


def test_a_cleanup_session_that_never_finishes_is_killed(tmp_path):
    rig, sid, _ = _finished_rig_with_stuck_workspace(tmp_path)
    rig.sup.sweep_uids()
    rig.clock.t += CLEANUP_WALL_S + 1
    rig.sup.sweep_uids()
    assert rig.cont.kill_calls("X-" + sid) >= 1
    assert rig.ms.get_session(sid)["uid_released_at"] is None


def test_the_uid_stays_held_while_something_else_still_runs_as_it(tmp_path):
    rig, sid, _ = _finished_rig_with_stuck_workspace(tmp_path)
    uid = rig.ms.get_session(sid)["session_uid"]
    rig.cont.uids_running.add(uid)
    assert rig.sup.sweep_uids() == 0
    assert not rig.cont.exists("X-" + sid)                              # no cleanup session while a stranger holds the uid
    rig.cont.uids_running.clear()
    rig.sup.sweep_uids()
    assert rig.cont.exists("X-" + sid)


def test_a_launch_failure_is_a_retry_later_not_a_crash(tmp_path):
    rig, sid, _ = _finished_rig_with_stuck_workspace(tmp_path)
    rig.cont.fail_launch = RuntimeError("helper refused")
    assert rig.sup.sweep_uids() == 0
    assert not (rig.sup.workspace_dir / ("X-" + sid)).exists()          # the scratch directory is not left behind
    assert rig.ms.get_session(sid)["uid_released_at"] is None


def test_an_ordinary_session_needs_no_cleanup_session(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.sup.poll(rig.mid)
    assert rig.ms.get_session(sid)["uid_released_at"] is not None
    assert not rig.cont.exists("X-" + sid) and rig.cont.spawns("X-" + sid) == []
