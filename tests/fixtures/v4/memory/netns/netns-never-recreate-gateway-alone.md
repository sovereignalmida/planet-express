---
id: netns-never-recreate-gateway-alone
title: Never recreate the gateway without its dependents
type: constraint
scope: ["stack:downloads", "container:vpn-gateway"]
source: operator-stated
evidence: ["doc:operator-notes-downloads"]
---
Do not recreate vpn-gateway on its own. Any recreate must be followed by recreating every container that
uses its network namespace, in the same maintenance step.
