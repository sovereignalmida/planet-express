"""Brokered integrations: `tool.invoke` (contract 8, brief 6.3).

A tool is `(name, params model, handler, risk, secret_ref)`. The session passes a tool name and params over the worker
socket; Core checks the grant, injects the application key, calls the application, and returns REDACTED, BOUNDED
output. The key lives only in Core's configuration: it is never in the session env, cwd, readable files, any event or any
response. Every call (and every refusal) is recorded as a `tool` event.

Gates, in order, all fail closed with the same uniform refusal to the caller (the reason goes in the event):
    the session's mission exists and is `investigating` or `executing`, and its endpoints are not revoked (a paused
    mission has none) -> the grant names the tool as `tool:<name>` -> the tool is a read (write tools stay gated until
    slice 7) -> params parse strictly -> every scope field is inside the grant's scope and not protected -> the tool-call
    budget has room -> the secret resolves.
Adding a tool is a reviewed capability (listed in CLAUDE.md's v4 surface).
"""

import json
import logging
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planet_express.core.redact import redact
from planet_express.integrations.rpc import RpcError
from planet_express.missions.events import redact_value
from planet_express.missions.models import Grant
from planet_express.missions.resources import BadResource, canon, classify
from planet_express.missions.store import MissionStore, grant_resources

ACTOR = "tool-broker"
REFUSAL_CAP = 20
log = logging.getLogger(__name__)
MAX_OUTPUT_BYTES = 32 * 1024
MAX_HTTP_BYTES = 1 << 20
HTTP_TIMEOUT_S = 5
MAX_PARAM_EVENT_BYTES = 2048
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
LIVE_STATES = ("investigating", "executing")


class ToolRefused(Exception):
    """Internal: why a call was refused. The caller only ever sees the uniform refusal."""


class HttpFn(Protocol):
    def __call__(self, method: str, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]: ...


def default_http(method: str, url: str, headers: Mapping[str, str], timeout: float = HTTP_TIMEOUT_S) -> tuple[int, bytes]:
    """GET-only, no redirects, bounded body. The caller built `url` from Core's own site configuration."""
    if method != "GET" or urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ToolRefused("only http(s) GET is supported")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):          # a redirect could carry the key to another host
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with opener.open(request, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_HTTP_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_HTTP_BYTES)


@dataclass(frozen=True)
class ToolSite:
    """Where a tool's application lives: names to base URLs. Core's configuration, never the session's."""
    urls: Mapping[str, str] = field(default_factory=dict)


@dataclass
class ToolContext:
    secret: str | None
    site: ToolSite
    http: HttpFn
    runner: Callable[[list[str], int], tuple[int, str, str]]
    stacks_root: str


@dataclass(frozen=True)
class Tool:
    name: str
    params: type[BaseModel]
    handler: Callable[[Any, ToolContext], tuple[int, str]]    # -> (application status, body text)
    risk: Literal["read", "write"]
    secret_ref: str | None
    scope: Mapping[str, str] = field(default_factory=dict)    # param field -> "stack" | "service"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TraefikReadParams(_P):
    what: Literal["routers", "services", "overview"]


class ComposeDryRunParams(_P):
    stack: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class AppHealthParams(_P):
    service: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ArrQueueParams(_P):
    app: Literal["sonarr", "radarr", "lidarr", "readarr"]
    id: int = Field(ge=0, le=2**31)


class DnsRewriteParams(_P):
    server: Literal["primary", "secondary"]
    domain: str = Field(pattern=r"^[A-Za-z0-9*][A-Za-z0-9.*_-]{0,253}$")
    answer: str = Field(max_length=253)
    action: Literal["add", "remove"]


def _url(site: ToolSite, key: str, path: str) -> str:
    base = site.urls.get(key)
    if not base:
        raise ToolRefused(f"no site configured for {key}")
    return base.rstrip("/") + path


def _get(ctx: ToolContext, url: str, headers: dict[str, str]) -> tuple[int, str]:
    status, body = ctx.http("GET", url, headers, HTTP_TIMEOUT_S)
    return status, body[:MAX_HTTP_BYTES].decode("utf-8", errors="replace")


def _traefik_read(p: TraefikReadParams, ctx: ToolContext) -> tuple[int, str]:
    path = {"routers": "/api/http/routers", "services": "/api/http/services", "overview": "/api/overview"}[p.what]
    headers = {"Authorization": ctx.secret} if ctx.secret else {}
    return _get(ctx, _url(ctx.site, "default", path), headers)


def _compose_file(stacks_root: str, stack: str) -> str:
    root = Path(stacks_root).resolve()
    for name in COMPOSE_NAMES:
        candidate = root / stack / name
        real = candidate.resolve()
        if real.is_file() and real.parent == (root / stack).resolve() and (root in real.parents):
            return str(real)
    raise ToolRefused(f"{stack}: no compose file")


def _compose_dry_run(p: ComposeDryRunParams, ctx: ToolContext) -> tuple[int, str]:
    """Validates the stack's compose file (`compose config --quiet`: nothing is created, nothing is resolved into the
    output). Exit 0 is HTTP-like 200."""
    argv = ["docker", "compose", "-f", _compose_file(ctx.stacks_root, p.stack), "config", "--quiet"]
    rc, out, err = ctx.runner(argv, 10)
    return (200 if rc == 0 else 422), (out or err or "ok")


def _health_get(p: AppHealthParams, ctx: ToolContext) -> tuple[int, str]:
    return _get(ctx, _url(ctx.site, p.service, ""), {})


def _arr_queue(p: ArrQueueParams, ctx: ToolContext) -> tuple[int, str]:
    return _get(ctx, _url(ctx.site, p.app, f"/api/v3/queue/{int(p.id)}"), {"X-Api-Key": ctx.secret or ""})


def _dns_rewrite(p: DnsRewriteParams, ctx: ToolContext) -> tuple[int, str]:     # pragma: no cover - gated until slice 7
    raise ToolRefused("dns.rewrite is gated until slice 7")


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("traefik.read", TraefikReadParams, _traefik_read, "read", "traefik-api"))
    reg.register(Tool("compose.dry_run", ComposeDryRunParams, _compose_dry_run, "read", None, {"stack": "stack"}))
    reg.register(Tool("app.health_get", AppHealthParams, _health_get, "read", None, {"service": "service"}))
    reg.register(Tool("arr.queue_item", ArrQueueParams, _arr_queue, "read", "arr-api"))
    reg.register(Tool("dns.rewrite", DnsRewriteParams, _dns_rewrite, "write", "dns-admin"))
    return reg


def _clip(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


class ToolBroker:
    def __init__(self, registry: ToolRegistry, *, missions: MissionStore, secrets: Callable[[str], str | None],
                 sites: Mapping[str, ToolSite], stacks_root: str, protected: Collection[str] = (),
                 http: HttpFn = default_http, runner: Callable[[list[str], int], tuple[int, str, str]] | None = None,
                 write_gate: Callable[[str, str, dict], bool] | None = None, max_output_bytes: int = MAX_OUTPUT_BYTES,
                 refusal_cap: int = REFUSAL_CAP, on_refusal_cap: Callable[[Any], None] | None = None):
        self.registry = registry
        self.ms = missions
        self.secrets = secrets
        self.sites = dict(sites)
        self.stacks_root = stacks_root
        self.protected = tuple(protected)
        self.http = http
        self.runner = runner or _default_runner
        self.write_gate = write_gate        # slice 7 supplies it; until then every write tool is refused
        self.max_output_bytes = max_output_bytes
        self.refusal_cap = refusal_cap
        self.on_refusal_cap = on_refusal_cap
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    # ── endpoint switch (the tripwire pause closes it; resume opens it) ─────
    def revoke(self, mission_id: str) -> None:
        with self._lock:
            self._revoked.add(mission_id)

    def restore(self, mission_id: str) -> None:
        with self._lock:
            self._revoked.discard(mission_id)

    # ── the call ────────────────────────────────────────────────────────────
    def invoke(self, session: Any, name: str, params: dict) -> dict:
        mission_id, sid = session.mission_id, str(getattr(session, "id", ""))
        if self._count(mission_id, "tool.refusal_cap", sid)[0]:
            raise RpcError("Refused", "unauthorized")      # a session that hit the cap gets nothing, and nothing is written
        try:
            return self._invoke(session, name, params)
        except ToolRefused as exc:
            self._refused(mission_id, sid, name, params, str(exc)[:200])
            raise RpcError("Refused", "unauthorized") from None
        except _BadParams as exc:
            self._refused(mission_id, sid, name, params, f"bad params: {exc}"[:200])
            raise RpcError("Invalid params", "bad_params") from None

    def core_read(self, mission_id: str, name: str, params: dict) -> dict:
        """A READ-ONLY tool call made by Core itself for a verification `query` check (slice 8). It is not a session
        call: no session token and no use of the session's call budget (events are `tool.verify`, which `_count` does not
        add up). Everything else is the SAME gate a session call passes (`_gate`): the grant must name `tool:<name>`, the
        endpoint switch must be open (a tripwire pause closes it), the scope fields must be inside the grant and clear of
        protected resources. On top of that the mission must be `verifying` and the tool must be `risk == "read"`. Any
        refusal raises ToolRefused or _BadParams to the caller, who fails the check."""
        mission, tool, _grant, parsed = self._gate(mission_id, name, params, states=("verifying",), read_only=True)
        status, text, truncated = self._call(tool, parsed, name)
        ok = 200 <= status < 300
        self._record(mission.id, "tool.verify", name, params, ok=ok, status=status, nbytes=len(text.encode()),
                     truncated=truncated)
        return {"ok": ok, "status": status, "output": text if ok else "", "truncated": truncated}

    def _refused(self, mission_id: str, sid: str, name: str, params: dict, reason: str) -> None:
        """Record a refusal, up to `refusal_cap` per session. The call that reaches the cap records one more event and
        fires `on_refusal_cap` (the supervisor freezes the session); from then on the session is refused outright."""
        used = self._count(mission_id, "tool.refused", sid)[0]
        if used >= self.refusal_cap:
            return
        self._record(mission_id, "tool.refused", name, params, reason=reason, session=sid)
        if used + 1 >= self.refusal_cap:
            self._record(mission_id, "tool.refusal_cap", name, {}, reason=f"{self.refusal_cap} refused calls", session=sid)
            if self.on_refusal_cap is not None:
                try:
                    self.on_refusal_cap(_SessionRef(sid, mission_id))
                except Exception:
                    log.exception("the refusal-cap hook failed for session %s", sid)

    def _gate(self, mission_id: str, name: str, params: dict, *, states: tuple[str, ...],
              read_only: bool) -> tuple[Any, Tool, Grant, BaseModel]:
        """The one gate both a session call (`_invoke`) and a Core verification read (`core_read`) pass, so the two
        cannot drift: mission live in `states`, endpoints not closed by a tripwire pause, tool known, named in the grant,
        write tools gated, params parse, scope inside the grant."""
        try:
            mission = self.ms.get_mission(mission_id)
        except LookupError:
            raise ToolRefused("no such mission") from None
        if mission.status not in states or mission.id in self._revoked:
            raise ToolRefused(f"endpoints are closed ({mission.status})")
        tool = self.registry.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolRefused("unknown tool")
        grant = mission.grant()
        if f"tool:{name}" not in grant.credentials:
            raise ToolRefused("the grant does not name this tool")
        if tool.risk == "write" and (read_only or self.write_gate is None or not self.write_gate(mission.id, name, params)):
            raise ToolRefused("write tools are gated until slice 7")
        try:
            parsed = tool.params.model_validate(params)
        except (ValidationError, ValueError, TypeError) as exc:
            raise _BadParams(type(exc).__name__) from None
        self._check_scope(tool, parsed, grant)
        return mission, tool, grant, parsed

    def _call(self, tool: Tool, parsed: BaseModel, name: str) -> tuple[int, str, bool]:
        """Fetch the secret, run the handler, scrub and clip the answer: (status, text, truncated)."""
        secret = None
        if tool.secret_ref:
            secret = self.secrets(tool.secret_ref)
            if not secret:
                raise ToolRefused("the tool's secret is not configured")
        ctx = ToolContext(secret, self.sites.get(name, ToolSite()), self.http, self.runner, self.stacks_root)
        try:
            status, text = tool.handler(parsed, ctx)
        except ToolRefused:
            raise
        except Exception as exc:               # noqa: BLE001 - an application failure is a result, never a crash
            text, status = f"{type(exc).__name__}", 599
        if secret:
            text = text.replace(secret, "[REDACTED]")
        text, truncated = _clip(redact(text), self.max_output_bytes)
        return status, text, truncated

    def _invoke(self, session: Any, name: str, params: dict) -> dict:
        mission, tool, grant, parsed = self._gate(session.mission_id, name, params, states=LIVE_STATES, read_only=False)
        used = self._calls_used(mission.id)
        if used >= grant.budgets.tool_calls:
            raise ToolRefused("tool-call budget exhausted")
        if self._output_used(mission.id) >= grant.budgets.output_bytes:
            raise ToolRefused("output budget exhausted")
        status, text, truncated = self._call(tool, parsed, name)
        ok = 200 <= status < 300
        self._record(mission.id, "tool.invoke", name, params, ok=ok, status=status, nbytes=len(text.encode()),
                     truncated=truncated)
        result: dict[str, Any] = {"ok": ok, "status": status, "output": text if ok else "", "truncated": truncated}
        if not ok:
            result["error"] = "the application did not accept the request"
        return result

    def _check_scope(self, tool: Tool, parsed: BaseModel, grant: Grant) -> None:
        try:
            scope = [canon(r) for r in grant_resources(grant)]
            protected = [*self.protected, *grant.protected.resources]
            protected = [canon(p) for p in protected] + list(grant.enforced_exclusions())
        except BadResource:
            raise ToolRefused("malformed grant resources") from None
        for field_name, kind in tool.scope.items():
            value = getattr(parsed, field_name)
            try:
                resource = canon(f"{kind}:{value}")
            except BadResource:
                raise _BadParams(field_name) from None
            if classify(resource, scope, protected, stacks_root=self.stacks_root,
                        parent_of_protected=False) != "in_scope":
                raise ToolRefused(f"{field_name} is outside the grant's scope or protected")

    # ── records and budgets ─────────────────────────────────────────────────
    def _record(self, mission_id: str, event: str, name: str, params: dict, **extra: Any) -> None:
        shown = redact_value(params if isinstance(params, dict) else {})
        text = json.dumps(shown, sort_keys=True)
        if len(text) > MAX_PARAM_EVENT_BYTES:
            shown = {"truncated": True, "bytes": len(text)}
        body = {"event": event, "tool": str(name)[:64], "params": shown, **{k: v for k, v in extra.items() if v is not None}}
        if "nbytes" in body:
            body["bytes"] = body.pop("nbytes")
        try:
            self.ms.append_event(mission_id, "tool", ACTOR, body)
        except (LookupError, sqlite3.IntegrityError):   # the mission does not exist: nothing to attach it to
            pass

    def _count(self, mission_id: str, event: str, session: str | None = None) -> tuple[int, int]:
        """(rows, sum of `bytes`) of this broker's `tool` events with this `event` name, by SQL: the log is never read
        into memory, however long the mission has run."""
        sql = ("SELECT COUNT(*), COALESCE(SUM(json_extract(body_json, '$.bytes')), 0) FROM mission_events "
               "WHERE mission_id=? AND kind='tool' AND actor=? AND json_extract(body_json, '$.event')=?")
        args: list[Any] = [mission_id, ACTOR, event]
        if session is not None:
            sql += " AND json_extract(body_json, '$.session')=?"
            args.append(session)
        with self.ms.store._connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return int(row[0]), int(row[1])

    def _calls_used(self, mission_id: str) -> int:
        return self._count(mission_id, "tool.invoke")[0]

    def _output_used(self, mission_id: str) -> int:
        return self._count(mission_id, "tool.invoke")[1]


@dataclass(frozen=True)
class _SessionRef:
    id: str
    mission_id: str


def broker_from_config(cfg: Any, *, missions: MissionStore, stacks_root: str, protected: Collection[str] = (),
                       environ: Mapping[str, str] | None = None, registry: ToolRegistry | None = None,
                       **kw: Any) -> "ToolBroker | None":
    """A broker over `missions.tool_sites` and `missions.tool_secret_env`; None when missions are off. A secret ref is
    resolved from Core's own environment at call time and is never copied anywhere else."""
    if not cfg.enabled:
        return None
    import os
    env = os.environ if environ is None else environ
    sites = {tool: ToolSite(urls=dict(urls)) for tool, urls in cfg.tool_sites.items()}
    refs = dict(cfg.tool_secret_env)

    def secret(ref: str) -> str | None:
        var = refs.get(ref)
        return (env.get(var) or None) if var else None

    return ToolBroker(registry or default_registry(), missions=missions, secrets=secret, sites=sites,
                      stacks_root=stacks_root, protected=protected, **kw)


class _BadParams(Exception):
    pass


def _default_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
    import casa_bender as bender
    return bender.run_argv(argv, timeout)


__all__ = [
    "ACTOR",
    "Tool",
    "ToolBroker",
    "ToolContext",
    "ToolRefused",
    "ToolRegistry",
    "ToolSite",
    "broker_from_config",
    "default_http",
    "default_registry",
]
