"""Tripwires (v4 slice 6, contract 9): continuous state diff against the grant while a mission executes.

`signals` reads host state, `diff` judges a change (allowed / unexpected_in_scope / protected / out_of_scope), and
`monitor` runs the scan in its own thread and pauses the mission (freeze, post-change capture, durable event) on
anything it cannot vouch for. With missions disabled nothing here starts a thread.
"""

from .diff import Change, DiffContext, diff_snapshots
from .monitor import (
    PAUSE_SLA_S,
    BlindBaseline,
    RollbackRefused,
    TripwireMonitor,
    TripwireStore,
    pause_card,
)
from .signals import SIGNALS, ScanEnv, Scanner, ScanResult, SignalError

__all__ = [
    "PAUSE_SLA_S", "SIGNALS", "BlindBaseline", "Change", "DiffContext", "RollbackRefused", "ScanEnv", "ScanResult", "Scanner",
    "SignalError", "TripwireMonitor", "TripwireStore", "diff_snapshots", "pause_card",
]
