---
id: netns-stale-after-recreate
title: Dependents lose network after the gateway container is recreated
type: fact
scope: ["stack:downloads", "container:vpn-gateway", "network:container-mode"]
source: observed
confidence_note: "seen in two incidents"
evidence: ["incident-example-1", "incident-example-2"]
review_after: 2027-04-01
---
Containers that join the network namespace of vpn-gateway (network_mode container:vpn-gateway) keep a
handle to the OLD namespace when vpn-gateway is recreated. They stay running and report healthy but have
no working network. A plain restart of the gateway does not cause this; a recreate (new container id) does.
