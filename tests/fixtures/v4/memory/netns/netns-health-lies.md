---
id: netns-health-lies
title: Container health does not prove network reachability
type: fact
scope: ["stack:downloads", "network:container-mode"]
source: inferred
evidence: ["incident-example-2"]
---
The built-in health check of the dependents only tests a local port, so a dependent with a dead
namespace still reports healthy. Verify by probing an external endpoint from inside the container.
