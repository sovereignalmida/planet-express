---
id: disk-pressure-held-rollback
title: Disk pressure caused by a held rollback image
type: fact
scope: ["host:disk", "stack:apps", "volume:docker-root"]
source: observed
evidence: ["incident-example-4"]
---
The docker root filesystem filled because a previous image version was held for rollback after a canary
update, alongside build cache and unused images. Pruning without regard to the hold removes the rollback
target.
