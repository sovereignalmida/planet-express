---
id: netns-recreate-dependents
title: Recover dependents after a gateway recreate
type: procedure
scope: ["stack:downloads", "container:vpn-gateway"]
source: operator-stated
evidence: ["incident-example-1"]
supersedes: []
---
Recreate the dependents of vpn-gateway so they join the new namespace.
1. List the containers whose network mode points at vpn-gateway.
   check: the list is non-empty and every entry belongs to stack downloads.
2. Recreate each listed container with compose up --force-recreate for that service only.
   check: each container shows a new container id and state running.
3. Probe outbound connectivity from inside one dependent.
   check: the probe to the stack's configured test endpoint returns HTTP 200.
