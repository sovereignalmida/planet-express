"""Checkpoint tests that need root: owner restore and a different uid that must not see the store.

They refuse to run unless PE_PRIVILEGED_TESTS_OK=1, which scripts/run_privileged_tests.sh sets only inside a
disposable environment (docker or the MOS VM). Never run them on a developer machine.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENABLED = os.geteuid() == 0 and os.environ.get("PE_PRIVILEGED_TESTS_OK") == "1"
pytestmark = [pytest.mark.skipif(
    not ENABLED, reason="root-only: run scripts/run_privileged_tests.sh (disposable container or the MOS VM)")]

from checkpoint_fakes import make_env, tree

WORKER_UID = 61555


def as_uid(uid, fn):
    """Run fn in a forked child as `uid`; return True if it succeeded."""
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            fn()
            code = 0
        except BaseException:  # noqa: BLE001 - any failure in the child is a refusal
            code = 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def test_owner_is_restored_byte_for_byte(tmp_path):
    env = make_env(tmp_path)
    os.chown(env.dotenv, 1234, 4321)
    os.chown(env.compose, 0, 77)
    before = tree(env.stacks)
    cid = env.svc.create(env.mission.id, env.scope)
    os.chown(env.dotenv, 0, 0)
    env.compose.write_text("changed\n")
    os.chown(env.compose, 55, 55)
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.meta_skipped == []
    assert tree(env.stacks) == before


def test_worker_uid_cannot_list_or_read_the_store(tmp_path):
    os.chmod(tmp_path, 0o755)
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    os.chmod(tmp_path / "state", 0o755)
    payload = env.root / cid / "files" / "0"
    assert not as_uid(WORKER_UID, lambda: os.listdir(env.root))
    assert not as_uid(WORKER_UID, lambda: os.listdir(env.root / cid))
    assert not as_uid(WORKER_UID, lambda: payload.read_bytes())
    assert as_uid(0, lambda: payload.read_bytes())
