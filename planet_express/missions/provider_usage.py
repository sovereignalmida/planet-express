"""What the model provider says about the account: credit left and spend, so the operator decides when to top up or stop.

Core polls the provider every `poll_s` and keeps the newest figures in memory and on disk (a restart shows the last ones, marked with
their age). Nothing here limits anything: the figures are shown in the Console and on the Missions page, and one Telegram alert goes
out when credit drops below `warn_below` (a second below `low_below`); the alert re-arms only when credit is back above `warn_below`.

Sources:
  * OpenRouter (upstream_host openrouter.ai): `GET /api/v1/credits` (account credit bought and used) and `GET /api/v1/key` (this
    key's spend today, this week, this month, and its own limit if it has one), read with the broker's key.
  * Anthropic direct: the organisation's cost report (`GET /v1/organizations/cost_report`), only with an Admin API key named by
    `usage.admin_key_env`. Anthropic gives no credit balance to an API; spend only.
  * Anything else: "no source yet".

No key or response body is ever logged or stored: only the numbers taken out of the reply, and an error is its HTTP status or
exception class.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

log = logging.getLogger(__name__)

MAX_REPLY = 1_000_000            # bytes of one provider reply
TIMEOUT_S = 15.0
ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MAX_COST_PAGES = 4               # 31 daily buckets per page: a month and a week fit in two
LEVEL_RANK = {None: 0, "warn": 1, "low": 2}

Fetch = Callable[[str, str, Mapping[str, str]], Any]          # (host, path_and_query, headers) -> parsed JSON


class UsageError(Exception):
    """A provider read that failed; the message is safe to show (status or exception class, never a body or key)."""


def https_json(host: str, path: str, headers: Mapping[str, str]) -> Any:
    conn = http.client.HTTPSConnection(host, timeout=TIMEOUT_S)
    try:
        conn.request("GET", path, headers=dict(headers))
        response = conn.getresponse()
        body = response.read(MAX_REPLY + 1)
        if response.status != 200:
            raise UsageError(f"the provider answered HTTP {response.status}")
        if len(body) > MAX_REPLY:
            raise UsageError("the provider's reply was too large")
        try:
            return json.loads(body)
        except ValueError:
            raise UsageError("the provider's reply was not JSON") from None
    except (OSError, http.client.HTTPException) as exc:
        raise UsageError(f"could not reach the provider ({type(exc).__name__})") from None
    finally:
        conn.close()


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number and abs(number) < 1e9 else None        # not NaN, not absurd


def source_for(upstream_host: str) -> str:
    host = upstream_host.split(":", 1)[0].lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    if host == ANTHROPIC_HOST:
        return "anthropic"
    return "other"


class ProviderUsage:
    def __init__(self, *, upstream_host: str, path_prefix: str, key_env: str, usage_cfg: Any, state_path: Path,
                 environ: Mapping[str, str] | None = None, fetch: Fetch | None = None, clock: Callable[[], float] = time.time,
                 notify: Callable[[str], None] | None = None):
        self.source = source_for(upstream_host)
        self.host, self.prefix, self.key_env = upstream_host, path_prefix, key_env
        self.cfg = usage_cfg
        self.state_path = Path(state_path)
        self.environ = environ
        self._fetch = fetch or https_json
        self._clock = clock
        self._notify = notify
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._figures: dict[str, Any] | None = None       # the last good read
        self._fetched_at: float | None = None
        self._error: str | None = None
        self._alerted: str | None = None                   # the most severe level already alerted ("warn"/"low"), until re-armed
        self._load()

    # ── reading the provider ─────────────────────────────────────────────────────────────────
    def _env(self, name: str) -> str:
        return (self.environ if self.environ is not None else os.environ).get(name, "") if name else ""

    def _openrouter(self) -> dict[str, Any]:
        key = self._env(self.key_env)
        if not key:
            raise UsageError(f"the model key ({self.key_env}) is not set in Core's environment")
        headers = {"authorization": f"Bearer {key}", "accept": "application/json"}
        info = (self._fetch(self.host, f"{self.prefix}/v1/key", headers) or {}).get("data") or {}
        key_left = _money(info.get("limit_remaining")) if info.get("limit") is not None else None
        try:
            credits = (self._fetch(self.host, f"{self.prefix}/v1/credits", headers) or {}).get("data") or {}
            total, used = _money(credits.get("total_credits")), _money(credits.get("total_usage"))
            if total is None or used is None:
                raise UsageError("the provider's credit reply had no figures")
        except UsageError:
            if key_left is None:
                raise
            total = used = None                            # the account credit is unreadable; this key's own limit still counts
        credit_left = None if total is None else round(total - used, 4)
        left = key_left if credit_left is None else credit_left if key_left is None else min(credit_left, key_left)
        return {"credit_left": credit_left, "total_credits": total, "total_usage": used,
                "key_limit_left": key_left, "left": left,
                "spend": {"day": _money(info.get("usage_daily")), "week": _money(info.get("usage_weekly")),
                          "month": _money(info.get("usage_monthly")), "all": _money(info.get("usage"))},
                "spend_scope": "this key"}

    def _anthropic(self) -> dict[str, Any]:
        key = self._env(getattr(self.cfg, "admin_key_env", ""))
        if not key:
            raise UsageError("set missions.model_broker.usage.admin_key_env to an Admin API key to see Anthropic spend")
        now = dt.datetime.fromtimestamp(self._clock(), dt.timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        week_start = (now - dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = min(month_start, week_start)               # early in a month the last 7 days reach into the one before
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "accept": "application/json"}
        query: dict[str, Any] = {"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "bucket_width": "1d", "limit": 31}
        buckets: list[tuple[str, float]] = []
        partial = False
        for page in range(MAX_COST_PAGES):
            reply = self._fetch(ANTHROPIC_HOST, "/v1/organizations/cost_report?" + urlencode(query), headers) or {}
            for bucket in reply.get("data") or []:
                if not isinstance(bucket, dict):
                    continue
                cents = sum(c for c in (_money((r or {}).get("amount")) for r in bucket.get("results") or []) if c is not None)
                buckets.append((str(bucket.get("starting_at") or "")[:10], cents / 100.0))       # amounts are USD cents
            if not reply.get("has_more") or not reply.get("next_page"):
                break
            if page == MAX_COST_PAGES - 1:
                partial = True                             # more pages than expected: say the totals are short, never pass them as whole
                break
            query["page"] = reply["next_page"]
        today = now.strftime("%Y-%m-%d")
        week_from, month_from = week_start.strftime("%Y-%m-%d"), month_start.strftime("%Y-%m-%d")
        return {"credit_left": None, "total_credits": None, "total_usage": None, "key_limit_left": None, "left": None,
                "spend": {"day": round(sum(v for d, v in buckets if d == today), 4),
                          "week": round(sum(v for d, v in buckets if d >= week_from), 4),       # the last 7 days, within this month
                          "month": round(sum(v for d, v in buckets if d >= month_from), 4), "all": None},
                "spend_scope": "the organisation", "partial": partial}

    def refresh(self) -> dict[str, Any]:
        """Read the provider once; keep the last good figures if this read fails."""
        if self.source not in ("openrouter", "anthropic"):
            return self.snapshot()
        try:
            figures = self._openrouter() if self.source == "openrouter" else self._anthropic()
        except UsageError as exc:
            with self._lock:
                changed = self._error != str(exc)
                self._error = str(exc)
            if changed:
                log.warning("provider usage: %s", exc)
            return self.snapshot()
        except Exception as exc:                          # noqa: BLE001 -- a malformed reply is an error to show, never a crash
            message = f"could not read the provider's reply ({type(exc).__name__})"
            with self._lock:
                changed = self._error != message
                self._error = message
            if changed:
                log.warning("provider usage: %s", message)
            return self.snapshot()
        with self._lock:
            self._figures, self._fetched_at, self._error = figures, self._clock(), None
        self._maybe_alert(figures)
        self._save()
        return self.snapshot()

    # ── alerts ──────────────────────────────────────────────────────────────────────────────
    def _level(self, left: float | None) -> str | None:
        if left is None:
            return None
        if left < self.cfg.low_below:
            return "low"
        if left < self.cfg.warn_below:
            return "warn"
        return None

    def _maybe_alert(self, figures: dict[str, Any]) -> None:
        left = figures.get("left")
        level = self._level(left)
        with self._lock:
            before = self._alerted
            if LEVEL_RANK[level] > LEVEL_RANK[before]:
                self._alerted = level
                send = True
            else:
                if level is None:
                    self._alerted = None                   # back above warn_below: the next drop alerts again
                elif LEVEL_RANK[level] < LEVEL_RANK[before]:
                    self._alerted = level                  # a part top-up: a later drop to `low` alerts again
                send = False
        if not send or self._notify is None:
            return
        name = "OpenRouter" if self.source == "openrouter" else "the model provider"
        if level == "low":
            text = (f"🔴 Model credit nearly gone: ${left:,.2f} left on {name} (red below ${self.cfg.low_below:,.2f}). "
                    "Console turns and missions will fail when it runs out. Nothing has been stopped.")
        else:
            text = (f"💳 Model credit is getting low: ${left:,.2f} left on {name} (amber below ${self.cfg.warn_below:,.2f}). "
                    "Nothing has been stopped; top up when you choose.")
        try:
            self._notify(text)
        except Exception:                                  # noqa: BLE001 -- an alert that cannot be sent is retried at the next drop
            log.warning("provider usage: could not send the low-credit alert")
            with self._lock:
                self._alerted = before

    # ── the view ────────────────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            figures = dict(self._figures) if self._figures else None
            fetched_at, error = self._fetched_at, self._error
        now = self._clock()
        out: dict[str, Any] = {"source": self.source, "available": figures is not None, "error": error,
                               "warn_below": self.cfg.warn_below, "low_below": self.cfg.low_below,
                               "fetched_at": fetched_at, "age_s": None if fetched_at is None else max(0, int(now - fetched_at))}
        if self.source == "other":
            out["note"] = "No usage source for this provider yet."
        if figures is None:
            out["state"] = "unknown"
            return out
        out.update(figures)
        out["stale"] = out["age_s"] is not None and out["age_s"] > 3 * self.cfg.poll_s
        level = self._level(figures.get("left"))
        out["state"] = "low" if level == "low" else "warn" if level == "warn" else "ok"
        return out

    # ── persistence ─────────────────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or saved.get("source") != self.source:
            return
        figures, fetched_at = saved.get("figures"), saved.get("fetched_at")
        if isinstance(figures, dict) and isinstance(fetched_at, (int, float)):
            self._figures, self._fetched_at = figures, float(fetched_at)
        if saved.get("alerted") in ("warn", "low"):
            self._alerted = saved["alerted"]

    def _save(self) -> None:
        with self._lock:
            data = {"source": self.source, "figures": self._figures, "fetched_at": self._fetched_at, "alerted": self._alerted}
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("provider usage: could not save %s (%s)", self.state_path, type(exc).__name__)

    # ── the poller ──────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None or self.source not in ("openrouter", "anthropic"):
            return

        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self.cfg.poll_s)

        self._thread = threading.Thread(target=loop, name="provider-usage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_S + 1)


def build(mb_cfg: Any, *, state_dir: Path, notify: Callable[[str], None] | None = None) -> ProviderUsage | None:
    """The daemon's poller, or None when the broker or its usage display is off."""
    if not getattr(mb_cfg, "enabled", False) or not mb_cfg.usage.enabled:
        return None
    return ProviderUsage(upstream_host=mb_cfg.upstream_host, path_prefix=mb_cfg.upstream_path_prefix, key_env=mb_cfg.key_env,
                         usage_cfg=mb_cfg.usage, state_path=Path(state_dir) / "provider_usage.json", notify=notify)


__all__ = ["ProviderUsage", "UsageError", "build", "https_json", "source_for"]
