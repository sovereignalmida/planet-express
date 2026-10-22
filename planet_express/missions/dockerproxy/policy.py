"""What the Docker proxy serves (contract 6.1). Deny by default: a request is served only if its API version, method and
endpoint are listed, every query key and every body field is listed, and every object it names resolves (through the
daemon, by label) inside the session's scope. The result is a `Plan`: the upstream request REBUILT from validated parts,
never the client's bytes. Pure apart from the `Inspector` it is given and the filesystem checks on bind sources."""

import json
import os
import posixpath
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from planet_express.core.redact import REDACTED
from planet_express.missions.events import _is_secret_key, redact_value
from planet_express.missions.models import Grant
from planet_express.missions.resources import (
    BadResource,
    canon,
    classify,
    contains,
    split,
)
from planet_express.missions.store import grant_resources

from .httpwire import BadRequest, Head, Refusal, strict_json

API_FLOOR, API_MAX = 41, 48              # the API minors this proxy has been written against (inclusive)


def served_versions(daemon_minor: int | None = None) -> frozenset[str]:
    """`v1.<floor>` .. `v1.<min(daemon, proxy max)>`: never newer than the daemon speaks or than this proxy understands. A daemon
    whose version could not be read is served up to the proxy's own maximum."""
    top = API_MAX if daemon_minor is None else min(daemon_minor, API_MAX)
    return frozenset(f"v1.{n}" for n in range(API_FLOOR, top + 1))


API_VERSIONS = served_versions()
JSON_CAP = 256 * 1024
ARCHIVE_CAP = 32 * 1024 * 1024
CREATED_LABEL = "io.planetexpress.created-by-proxy"      # put on every container a worker creates; its mounts are re-checked at start
PROJECT_LABEL, SERVICE_LABEL = "com.docker.compose.project", "com.docker.compose.service"

DANGEROUS_CAPS = frozenset({"ALL", "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_RAWIO", "SYS_BOOT", "DAC_READ_SEARCH",
                           "MAC_ADMIN", "BPF", "PERFMON"})
DOCKER_SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock")
SYSTEM_DIRS = ("/etc", "/root", "/home", "/var/lib/docker", "/var/lib/containerd", "/proc", "/sys", "/boot", "/dev", "/usr",
               "/bin", "/sbin", "/lib", "/lib64", "/var/run", "/run", "/var/spool", "/var/lib/dpkg")
CAP_ALLOW = frozenset({"CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID", "NET_BIND_SERVICE"})
SECURITY_OPT_ALLOW = frozenset({"no-new-privileges", "no-new-privileges:true"})
BUILTIN_NETWORKS = frozenset({"bridge", "none", "default"})
BIND_OPTS = frozenset({"ro", "rw", "z", "Z", "rprivate", "private", "nocopy"})
DOCKER_SOCKET_NAMES = frozenset({"docker.sock", "containerd.sock", "podman.sock", "docker.pid"})
RESTART_NAMES = frozenset({"", "no", "always", "unless-stopped", "on-failure"})
LOG_DRIVERS = frozenset({"json-file", "local", "journald"})
FILTER_KEYS = frozenset({"status", "name", "label", "id", "ancestor", "network", "volume", "health", "exited", "before",
                         "since", "expose", "publish", "dangling", "reference", "driver", "scope", "type", "event",
                         "container", "image", "until"})

_NEEDS_NO_ROOT_EQUIVALENT = ("exec.create", "container.attach", "container.archive.put", "container.start", "container.restart")
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_IMG = r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,254}"
_NAME = re.compile(rf"^{_ID}$")
_IMAGE = re.compile(rf"^{_IMG}$")
_SIGNAL = re.compile(r"^(SIG)?[A-Z0-9]{1,12}$")
_TIME = re.compile(r"^[0-9A-Za-z:.+-]{1,40}$")
_NUM = re.compile(r"^[0-9]{1,12}$")
_PLATFORM = re.compile(r"^[a-z0-9/._-]{1,40}$")
_DETACH = re.compile(r"^[A-Za-z0-9,@\[\]\\^_-]{0,20}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}=")
_PORTKEY = re.compile(r"^[0-9]{1,5}(/(tcp|udp|sctp))?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SIZE = 1 << 62


class Inspector(Protocol):
    def container(self, ref: str) -> "ContainerInfo | None": ...
    def network(self, ref: str) -> "NetworkInfo | None": ...
    def volume(self, name: str) -> dict | None: ...
    def image_exists(self, ref: str) -> bool: ...


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    labels: Mapping[str, str]
    host_config: Mapping = field(default_factory=dict, compare=False, hash=False)    # from inspect: Binds, Privileged, ...
    mounts: tuple = field(default=(), compare=False, hash=False)                      # from inspect: the container's mounts


@dataclass(frozen=True)
class NetworkInfo:
    id: str
    name: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Scope:
    """What the session may touch, as canonical ids (contract 2.3)."""
    resources: tuple[str, ...]            # stacks, services, mounts and paths of the grant
    paths: tuple[str, ...]                # only the paths and mounts: where a bind source may be
    protected: tuple[str, ...]
    host: str = "local"
    stacks_root: str | None = None
    protected_gids: frozenset[int] = frozenset({0})

    @classmethod
    def from_grant(cls, grant: Grant, *, protected=(), stacks_root: str | None = None,
                   protected_gids=()) -> "Scope":
        resources = tuple(canon(r) for r in grant_resources(grant))
        paths = tuple(r for r in resources if r.split("/", 1)[1].startswith(("path:", "mount:")))
        prot = tuple(dict.fromkeys((*(canon(r) for r in (*protected, *grant.protected.resources)),
                                    *grant.enforced_exclusions())))
        return cls(resources, paths, prot, grant.scope.hosts[0] if grant.scope.hosts else "local", stacks_root,
                   frozenset({0, *protected_gids}))

    def classify(self, resource: str, *, parent_of_protected: bool = True) -> str:
        return classify(resource, self.resources, self.protected, stacks_root=self.stacks_root,
                        parent_of_protected=parent_of_protected)

    def classify_path(self, resource: str) -> str:
        return classify(resource, self.paths, self.protected, stacks_root=self.stacks_root)


def compose_id(kind: str, labels: Mapping[str, str], host: str) -> str | None:
    """`service:<project>/<service>` (kind service) or `stack:<project>` of a compose-labelled object, else None."""
    project, service = labels.get(PROJECT_LABEL), labels.get(SERVICE_LABEL)
    try:
        if kind == "service":
            return canon(f"service:{project}/{service}", host) if project and service else None
        return canon(f"stack:{project}", host) if project else None
    except BadResource:
        return None


# ── routes ────────────────────────────────────────────────────────────────────────────────────────

def _flag(value: str) -> str:
    if value not in ("0", "1", "true", "false"):
        raise BadRequest("bad flag value")
    return value


def _uint(limit: int) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not _NUM.match(value) or int(value) > limit:
            raise BadRequest("bad number")
        return value
    return check


def _regex(pattern: re.Pattern) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.match(value):
            raise BadRequest("bad query value")
        return value
    return check


def _tail(value: str) -> str:
    if value != "all" and not (_NUM.match(value) and int(value) <= 100000):
        raise BadRequest("bad tail")
    return value


def _path(value: str) -> str:
    if not value or len(value) > 4096 or "\x00" in value:
        raise BadRequest("bad path")
    return value


def _filters(value: str) -> str:
    """A Docker `filters` JSON object, rebuilt compactly: known keys, string leaves, bounded."""
    doc = strict_json(value.encode("utf-8"), cap=2048)
    out: dict[str, Any] = {}
    count = 0
    for key, item in doc.items():
        if key not in FILTER_KEYS:
            raise Refusal("unknown filter")
        if isinstance(item, list) and all(isinstance(v, str) and len(v) <= 200 for v in item) or isinstance(item, dict) and all(isinstance(k, str) and len(k) <= 200 and isinstance(v, bool)
                                            for k, v in item.items()):
            out[key] = item
            count += len(item)
        else:
            raise BadRequest("bad filter value")
    if count > 32:
        raise BadRequest("too many filter values")
    return json.dumps(out, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class Route:
    name: str
    methods: tuple[str, ...]
    pattern: re.Pattern
    write: bool = False
    query: Mapping[str, Callable[[str], str]] = field(default_factory=dict)
    body: str | None = None                       # "json", "raw" or None
    target: str | None = None                     # what group `id` names: container, network, volume, image, exec
    need: str = "none"                            # container/network access: "scope" (in scope) or "visible" (not protected)
    stream: bool = False
    hijack: bool = False
    json_response: bool = False
    readonly_ok: bool = False


def _r(path: str) -> re.Pattern:
    return re.compile("^" + path.replace("{id}", f"(?P<id>{_ID})").replace("{img}", f"(?P<id>{_IMG})") + "$")


_Q = {
    "all": _flag, "size": _flag, "limit": _uint(1000), "filters": _filters, "follow": _flag, "stdout": _flag, "stderr": _flag,
    "since": _regex(_TIME), "until": _regex(_TIME), "timestamps": _flag, "tail": _tail, "stream": _flag, "one-shot": _flag,
    "path": _path, "name": _regex(_NAME), "platform": _regex(_PLATFORM), "t": _uint(3600), "signal": _regex(_SIGNAL),
    "force": _flag, "v": _flag, "detachKeys": _regex(_DETACH), "logs": _flag, "stdin": _flag, "verbose": _flag,
    "digests": _flag, "noOverwriteDirChown": _flag, "copyUIDGID": _flag,
}


def _q(*keys: str) -> dict[str, Callable[[str], str]]:
    return {k: _Q[k] for k in keys}


ROUTES: tuple[Route, ...] = (
    Route("ping", ("GET", "HEAD"), _r("/_ping"), readonly_ok=True),
    Route("containers.list", ("GET",), _r("/containers/json"), query=_q("all", "limit", "size", "filters"),
          json_response=True, readonly_ok=True),
    Route("container.inspect", ("GET",), _r("/containers/{id}/json"), query=_q("size"), target="container", need="visible",
          json_response=True, readonly_ok=True),
    Route("container.logs", ("GET",), _r("/containers/{id}/logs"),
          query=_q("follow", "stdout", "stderr", "since", "until", "timestamps", "tail"), target="container", need="scope",
          stream=True),
    Route("container.stats", ("GET",), _r("/containers/{id}/stats"), query=_q("stream", "one-shot"), target="container",
          need="scope", stream=True),
    Route("container.archive.get", ("GET", "HEAD"), _r("/containers/{id}/archive"), query=_q("path"), target="container",
          need="scope", stream=True),
    Route("images.list", ("GET",), _r("/images/json"), query=_q("all", "filters", "digests"), json_response=True,
          readonly_ok=True),
    Route("image.inspect", ("GET",), _r("/images/{img}/json"), target="image", json_response=True, readonly_ok=True),
    Route("networks.list", ("GET",), _r("/networks"), query=_q("filters"), json_response=True, readonly_ok=True),
    Route("network.inspect", ("GET",), _r("/networks/{id}"), query=_q("verbose"), target="network", need="visible",
          json_response=True, readonly_ok=True),
    Route("volumes.list", ("GET",), _r("/volumes"), query=_q("filters"), json_response=True, readonly_ok=True),
    Route("volume.inspect", ("GET",), _r("/volumes/{id}"), target="volume", json_response=True, readonly_ok=True),
    Route("events", ("GET",), _r("/events"), query=_q("since", "until", "filters"), stream=True, readonly_ok=True),
    Route("exec.inspect", ("GET",), _r("/exec/{id}/json"), target="exec", json_response=True),
    Route("container.create", ("POST",), _r("/containers/create"), write=True, query=_q("name", "platform"), body="json",
          json_response=True),
    Route("container.start", ("POST",), _r("/containers/{id}/start"), write=True, query=_q("detachKeys"), target="container",
          need="scope"),
    Route("container.stop", ("POST",), _r("/containers/{id}/stop"), write=True, query=_q("t", "signal"), target="container",
          need="scope"),
    Route("container.restart", ("POST",), _r("/containers/{id}/restart"), write=True, query=_q("t", "signal"),
          target="container", need="scope"),
    Route("container.kill", ("POST",), _r("/containers/{id}/kill"), write=True, query=_q("signal"), target="container",
          need="scope"),
    Route("container.rename", ("POST",), _r("/containers/{id}/rename"), write=True, query=_q("name"), target="container",
          need="scope"),
    Route("container.update", ("POST",), _r("/containers/{id}/update"), write=True, body="json", target="container",
          need="scope", json_response=True),
    Route("container.remove", ("DELETE",), _r("/containers/{id}"), write=True, query=_q("force", "v"), target="container",
          need="scope"),
    Route("container.archive.put", ("PUT",), _r("/containers/{id}/archive"), write=True,
          query=_q("path", "noOverwriteDirChown", "copyUIDGID"), body="raw", target="container", need="scope"),
    Route("exec.create", ("POST",), _r("/containers/{id}/exec"), write=True, body="json", target="container", need="scope",
          json_response=True),
    Route("exec.start", ("POST",), _r("/exec/{id}/start"), write=True, body="json", target="exec", hijack=True, stream=True),
    Route("container.attach", ("POST",), _r("/containers/{id}/attach"), write=True,
          query=_q("detachKeys", "logs", "stream", "stdin", "stdout", "stderr"), target="container", need="scope",
          hijack=True, stream=True),
    Route("network.connect", ("POST",), _r("/networks/{id}/connect"), write=True, body="json", target="network", need="scope"),
    Route("network.disconnect", ("POST",), _r("/networks/{id}/disconnect"), write=True, body="json", target="network",
          need="scope"),
)


@dataclass
class Plan:
    route: Route
    method: str
    upstream_path: str                       # without the version prefix
    query: dict[str, str]
    body: bytes | None = None                # re-serialised JSON
    raw_length: int = 0                      # a raw body (archive put), streamed
    targets: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    exec_container: str | None = None        # exec.create: the container id the exec belongs to


# ── field rules ───────────────────────────────────────────────────────────────────────────────────

def _int(value, lo=0, hi=_SIZE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise BadRequest("bad number field")
    return value


def _str(value, limit=512, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (pattern and not pattern.match(value)):
        raise BadRequest("bad string field")
    return value


def _bool(value) -> bool:
    if not isinstance(value, bool):
        raise BadRequest("bad boolean field")
    return value


def _strlist(value, limit=256, item=8192) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise BadRequest("bad list field")
    return [_str(v, item) for v in value]


def _empty(value) -> bool:
    return value in (None, [], {}, "", False, 0)


class Policy:
    """One per session (or one for the read-only variant, with no scope). Raises `Refusal`; never returns a partial plan."""

    def __init__(self, scope: Scope | None, inspector: Inspector, *, read_only: bool = False,
                 exec_owner: Callable[[str], str | None] = lambda exec_id: None,
                 json_cap: int = JSON_CAP, archive_cap: int = ARCHIVE_CAP,
                 versions: frozenset[str] = API_VERSIONS, is_socket: Callable[[str], bool] | None = None,
                 label_allow: Iterable[str] = ()):
        if not read_only and scope is None:
            raise ValueError("a scoped policy needs a scope")
        self.scope, self.inspector, self.read_only = scope, inspector, read_only
        self.exec_owner, self.json_cap, self.archive_cap, self.versions = exec_owner, json_cap, archive_cap, versions
        self._is_socket = is_socket or _is_unix_socket
        self.label_allow = tuple(label_allow)

    # -- matching -------------------------------------------------------------------------------

    def match(self, head: Head) -> tuple[Route, dict[str, str]]:
        if not self.versions:
            raise Refusal("the daemon's API version is older than this proxy supports")
        if head.version is not None and head.version not in self.versions:
            raise Refusal("unknown API version")
        wrong_method = False
        for route in ROUTES:
            found = route.pattern.match(head.path)
            if found is None:
                continue
            if head.method not in route.methods:
                wrong_method = True                       # another route may share the path (archive get / put)
                continue
            if self.read_only and not route.readonly_ok:
                raise Refusal("this proxy is read-only: endpoint not served")
            if head.version is None and route.name != "ping":
                raise Refusal("an API version is required")
            return route, found.groupdict()
        raise Refusal("method not allowed on this endpoint" if wrong_method else "endpoint not served")

    def clean_query(self, route: Route, raw: Mapping[str, str]) -> dict[str, str]:
        out = {}
        for key, value in raw.items():
            check = route.query.get(key)
            if check is None:
                raise Refusal("query parameter not served")         # includes every `remote`, `fromSrc`, `fromImage`, `link`
            out[key] = check(value)
        if route.name == "container.remove" and out.get("v") in ("1", "true"):
            raise Refusal("removing anonymous volumes is not served")
        if route.name in ("container.archive.get", "container.archive.put") and "path" not in out:
            raise BadRequest("archive needs a path")
        return out

    def check_upgrade(self, route: Route, head: Head) -> bool:
        """True if the client asked for a hijacked connection (only exec start and attach may)."""
        upgrade, connection = head.headers.get("upgrade"), head.headers.get("connection", "")
        tokens = {t.strip().lower() for t in connection.split(",") if t.strip()}
        if upgrade is None and "upgrade" not in tokens:
            if not tokens <= {"close", "keep-alive"}:
                raise BadRequest("unsupported Connection header")
            return False
        if not route.hijack:
            raise Refusal("connection upgrade is not served on this endpoint")
        if upgrade is None or upgrade.lower() != "tcp" or tokens != {"upgrade"}:
            raise BadRequest("only `Connection: Upgrade` with `Upgrade: tcp` is served")
        return True

    # -- the plan ---------------------------------------------------------------------------------

    def plan(self, route: Route, method: str, groups: Mapping[str, str], query: dict[str, str], body: Any,
             raw_length: int = 0) -> Plan:
        plan = Plan(route, method, "", query)
        ident = groups.get("id")
        if route.target == "container":
            info = self._container(ident, route.need)
            plan.targets = [self._service_id(info)] if self._service_id(info) else []
            plan.upstream_path = self._with_suffix(route, info.id)
            plan.summary = {"container": info.name or info.id[:12]}
            if route.name == "exec.create":
                plan.exec_container = info.id
            if route.name in _NEEDS_NO_ROOT_EQUIVALENT:
                self._refuse_if_root_equivalent(info)
            if route.name in ("container.start", "container.restart") and info.labels.get(CREATED_LABEL):
                self._revalidate_mounts(info)
        elif route.target == "network":
            net = self._network(ident, route.need)
            plan.upstream_path = f"/networks/{net.id}" + ("/" + route.name.split(".")[1] if route.write else "")
            plan.summary = {"network": net.name}
        elif route.target == "volume":
            vol = self.inspector.volume(ident)
            if vol is None:
                raise Refusal("no such volume", 404)
            if not self.read_only and not self._volume_in_scope(vol):
                raise Refusal("the volume is outside this mission's scope")      # its metadata includes host paths
            plan.upstream_path = f"/volumes/{ident}"
        elif route.target == "image":
            plan.upstream_path = f"/images/{ident}/json"
        elif route.target == "exec":
            owner = self.exec_owner(ident)
            if owner is None or not _HEX64.match(ident or ""):
                raise Refusal("not an exec this session created")
            info = self._container(owner, "scope")
            if route.name == "exec.start":
                self._refuse_if_root_equivalent(info)
            plan.upstream_path = f"/exec/{ident}/" + ("start" if route.name == "exec.start" else "json")
            plan.targets = [self._service_id(info)] if self._service_id(info) else []
            plan.summary = {"exec": ident[:12], "container": info.name or info.id[:12]}
        else:
            plan.upstream_path = self._fixed_path(route)
        handler = getattr(self, "_body_" + route.name.replace(".", "_"), None)
        if route.body == "json":
            if handler is None:
                raise Refusal("endpoint not served")
            plan.body = handler(plan, body, query)
        elif route.body == "raw":
            plan.raw_length = raw_length
            plan.summary["bytes"] = raw_length if raw_length >= 0 else "chunked"
        if route.name in ("container.stop", "container.restart", "container.kill"):
            plan.summary.update({k: v for k, v in query.items() if k in ("t", "signal")})
        if route.name == "container.rename":
            plan.summary["new_name"] = query.get("name", "")
        if route.name == "container.remove":
            plan.summary["force"] = query.get("force", "0")
        if route.name.startswith("container.archive"):
            plan.summary["path"] = query["path"][:200]
        return plan

    @staticmethod
    def _fixed_path(route: Route) -> str:
        return {"ping": "/_ping", "containers.list": "/containers/json", "images.list": "/images/json",
                "networks.list": "/networks", "volumes.list": "/volumes", "events": "/events",
                "container.create": "/containers/create"}[route.name]

    @staticmethod
    def _with_suffix(route: Route, cid: str) -> str:
        suffix = {"container.inspect": "/json", "container.logs": "/logs", "container.stats": "/stats",
                  "container.archive.get": "/archive", "container.archive.put": "/archive", "container.start": "/start",
                  "container.stop": "/stop", "container.restart": "/restart", "container.kill": "/kill",
                  "container.rename": "/rename", "container.update": "/update", "container.remove": "",
                  "exec.create": "/exec", "container.attach": "/attach"}[route.name]
        return f"/containers/{cid}{suffix}"

    # -- object resolution --------------------------------------------------------------------

    def _service_id(self, info: ContainerInfo) -> str | None:
        return compose_id("service", info.labels, self.scope.host if self.scope else "local")

    @property
    def max_minor(self) -> int:
        return max((int(v[3:]) for v in self.versions), default=0)

    def container_verdict(self, info: ContainerInfo) -> str:
        scope = self.scope
        if scope is None:
            return "in_scope"
        for candidate in (self._service_id(info), _try_canon(f"container:{info.name.lstrip('/')}", scope.host)):
            if candidate and scope.classify(candidate) == "protected":
                return "protected"
        service = self._service_id(info)
        return scope.classify(service) if service else "out_of_scope"

    def _container(self, ref: str, need: str) -> ContainerInfo:
        if not _NAME.match(ref or ""):
            raise Refusal("bad container reference")
        info = self.inspector.container(ref)
        if info is None:
            raise Refusal("no such container", 404)
        verdict = self.container_verdict(info)
        if verdict == "protected":
            raise Refusal("the container is a protected resource")
        if need == "scope" and verdict != "in_scope":
            raise Refusal("the container is outside this mission's scope")
        return info

    def _network_verdict(self, info: NetworkInfo) -> str:
        scope = self.scope
        if scope is None:
            return "in_scope"
        for candidate in (compose_id("stack", info.labels, scope.host), _try_canon(f"network:{info.name}", scope.host)):
            if candidate and scope.classify(candidate, parent_of_protected=False) == "protected":
                return "protected"
        stack = compose_id("stack", info.labels, scope.host)
        return scope.classify(stack, parent_of_protected=False) if stack else "out_of_scope"

    def _network(self, ref: str, need: str) -> NetworkInfo:
        if not _NAME.match(ref or ""):
            raise Refusal("bad network reference")
        info = self.inspector.network(ref)
        if info is None:
            raise Refusal("no such network", 404)
        verdict = self._network_verdict(info)
        if verdict == "protected":
            raise Refusal("the network is a protected resource")
        if need == "scope" and verdict != "in_scope":
            raise Refusal("the network is outside this mission's scope")
        return info

    def _scoped_network_name(self, name: str) -> None:
        """A network named in a create body: a built-in bridge/none, or one that resolves in scope."""
        if name in BUILTIN_NETWORKS:
            return
        if name == "host" or name.startswith("container:"):
            raise Refusal("host and container network modes are not served")
        self._network(name, "scope")

    def filter_list(self, route_name: str, doc: Any) -> Any:
        """The volumes/networks list as the worker may see it: protected entries dropped; volumes also dropped unless in scope;
        host paths and driver options stripped from what remains (the same rules as inspect)."""
        def clean(item: dict) -> dict:
            return {k: v for k, v in item.items() if k not in ("Mountpoint", "Options")}
        host = self.scope.host if self.scope else "local"
        if route_name == "volumes.list" and isinstance(doc, dict):
            kept = []
            for vol in doc.get("Volumes") or []:
                if not isinstance(vol, dict):
                    continue
                stack = compose_id("stack", vol.get("Labels") if isinstance(vol.get("Labels"), dict) else {}, host)
                if self.scope is not None and (stack is None and not self.read_only or stack is not None and (
                        self.scope.classify(stack, parent_of_protected=False) == "protected"
                        or (not self.read_only and self.scope.classify(stack, parent_of_protected=False) != "in_scope"))):
                    continue
                kept.append(clean(vol))
            return {**doc, "Volumes": kept}
        if route_name == "networks.list" and isinstance(doc, list):
            out = []
            for net in doc:
                if not isinstance(net, dict):
                    continue
                labels = net.get("Labels") if isinstance(net.get("Labels"), dict) else {}
                info = NetworkInfo(str(net.get("Id", "")), str(net.get("Name", "")), {str(k): str(v) for k, v in labels.items()})
                if self._network_verdict(info) != "protected":
                    out.append(clean(net))
            return out
        return doc

    def _volume_in_scope(self, vol: Mapping) -> bool:
        labels = vol.get("Labels") or {}
        stack = compose_id("stack", labels if isinstance(labels, dict) else {}, self.scope.host)
        return stack is not None and self.scope.classify(stack, parent_of_protected=False) == "in_scope"

    def hazards(self, info: ContainerInfo, *, protected_mounts: bool = False) -> list[str]:
        """Every reason this container may not be acted on through the proxy (empty: it may). The approval card lists these."""
        try:
            self._refuse_if_root_equivalent(info, protected_mounts=protected_mounts)
        except Refusal as refusal:
            return [refusal.reason]
        return []

    def _refuse_if_root_equivalent(self, info: ContainerInfo, *, protected_mounts: bool = False) -> None:
        """Acting on a container (exec, attach, archive upload, start, restart; or inheriting its volumes) is root on the host
        when the container is privileged, shares the host's pid or user namespace, holds a capability that reaches the kernel,
        or has the Docker daemon's socket (or a directory holding it) mounted: the proxy's whole point is that the worker never
        reaches the daemon any other way. With `protected_mounts` (VolumesFrom) a mount of a protected path counts too. Such a
        container can still be read."""
        host = info.host_config if isinstance(info.host_config, Mapping) else {}
        if host.get("Privileged"):
            raise Refusal("a privileged container may not be acted on through the proxy")
        for key in ("PidMode", "UsernsMode", "IpcMode", "UTSMode", "CgroupnsMode"):
            if host.get(key) == "host":
                raise Refusal(f"a container with {key} host may not be acted on through the proxy")
        caps = {str(c).upper().removeprefix("CAP_") for c in (host.get("CapAdd") or []) if isinstance(c, str)}
        if caps & DANGEROUS_CAPS:
            raise Refusal("a container holding a capability that reaches the kernel may not be acted on through the proxy")
        if host.get("Devices"):
            raise Refusal("a container with host devices may not be acted on through the proxy")
        if any(isinstance(o, str) and "unconfined" in o.lower() for o in (host.get("SecurityOpt") or [])):
            raise Refusal("a container with an unconfined seccomp or apparmor profile may not be acted on through the proxy")
        sources = [m.get("Source") for m in info.mounts if isinstance(m, dict) and m.get("Type") in (None, "bind")]
        sources += [str(entry).split(":", 1)[0] for entry in (host.get("Binds") or []) if isinstance(entry, str)]
        for mount in info.mounts:
            # A named volume can be bind-backed (`type=none,o=bind,device=/var/run`): docker reports it as Type volume.
            if isinstance(mount, dict) and mount.get("Type") == "volume":
                sources += self._volume_devices(mount.get("Name"))
        for source in sources:
            if not isinstance(source, str) or not source.startswith("/"):
                continue
            if self._reaches_docker(source):
                raise Refusal("a container that has the Docker socket mounted may not be acted on through the proxy")
            if protected_mounts and self.scope is not None:
                rid = _try_canon(f"path:{posixpath.normpath(source)}", self.scope.host)
                if rid is not None and self.scope.classify(rid) == "protected":
                    raise Refusal("the container mounts a protected path, so its volumes may not be inherited")
        self._refuse_writable_system_mounts(info, host)

    def _writable_binds(self, info: ContainerInfo, host: Mapping) -> list[str]:
        """Host paths this container has bound read-write (inspect's Mounts, and HostConfig.Binds when Mounts is silent)."""
        out = [m.get("Source") for m in info.mounts
               if isinstance(m, dict) and m.get("Type") in (None, "bind") and m.get("RW", True) is not False]
        for entry in host.get("Binds") or []:
            parts = str(entry).split(":")
            if len(parts) < 3 or "ro" not in parts[2].split(","):
                out.append(parts[0])
        return [s for s in out if isinstance(s, str) and s.startswith("/")]

    def _refuse_writable_system_mounts(self, info: ContainerInfo, host: Mapping) -> None:
        """A read-write bind of a host path outside the grant is host write access once someone can run code in the container.
        Data volumes outside the grant stay allowed (the accepted residual, watched by tripwires), but not the system's own
        places (/etc, /root, home directories, /var/lib/docker, /proc, /sys, ...), nor any protected non-data path."""
        scope = self.scope
        if scope is None:
            return
        for source in self._writable_binds(info, host):
            norm = posixpath.normpath(source)
            rid = _try_canon(f"path:{norm}", scope.host)
            if rid is not None and scope.classify_path(rid) == "in_scope":
                continue                                        # inside the grant's own paths and mounts
            system = any(norm == d or norm.startswith(d.rstrip("/") + "/") or d.startswith(norm.rstrip("/") + "/")
                         for d in SYSTEM_DIRS)
            protected_path = rid is not None and any(
                split(p)[1] == "path" and contains(p, rid) or split(p)[1] == "path" and contains(rid, p) for p in scope.protected)
            if norm == "/" or system or protected_path:
                raise Refusal(f"a container with {norm[:80]} mounted read-write may not be acted on through the proxy: "
                              "that is host write access outside the grant")

    def _volume_devices(self, name) -> list[str]:
        if not isinstance(name, str) or not _NAME.match(name):
            return []
        vol = self.inspector.volume(name)
        options = vol.get("Options") if isinstance(vol, dict) else None
        device = options.get("device") if isinstance(options, dict) else None
        return [device] if isinstance(device, str) else []

    def _reaches_docker(self, source: str) -> bool:
        """Is this host path the Docker socket, a directory holding it, or a socket under another name?"""
        real = os.path.realpath(source)
        reaches = any(sock == path or sock.startswith(path.rstrip("/") + "/")
                      for path in (source, real) for sock in DOCKER_SOCKET_PATHS)
        return reaches or os.path.basename(real) in DOCKER_SOCKET_NAMES or self._is_socket(real)

    def _scoped_volume(self, name: str) -> None:
        if not _NAME.match(name):
            raise Refusal("bad volume name")
        vol = self.inspector.volume(name)
        if vol is None:
            raise Refusal("volumes are never created by a worker: the volume does not exist")
        if vol.get("Driver") != "local" or vol.get("Options"):
            raise Refusal("only a plain local volume with no driver options may be mounted")
        labels = vol.get("Labels") or {}
        stack = compose_id("stack", labels if isinstance(labels, dict) else {}, self.scope.host)
        if stack is None or self.scope.classify(stack, parent_of_protected=False) != "in_scope":
            raise Refusal("the volume is outside this mission's scope")

    def _scoped_bind(self, source: str) -> None:
        scope = self.scope
        if (not source.startswith("/") or "\x00" in source or len(source) > 4096
                or posixpath.normpath(source) != source and source != "/"):
            raise Refusal("a bind source must be an absolute normalised path")
        if os.path.basename(source) in DOCKER_SOCKET_NAMES or self._is_socket(source):
            raise Refusal("a socket may not be bind mounted")
        real = os.path.realpath(source)
        if real != source:
            raise Refusal("a bind source may not cross a symlink")
        if os.path.basename(real) in DOCKER_SOCKET_NAMES:
            raise Refusal("a socket may not be bind mounted")
        try:
            rid = canon(f"path:{source}", scope.host)
        except BadResource:
            raise Refusal("bad bind source") from None
        verdict = scope.classify_path(rid)
        if verdict == "protected":
            raise Refusal("the bind source touches a protected resource")
        if verdict != "in_scope":
            raise Refusal("the bind source is outside the grant's paths and mounts")

    def _revalidate_mounts(self, info: ContainerInfo) -> None:
        """A container a worker created through the proxy (it carries CREATED_LABEL, which only the proxy sets and which docker
        cannot change afterwards) is checked again when it is started: its bind sources were validated as paths at
        create time, and a worker-writable path can be swapped for a symlink (to the Docker socket, say) in between. This
        narrows the window from "whenever the worker likes" to the instant between this check and the daemon's mount; a
        no-follow bind in the daemon is what would close it, and the proxy cannot add that (accepted residual, reported)."""
        host = info.host_config if isinstance(info.host_config, Mapping) else {}
        for entry in host.get("Binds") or []:
            if not isinstance(entry, str):
                raise Refusal("the container's mounts cannot be checked")
            self._bind(entry, [])
        for entry in host.get("VolumesFrom") or []:           # inherited mounts are re-checked too
            if not isinstance(entry, str):
                raise Refusal("the container's mounts cannot be checked")
            self._refuse_if_root_equivalent(self._container(entry.partition(":")[0], "scope"), protected_mounts=True)
        for mount in host.get("Mounts") or []:
            if not isinstance(mount, dict) or not isinstance(mount.get("Source"), str):
                raise Refusal("the container's mounts cannot be checked")
            if mount.get("Type") == "bind":
                self._scoped_bind(mount["Source"])
            elif mount.get("Type") == "volume":
                self._scoped_volume(mount["Source"])
            else:
                raise Refusal("the container has a mount type that is not served")

    def _mount_target(self, target: str) -> str:
        if (not isinstance(target, str) or not target.startswith("/") or "\x00" in target or len(target) > 4096
                or (posixpath.normpath(target) != target and target != "/")):
            raise Refusal("a mount target must be an absolute normalised path")
        return target

    # -- bodies ----------------------------------------------------------------------------------

    def _body_container_create(self, plan: Plan, body: dict, query: dict) -> bytes:
        top_ok = {"Image", "Cmd", "Entrypoint", "Env", "WorkingDir", "User", "Labels", "Hostname", "Domainname",
                  "ExposedPorts", "Tty", "OpenStdin", "StdinOnce", "AttachStdin", "AttachStdout", "AttachStderr",
                  "Healthcheck", "StopSignal", "StopTimeout", "HostConfig", "NetworkingConfig", "MacAddress",
                  "NetworkDisabled", "ArgsEscaped"}
        extra = set(body) - top_ok
        if extra:
            raise Refusal(f"container field not served: {min(extra)[:40]}")
        out: dict[str, Any] = {}
        image = _str(body.get("Image"), 255, _IMAGE)
        if not self.inspector.image_exists(image):
            raise Refusal("the image is not present: a worker never pulls (Core does, from the registry allowlist)")
        out["Image"] = image
        for key in ("Cmd", "Entrypoint"):
            if body.get(key) is not None:
                out[key] = _strlist(body[key], 128)
        if body.get("Env") is not None:
            env = _strlist(body["Env"], 256)
            if not all(_ENV.match(e) for e in env):
                raise BadRequest("bad environment entry")
            out["Env"] = env
        for key in ("WorkingDir", "User", "Hostname", "Domainname", "StopSignal", "MacAddress"):
            if body.get(key) is not None:
                out[key] = _str(body[key], 256)
        for key in ("Tty", "OpenStdin", "StdinOnce", "AttachStdin", "AttachStdout", "AttachStderr", "NetworkDisabled",
                    "ArgsEscaped"):
            if body.get(key) is not None:
                out[key] = _bool(body[key])
        if body.get("StopTimeout") is not None:
            out["StopTimeout"] = _int(body["StopTimeout"], 0, 3600)
        labels = body.get("Labels")
        if not isinstance(labels, dict) or len(labels) > 128 or not all(
                isinstance(k, str) and isinstance(v, str) and len(k) <= 256 and len(v) <= 1024 for k, v in labels.items()):
            raise BadRequest("labels must be a bounded string map")
        service = compose_id("service", labels, self.scope.host)
        if service is None:
            raise Refusal("a new container must carry the compose project and service labels that put it in scope")
        verdict = self.scope.classify(service)
        if verdict != "in_scope":
            raise Refusal("the new container's service is protected" if verdict == "protected"
                          else "the new container's service is outside this mission's scope")
        self._check_labels(labels, service)
        out["Labels"] = {**labels, CREATED_LABEL: "1"}          # ours, not the worker's: set (or overwritten) here
        plan.targets = [service]
        if body.get("ExposedPorts") is not None:
            ports = body["ExposedPorts"]
            if not isinstance(ports, dict) or len(ports) > 64 or not all(_PORTKEY.match(str(k)) and v == {} for k, v in ports.items()):
                raise BadRequest("bad ExposedPorts")
            out["ExposedPorts"] = ports
        if body.get("Healthcheck") is not None:
            out["Healthcheck"] = self._healthcheck(body["Healthcheck"])
        if body.get("NetworkingConfig") is not None:
            out["NetworkingConfig"] = self._networking(body["NetworkingConfig"])
        mounts: list[dict] = []
        host, mount_summary = self._host_config(body.get("HostConfig") or {}, mounts)
        out["HostConfig"] = host
        plan.summary = {"image": image, "service": service, "name": query.get("name", ""),
                        "mounts": mount_summary, "cap_add": host.get("CapAdd", []),
                        "cmd_argc": len(out.get("Cmd", [])), "env_names": sorted(e.split("=", 1)[0] for e in out.get("Env", []))[:64]}
        return json.dumps(out, separators=(",", ":"), sort_keys=True).encode()

    def _check_labels(self, labels: Mapping[str, str], service: str) -> None:
        """Labels drive other components (a reverse proxy routes by `traefik.*`, an updater acts on `com.centurylinklabs.*`, a
        healer on `autoheal*`, a dashboard on `homepage.*`), so a worker may not invent them. Allowed: the compose labels, the
        proxy's own, a label the in-scope service ALREADY carries with the same value (a recreate keeps what it had), and
        whatever the operator listed in `missions.docker_label_allow`."""
        project, svc = service.rsplit(":", 1)[1].split("/", 1)
        try:
            existing = list(getattr(self.inspector, "service_labels", lambda p, s: [])(project, svc))
        except Exception:                                    # noqa: BLE001 - cannot tell what it had: nothing is carried over
            existing = []
        for key, value in labels.items():
            if key.startswith("com.docker.compose.") or key == CREATED_LABEL:
                continue
            if any(key == a or (a.endswith(".") and key.startswith(a)) for a in self.label_allow):
                continue
            if any(old.get(key) == value for old in existing):
                continue
            raise Refusal(f"label {key[:60]!r} is not allowed: it is not carried over from the service and not in docker_label_allow")

    def _healthcheck(self, value) -> dict:
        if not isinstance(value, dict) or set(value) - {"Test", "Interval", "Timeout", "Retries", "StartPeriod", "StartInterval"}:
            raise Refusal("Healthcheck field not served")
        out: dict[str, Any] = {}
        if "Test" in value:
            out["Test"] = _strlist(value["Test"], 64)
        for key in ("Interval", "Timeout", "StartPeriod", "StartInterval"):
            if key in value:
                out[key] = _int(value[key])
        if "Retries" in value:
            out["Retries"] = _int(value["Retries"], 0, 1000)
        return out

    def _networking(self, value) -> dict:
        if not isinstance(value, dict) or set(value) - {"EndpointsConfig"}:
            raise Refusal("NetworkingConfig field not served")
        endpoints = value.get("EndpointsConfig") or {}
        if not isinstance(endpoints, dict) or len(endpoints) > 8:
            raise BadRequest("bad EndpointsConfig")
        out = {}
        for name, config in endpoints.items():
            self._scoped_network_name(_str(name, 128))
            if config is None:
                config = {}
            if not isinstance(config, dict) or set(config) - {"Aliases"}:
                raise Refusal("endpoint field not served")
            out[name] = {"Aliases": _strlist(config.get("Aliases"), 16, 128)} if "Aliases" in config else {}
        return {"EndpointsConfig": out}

    def _host_config(self, host: Any, mounts: list) -> tuple[dict, list]:
        if not isinstance(host, dict):
            raise BadRequest("HostConfig must be an object")
        allowed = {"Binds", "Mounts", "RestartPolicy", "NetworkMode", "PortBindings", "PublishAllPorts", "CapAdd", "CapDrop",
                   "Dns", "DnsSearch", "ExtraHosts", "Memory", "MemorySwap", "MemoryReservation", "NanoCpus", "CpuShares",
                   "CpusetCpus", "PidsLimit", "ReadonlyRootfs", "LogConfig", "Init", "ShmSize", "Ulimits", "VolumesFrom",
                   "GroupAdd", "Privileged", "PidMode", "IpcMode", "UsernsMode", "CgroupnsMode", "UTSMode", "SecurityOpt",
                   "Devices", "Sysctls", "Tmpfs", "DeviceCgroupRules", "DeviceRequests", "AutoRemove"}
        extra = set(host) - allowed
        if extra:
            raise Refusal(f"HostConfig field not served: {min(extra)[:40]}")
        if host.get("Privileged"):
            raise Refusal("privileged containers are refused")
        for key in ("Devices", "Sysctls", "Tmpfs", "DeviceCgroupRules", "DeviceRequests"):
            if not _empty(host.get(key)):
                raise Refusal(f"{key} is refused")
        out: dict[str, Any] = {}
        for key in ("PidMode", "IpcMode", "UsernsMode", "CgroupnsMode", "UTSMode"):
            value = host.get(key)
            if value is None:
                continue
            allowed_modes = {"", "private", "shareable", "none"} if key == "IpcMode" else {"", "private"}
            if _str(value, 64) not in allowed_modes:
                raise Refusal(f"{key} {'host' if value == 'host' else 'other than private'} is refused")
            out[key] = value
        caps = [c.upper().removeprefix("CAP_") for c in _strlist(host.get("CapAdd"), 64, 40)]
        bad = sorted(set(caps) - CAP_ALLOW)
        if bad:
            raise Refusal(f"capability not allowed: {bad[0][:30]}")
        if caps:
            out["CapAdd"] = caps
        if host.get("CapDrop") is not None:
            out["CapDrop"] = _strlist(host["CapDrop"], 64, 40)
        sec = _strlist(host.get("SecurityOpt"), 8, 64)
        if set(sec) - SECURITY_OPT_ALLOW:
            raise Refusal("SecurityOpt beyond the allowlist is refused")
        if sec:
            out["SecurityOpt"] = sec
        groups = host.get("GroupAdd")
        if groups is not None:
            if not isinstance(groups, list) or len(groups) > 16:
                raise BadRequest("bad GroupAdd")
            for entry in groups:
                text = str(entry)
                if isinstance(entry, bool) or not text.isdigit() or int(text) in self.scope.protected_gids:
                    raise Refusal("GroupAdd to a protected or non-numeric group is refused")
            out["GroupAdd"] = groups
        if host.get("VolumesFrom") is not None:
            entries = _strlist(host["VolumesFrom"], 16, 140)
            for entry in entries:
                ref, _, mode = entry.partition(":")
                if mode not in ("", "ro", "rw"):
                    raise BadRequest("bad VolumesFrom mode")
                self._refuse_if_root_equivalent(self._container(ref, "scope"), protected_mounts=True)
            out["VolumesFrom"] = entries
        network = host.get("NetworkMode")
        if network is not None:
            self._scoped_network_name(_str(network, 128))
            out["NetworkMode"] = network
        summary: list[dict] = []
        binds = _strlist(host.get("Binds"), 64, 8192)
        if binds:
            out["Binds"] = [self._bind(entry, summary) for entry in binds]
        raw_mounts = host.get("Mounts")
        if raw_mounts is not None:
            if not isinstance(raw_mounts, list) or len(raw_mounts) > 64:
                raise BadRequest("bad Mounts")
            out["Mounts"] = [self._mount(m, summary) for m in raw_mounts]
        if host.get("RestartPolicy") is not None:
            out["RestartPolicy"] = self._restart(host["RestartPolicy"])
        if host.get("PortBindings") is not None:
            out["PortBindings"] = self._ports(host["PortBindings"])
        for key in ("PublishAllPorts", "ReadonlyRootfs", "Init", "AutoRemove"):
            if host.get(key) is not None:
                out[key] = _bool(host[key])
        for key in ("Dns", "DnsSearch", "ExtraHosts"):
            if host.get(key) is not None:
                out[key] = _strlist(host[key], 32, 256)
        for key in ("Memory", "MemorySwap", "MemoryReservation", "NanoCpus", "CpuShares", "ShmSize"):
            if host.get(key) is not None:
                out[key] = _int(host[key], -1 if key == "MemorySwap" else 0)
        if host.get("PidsLimit") is not None:
            out["PidsLimit"] = _int(host["PidsLimit"], -1, 1 << 20)
        if host.get("CpusetCpus") is not None:
            out["CpusetCpus"] = _str(host["CpusetCpus"], 64)
        if host.get("LogConfig") is not None:
            out["LogConfig"] = self._logconfig(host["LogConfig"])
        if host.get("Ulimits") is not None:
            ul = host["Ulimits"]
            if not isinstance(ul, list) or len(ul) > 16 or not all(
                    isinstance(u, dict) and set(u) <= {"Name", "Soft", "Hard"} and isinstance(u.get("Name"), str)
                    and _int(u.get("Soft", 0), -1) is not None and _int(u.get("Hard", 0), -1) is not None for u in ul):
                raise BadRequest("bad Ulimits")
            out["Ulimits"] = ul
        return out, summary

    def _bind(self, entry: str, summary: list) -> str:
        parts = entry.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise Refusal("a bind needs a source and a target (anonymous volumes are refused)")
        source, target = parts[0], self._mount_target(parts[1])
        opts = parts[2].split(",") if len(parts) == 3 else []
        if not set(opts) <= BIND_OPTS or "" in opts:
            raise Refusal("bind option not served (propagation and the like)")
        if source.startswith("/"):
            self._scoped_bind(source)
            summary.append({"type": "bind", "source": source, "target": target, "ro": "ro" in opts})
        else:
            self._scoped_volume(source)
            summary.append({"type": "volume", "source": source, "target": target, "ro": "ro" in opts})
        return entry

    def _mount(self, mount: Any, summary: list) -> dict:
        if not isinstance(mount, dict) or set(mount) - {"Type", "Source", "Target", "ReadOnly", "BindOptions", "VolumeOptions"}:
            raise Refusal("Mounts field not served")
        kind, source = mount.get("Type"), mount.get("Source")
        target = self._mount_target(mount.get("Target"))
        if kind not in ("bind", "volume") or not isinstance(source, str) or not source:
            raise Refusal("only bind and named-volume mounts are served")
        out: dict[str, Any] = {"Type": kind, "Source": source, "Target": target}
        if mount.get("ReadOnly") is not None:
            out["ReadOnly"] = _bool(mount["ReadOnly"])
        bind_opts = mount.get("BindOptions")
        if bind_opts is not None:
            if kind != "bind" or not isinstance(bind_opts, dict) or set(bind_opts) - {"Propagation", "NonRecursive"} or (
                    bind_opts.get("Propagation") not in (None, "rprivate", "private")):
                raise Refusal("bind option not served")
            out["BindOptions"] = bind_opts
        vol_opts = mount.get("VolumeOptions")
        if vol_opts is not None:
            if kind != "volume" or not isinstance(vol_opts, dict) or set(vol_opts) - {"NoCopy"}:
                raise Refusal("volume options (drivers, labels, subpaths) are not served")
            out["VolumeOptions"] = vol_opts
        if kind == "bind":
            if not source.startswith("/"):
                raise Refusal("a bind source must be an absolute path")
            self._scoped_bind(source)
        else:
            self._scoped_volume(source)
        summary.append({"type": kind, "source": source, "target": target, "ro": bool(out.get("ReadOnly"))})
        return out

    @staticmethod
    def _restart(value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {"Name", "MaximumRetryCount"} or value.get("Name", "") not in RESTART_NAMES:
            raise Refusal("RestartPolicy not served")
        out = {"Name": value.get("Name", "")}
        if "MaximumRetryCount" in value:
            out["MaximumRetryCount"] = _int(value["MaximumRetryCount"], 0, 1000)
        return out

    @staticmethod
    def _ports(value: Any) -> dict:
        if not isinstance(value, dict) or len(value) > 64:
            raise BadRequest("bad PortBindings")
        out = {}
        for key, bindings in value.items():
            if not _PORTKEY.match(str(key)) or not isinstance(bindings, list) or len(bindings) > 8:
                raise BadRequest("bad PortBindings")
            clean = []
            for b in bindings:
                if not isinstance(b, dict) or set(b) - {"HostIp", "HostPort"}:
                    raise Refusal("PortBindings field not served")
                clean.append({"HostIp": _str(b.get("HostIp", ""), 64), "HostPort": _str(b.get("HostPort", ""), 5)})
            out[key] = clean
        return out

    @staticmethod
    def _logconfig(value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {"Type", "Config"} or value.get("Type") not in LOG_DRIVERS:
            raise Refusal("LogConfig not served")
        config = value.get("Config") or {}
        if not isinstance(config, dict) or set(config) - {"max-size", "max-file"} or not all(
                isinstance(v, str) and len(v) <= 16 for v in config.values()):
            raise Refusal("LogConfig option not served")
        return {"Type": value["Type"], "Config": config}

    def _body_container_update(self, plan: Plan, body: dict, query: dict) -> bytes:
        ints = {"Memory", "MemorySwap", "MemoryReservation", "CpuShares", "CpuPeriod", "CpuQuota", "BlkioWeight",
                "PidsLimit", "NanoCpus"}
        extra = set(body) - ints - {"RestartPolicy", "CpusetCpus", "CpusetMems"}
        if extra:
            raise Refusal(f"update field not served: {min(extra)[:40]}")
        out: dict[str, Any] = {}
        for key, value in body.items():
            if key in ints:
                out[key] = _int(value, -1)
            elif key == "RestartPolicy":
                out[key] = self._restart(value)
            else:
                out[key] = _str(value, 64)
        plan.summary["fields"] = sorted(out)
        return json.dumps(out, separators=(",", ":"), sort_keys=True).encode()

    def _body_exec_create(self, plan: Plan, body: dict, query: dict) -> bytes:
        extra = set(body) - {"AttachStdin", "AttachStdout", "AttachStderr", "Tty", "Cmd", "Env", "WorkingDir", "User",
                              "DetachKeys", "Privileged", "ConsoleSize"}
        if extra:
            raise Refusal(f"exec field not served: {min(extra)[:40]}")
        if body.get("Privileged"):
            raise Refusal("privileged exec is refused")
        out: dict[str, Any] = {}
        for key in ("AttachStdin", "AttachStdout", "AttachStderr", "Tty"):
            if key in body:
                out[key] = _bool(body[key])
        cmd = _strlist(body.get("Cmd"), 128)
        if not cmd:
            raise BadRequest("exec needs a command")
        out["Cmd"] = cmd
        if "Env" in body:
            env = _strlist(body["Env"], 64)
            if not all(_ENV.match(e) for e in env):
                raise BadRequest("bad environment entry")
            out["Env"] = env
        for key in ("WorkingDir", "User", "DetachKeys"):
            if key in body:
                out[key] = _str(body[key], 256)
        if "ConsoleSize" in body:
            size = body["ConsoleSize"]
            if not isinstance(size, list) or len(size) != 2 or not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 10000 for n in size):
                raise BadRequest("bad ConsoleSize")
            out["ConsoleSize"] = size
        plan.summary.update({"cmd_argc": len(cmd), "cmd0": cmd[0][:120]})
        return json.dumps(out, separators=(",", ":"), sort_keys=True).encode()

    def _body_exec_start(self, plan: Plan, body: dict, query: dict) -> bytes:
        if set(body) - {"Detach", "Tty", "ConsoleSize"}:
            raise Refusal("exec start field not served")
        out = {k: _bool(body[k]) for k in ("Detach", "Tty") if k in body}
        return json.dumps(out, separators=(",", ":"), sort_keys=True).encode()

    def _network_body(self, plan: Plan, body: dict, *, connect: bool) -> bytes:
        allowed = {"Container", "EndpointConfig"} if connect else {"Container", "Force"}
        if set(body) - allowed:
            raise Refusal("network field not served")
        info = self._container(_str(body.get("Container"), 128, _NAME), "scope")
        out: dict[str, Any] = {"Container": info.id}
        if connect and body.get("EndpointConfig") is not None:
            config = body["EndpointConfig"]
            if not isinstance(config, dict) or set(config) - {"Aliases"}:
                raise Refusal("endpoint field not served")
            out["EndpointConfig"] = {"Aliases": _strlist(config.get("Aliases"), 16, 128)} if "Aliases" in config else {}
        if not connect and "Force" in body:
            out["Force"] = _bool(body["Force"])
        sid = self._service_id(info)
        plan.targets += [sid] if sid else []
        plan.summary["container"] = info.name or info.id[:12]
        return json.dumps(out, separators=(",", ":"), sort_keys=True).encode()

    def _body_network_connect(self, plan, body, query):
        return self._network_body(plan, body, connect=True)

    def _body_network_disconnect(self, plan, body, query):
        return self._network_body(plan, body, connect=False)


def _try_canon(text: str, host: str) -> str | None:
    try:
        return canon(text, host)
    except BadResource:
        return None


def _is_unix_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# ── responses ─────────────────────────────────────────────────────────────────────────────────────

def redact_inspect(doc: Any) -> Any:
    """Environment values are never shown (names stay), secret-looking labels and any secret-shaped string are masked."""
    def walk(value):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "Env" and isinstance(item, list):
                    out[key] = [(e.split("=", 1)[0] + "=" + REDACTED) if isinstance(e, str) and "=" in e else REDACTED
                                for e in item]
                elif key == "Labels" and isinstance(item, dict):
                    out[key] = {k: (REDACTED if _is_secret_key(str(k)) else redact_value(v)) for k, v in item.items()}
                else:
                    out[key] = walk(item)
            return out
        if isinstance(value, list):
            return [walk(v) for v in value]
        return redact_value(value) if isinstance(value, str) else value
    return walk(doc)


__all__ = [
    "API_VERSIONS",
    "ROUTES",
    "ContainerInfo",
    "Inspector",
    "NetworkInfo",
    "Plan",
    "Policy",
    "Route",
    "Scope",
    "compose_id",
    "redact_inspect",
]
