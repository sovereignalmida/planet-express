"""Checkpoint and restore (v4 slice 5, contract §7).

Before a Repair mission's first mutation Core snapshots the in-scope files and records host state; after a
crash or a tripwire the operator can restore it, and restore refuses to overwrite anything that drifted.
Nothing here starts a session, runs a tripwire or decides approval: the supervisor and later slices call
`CheckpointService`. When missions are disabled no entry point does anything.
"""

from .backup import BackupFreshness, BackupStatus, ConfiguredBackupFreshness
from .provider import (
    CheckpointProvider,
    ComposeFile,
    ContainerFacts,
    MosCheckpointProvider,
    ProviderError,
    RecreateResult,
    SystemdCheckpointProvider,
)
from .service import (
    BackupFloorError,
    CheckpointCorrupt,
    CheckpointDisabled,
    CheckpointError,
    CheckpointExists,
    CheckpointId,
    CheckpointInUse,
    CheckpointNotFound,
    CheckpointScope,
    CheckpointService,
    CheckpointSettings,
    DiskGuardError,
    RestoreReport,
    ScopeChanged,
    ScopeRefused,
)

__all__ = [
    "BackupFloorError", "BackupFreshness", "BackupStatus", "CheckpointCorrupt", "CheckpointDisabled",
    "CheckpointError", "CheckpointExists", "CheckpointId", "CheckpointInUse", "CheckpointNotFound",
    "CheckpointProvider", "CheckpointScope", "CheckpointService", "CheckpointSettings", "ComposeFile",
    "ConfiguredBackupFreshness", "ContainerFacts", "DiskGuardError", "MosCheckpointProvider", "ProviderError",
    "RecreateResult", "RestoreReport", "ScopeChanged", "ScopeRefused",
    "SystemdCheckpointProvider",
]
