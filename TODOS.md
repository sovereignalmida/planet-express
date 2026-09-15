# TODOS

## Dashboard

### "Load earlier" log paging in the container panel

**What:** Let the dashboard's container log panel page backward past the "earlier lines skipped" marker.

**Why:** A noisy container that writes more than 500 lines (or 256 KiB) between the panel's 3s polls hits the cap; today you see the gap marker but must SSH in and run `docker logs` to read what was skipped, which is exactly what you'd want from your phone mid-incident.

**Context:** Deferred during `/plan-eng-review` (2026-09-13, decision D1) to keep landing 1c small; the skipped-lines marker keeps gaps visible so nothing is silently lost. Spec already worked out in the spec review of `docs/designs/planet-express-2-0-slices.md`: fetch `docker logs --timestamps --since <pre-gap cursor> --until <first returned ts> --tail 500`, dedup both edges by content hash, repeat until `skipped` is false. Needs an `until` parameter on the RPC `logs.tail` method (`planet_express/integrations/rpc.py`), a "load earlier" control in `static/dashboard.js`, and tests in `test_actions.py` for edge dedup and multi-page gaps. Start by reproducing a gap on the test homelab's `crash-loop` fixture.

**Effort:** M
**Priority:** P3
**Depends on:** Landing 1c (dashboard RPC + container panel)

## Completed
