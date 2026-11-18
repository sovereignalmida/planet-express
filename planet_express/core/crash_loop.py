"""When is a container crash-looping? One rule for this host (Leela) and the other hosts (remote_health).

Docker's RestartCount counts the restart policy's restarts since the container was created or last started by hand
(`docker start`/`restart`, a recreate). It is not a rate, so a high count on its own is never a loop: a container that
restarted 60 times in one past incident and has been up and healthy for days is fine (2026-11-18: nine healthy containers
were reported down for exactly that). A loop is a container restarting NOW:

  * Docker says it is restarting;
  * it restarted CRASH_LOOP_RESTART_THRESHOLD or more times since the last scan (`delta`, when there is a baseline);
  * it has restarted and has been up for less than CRASH_LOOP_MIN_UPTIME_SECONDS;
  * its count is at or over the threshold, it has been up for less than CRASH_LOOP_NO_BASELINE_UPTIME_SECONDS, and it
    either has no baseline (first scan, a new or recreated container, another host) or restarted since the last scan.
    This also catches a steady loop slower than the gap between scans.

Pure: no Docker, no files.
"""

from __future__ import annotations

CRASH_LOOP_RESTART_THRESHOLD = 3
CRASH_LOOP_MIN_UPTIME_SECONDS = 60
CRASH_LOOP_NO_BASELINE_UPTIME_SECONDS = 600


def crash_loop_reason(*, state: str | None, up: bool, restart_count: int, delta: int | None,
                      uptime_seconds: float | None) -> str | None:
    """Why the container is crash-looping now, in words for its issue text, or None if it is not.

    `state` is Docker's State.Status, `up` whether it is running, `restart_count` Docker's RestartCount, `delta` the
    restarts since the last scan (None: no baseline), `uptime_seconds` how long it has been up (None: unknown)."""
    if state == "restarting":
        return f"restarting now, {restart_count} restarts lifetime"
    if delta is not None and delta >= CRASH_LOOP_RESTART_THRESHOLD:
        return f"restarted {delta}x since the last scan, {restart_count} lifetime"
    if uptime_seconds is None or uptime_seconds < 0 or not up:
        return None                                  # unknown (or a clock ahead of ours): no uptime to judge
    young = f"restarted {restart_count}x lifetime, up only {int(uptime_seconds)}s"
    if restart_count >= 1 and uptime_seconds < CRASH_LOOP_MIN_UPTIME_SECONDS:
        return young
    if (restart_count >= CRASH_LOOP_RESTART_THRESHOLD and uptime_seconds < CRASH_LOOP_NO_BASELINE_UPTIME_SECONDS
            and (delta is None or delta >= 1)):
        return young
    return None
