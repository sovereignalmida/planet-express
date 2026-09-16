"""
Tests for casa_hermes.py -- analyze() calls a real LLM so it isn't unit-testable
without a mock; these cover the pure-Python pieces (_slim_snapshot) plus a
regression guard on SYSTEM_PROMPT wording for a real false-positive we hit: the
LLM read a backup job's systemd "state": "inactive" (its normal resting state
between timer-triggered oneshot runs) as a failure signal and fabricated "no
recent verified run" against a snapshot whose own result/last_run said the run
succeeded a few hours earlier. See casa_leela.py:check_backups() -- "state" is
ActiveState, not a health signal; "result" and "last_run" are.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_hermes


def test_slim_snapshot_passes_backup_result_and_last_run_through_unmodified():
    snapshot = {
        "timestamp": "2026-07-25T11:57:08+00:00",
        "backups": {
            "daily": {"state": "inactive", "result": "success", "exit_code": "0",
                       "last_run": "Sat 2026-07-25 03:10:09 WEST"},
        },
    }
    slim = casa_hermes._slim_snapshot(snapshot)
    assert slim["backups"] == snapshot["backups"]


def test_system_prompt_clarifies_backup_inactive_state_is_not_a_failure_signal():
    prompt = casa_hermes.SYSTEM_PROMPT
    assert "oneshot" in prompt.lower()
    assert '"state": "inactive"' in prompt
    assert "NORMAL resting state" in prompt
