---
id: disk-prune-order
title: Free disk space without losing the rollback target
type: procedure
scope: ["host:disk", "volume:docker-root"]
source: operator-stated
evidence: ["incident-example-4"]
---
Reclaim space in the order that is safe for a held rollback.
1. Record which image is held for rollback.
   check: the held image reference is written down.
2. Prune build cache only.
   check: free space increased and the held image still exists.
3. Prune dangling images that are not the held image.
   check: the held image is still present and free space is above the alert threshold.
