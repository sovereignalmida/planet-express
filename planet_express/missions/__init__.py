"""Mission control (v4). Off by default: nothing here runs unless `missions.enabled` is true.

Slice 1 is the durable core only: strict models, canonical hashing, the v6 DDL, the state machine,
leases with fences, approvals and the one-transaction session start. No runtime, no workers.
"""
