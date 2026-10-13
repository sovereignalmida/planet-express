---
id: netns-keep-container-mode
title: Keep container network mode for the downloads stack
type: decision
scope: ["stack:downloads"]
source: operator-stated
evidence: ["doc:operator-notes-downloads"]
review_after: 2027-06-30
---
Decision: keep network_mode container:vpn-gateway rather than a per-service sidecar until the stack is
rebuilt. Trade-off accepted: the recreate hazard described in netns-stale-after-recreate.
