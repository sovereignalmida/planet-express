"""The dependency graph: edges between services, discovered from compose state -- never
declared.

v3 Phase 1 (`docs/designs/phase-1-state-model.md` SS3). The graph is rebuilt from parsed compose
files every time this runs; nothing here is hand-maintained. Each known kind of coupling is one
`Detector`, registered in `DETECTORS`. Adding a new kind of coupling later is adding a detector,
not redesigning the graph.

The namespace-reference detector exists because of a real, already-happened incident: CASA_GSP
and CASA_QBIT both ran with `network_mode: container:CASA_GLUETON` (CASA_GSP in
`stacks/network/docker-compose.yml`, CASA_QBIT in `stacks/media/docker-compose.yml` -- two
different compose projects). That coupling was invisible to `casa_boot.py` and existed only as
prose in `casa_farnsworth.py`'s LLM planning prompt, reachable only after something had already
broken. See `docs/designs/phase-1-state-model.md` SS2 for the evidence trail.

The rule this module exists to enforce: **a detector either emits a `Dependency` or declines.**
Nothing silently drops. A `network_mode` this module recognizes as *not* a reference (`host`,
`bridge`, `none`, absent) is a decline, not a gap. A `network_mode` that looks like a reference
but names a service/container nothing here can find becomes an `UnresolvedDependency` instead of
vanishing -- that's the mechanism for PE noticing its own blind spots, not an LLM inventing new
relationship semantics at runtime.
"""

import logging
import re
from dataclasses import dataclass
from typing import Protocol

import yaml

log = logging.getLogger("planetexpress.dependencies")

NAMESPACE_IGNORED = frozenset({"host", "bridge", "none", "default"})


class ComposeParseError(Exception):
    """`content` is not a compose document this module can index. Parsing a file a caller
    already has on disk is pure -- no filesystem call happens here; the caller reads it."""


@dataclass(frozen=True)
class ComposeStack:
    """One compose project, already parsed to a dict by `yaml.safe_load` -- pure data, no
    filesystem, no docker. A caller (eventually `casa_boot.py` or its successor) reads the file
    and hands the result in; nothing in this module touches a path or a socket."""

    name: str
    services: dict  # the parsed `services:` mapping, keyed by service name


def load_compose_stack(name: str, content: str) -> ComposeStack:
    """Parse one compose file's text into a `ComposeStack`. Mirrors
    `compose_plans.parse_services`'s validation, since both refuse the same malformed input --
    but this keeps its own copy rather than importing the application layer into `core`."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ComposeParseError(f"{name}: not valid YAML: {exc}") from None
    if not isinstance(document, dict):
        raise ComposeParseError(f"{name}: not a compose document")
    services = document.get("services")
    if not isinstance(services, dict):
        raise ComposeParseError(f"{name}: declares no services")
    return ComposeStack(name=name, services=services)


@dataclass(frozen=True)
class Dependency:
    """One discovered edge. `source`/`target` are `"<stack>/<service>"`. `cross_project` is
    True exactly when source and target are not in the same `ComposeStack` -- this is the field
    the Gluetun incident needed and didn't have.

    Not every `kind` means the same thing as an edge: `depends_on` and `namespace` are real
    start-order constraints (source cannot come up correctly before target). `shared_mount` is
    informational and symmetric -- two services sharing a path are a coupled failure domain
    (one going read-only can make the other misbehave), not an ordering rule; `source`/`target`
    for that kind is just alphabetical, not "depends on." A boot-ordering caller should filter
    to `kind in ("depends_on", "namespace")`."""

    kind: str
    source: str
    target: str
    cross_project: bool
    detector: str
    detail: str | None = None


@dataclass(frozen=True)
class UnresolvedDependency:
    """Something that looks like a reference to another resource, found by a detector that
    recognizes the *shape* but not the *target*. Surfaced so a human adds either data (a missing
    `container_name:`) or a new detector -- never dropped silently."""

    stack: str
    service: str
    field: str
    value: str
    reason: str
    detector: str


@dataclass(frozen=True)
class DependencyGraph:
    dependencies: tuple[Dependency, ...]
    unresolved: tuple[UnresolvedDependency, ...]

    def for_service(self, stack: str, service: str) -> tuple[Dependency, ...]:
        """Every edge naming `<stack>/<service>` as source, any kind. A caller that wants "what
        must be healthy before this starts" (the hook `casa_boot.py`'s successor would call)
        filters this to `kind in ("depends_on", "namespace")` -- see `Dependency`'s docstring on
        why `shared_mount` is not an ordering edge."""
        key = f"{stack}/{service}"
        return tuple(d for d in self.dependencies if d.source == key)


class Detector(Protocol):
    name: str

    def detect(
        self, stacks: tuple[ComposeStack, ...]
    ) -> tuple[tuple[Dependency, ...], tuple[UnresolvedDependency, ...]]: ...


class DependsOnDetector:
    """Compose's own `depends_on:`, in both list form and the long dict form with a
    `condition:`. Always same-project -- compose's `depends_on` cannot name a service in
    another project, which is exactly why the Gluetun coupling needed its own detector."""

    name = "depends_on"

    def detect(
        self, stacks: tuple[ComposeStack, ...]
    ) -> tuple[tuple[Dependency, ...], tuple[UnresolvedDependency, ...]]:
        found: list[Dependency] = []
        unresolved: list[UnresolvedDependency] = []
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                raw = svc.get("depends_on")
                if raw is None:
                    continue
                names = list(raw) if isinstance(raw, (list, dict)) else None
                if names is None:
                    unresolved.append(
                        UnresolvedDependency(
                            stack=stack.name,
                            service=svc_name,
                            field="depends_on",
                            value=repr(raw),
                            reason="depends_on is neither a list nor a mapping",
                            detector=self.name,
                        )
                    )
                    continue
                for target_name in names:
                    if target_name in stack.services:
                        found.append(
                            Dependency(
                                kind="depends_on",
                                source=f"{stack.name}/{svc_name}",
                                target=f"{stack.name}/{target_name}",
                                cross_project=False,
                                detector=self.name,
                            )
                        )
                    else:
                        unresolved.append(
                            UnresolvedDependency(
                                stack=stack.name,
                                service=svc_name,
                                field="depends_on",
                                value=target_name,
                                reason=f"no service {target_name!r} in stack {stack.name!r}",
                                detector=self.name,
                            )
                        )
        return tuple(found), tuple(unresolved)


class NamespaceReferenceDetector:
    """`network_mode: service:<name>` (same-project only, by compose's own rules) and
    `network_mode: container:<name>` (same-project or cross-project -- resolved by matching
    `container_name:` across every known stack, the same way `actions.py`'s
    `parse_router_owners()` resolves it from a live `docker inspect`, except here it runs on
    parsed compose files, before anything has been started)."""

    name = "namespace_reference"

    def detect(
        self, stacks: tuple[ComposeStack, ...]
    ) -> tuple[tuple[Dependency, ...], tuple[UnresolvedDependency, ...]]:
        container_name_index: dict[str, tuple[str, str]] = {}
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                cname = svc.get("container_name")
                if isinstance(cname, str) and cname:
                    container_name_index[cname] = (stack.name, svc_name)

        found: list[Dependency] = []
        unresolved: list[UnresolvedDependency] = []
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                mode = svc.get("network_mode")
                if not isinstance(mode, str):
                    continue
                if mode in NAMESPACE_IGNORED:
                    continue
                if ":" not in mode:
                    unresolved.append(
                        UnresolvedDependency(
                            stack=stack.name,
                            service=svc_name,
                            field="network_mode",
                            value=mode,
                            reason="not a recognized network_mode value",
                            detector=self.name,
                        )
                    )
                    continue
                ref_kind, _, ref_value = mode.partition(":")
                source = f"{stack.name}/{svc_name}"
                if ref_kind == "service":
                    # Compose only resolves `service:` within the same project.
                    if ref_value in stack.services:
                        found.append(
                            Dependency(
                                kind="namespace",
                                source=source,
                                target=f"{stack.name}/{ref_value}",
                                cross_project=False,
                                detector=self.name,
                                detail=mode,
                            )
                        )
                    else:
                        unresolved.append(
                            UnresolvedDependency(
                                stack=stack.name,
                                service=svc_name,
                                field="network_mode",
                                value=mode,
                                reason=(
                                    f"no service {ref_value!r} in stack {stack.name!r} "
                                    "(service: only resolves within one compose project)"
                                ),
                                detector=self.name,
                            )
                        )
                elif ref_kind == "container":
                    owner = container_name_index.get(ref_value)
                    if owner is None:
                        unresolved.append(
                            UnresolvedDependency(
                                stack=stack.name,
                                service=svc_name,
                                field="network_mode",
                                value=mode,
                                reason=(
                                    f"no known service declares container_name: {ref_value!r}"
                                ),
                                detector=self.name,
                            )
                        )
                        continue
                    owner_stack, owner_service = owner
                    found.append(
                        Dependency(
                            kind="namespace",
                            source=source,
                            target=f"{owner_stack}/{owner_service}",
                            cross_project=owner_stack != stack.name,
                            detector=self.name,
                            detail=mode,
                        )
                    )
                else:
                    unresolved.append(
                        UnresolvedDependency(
                            stack=stack.name,
                            service=svc_name,
                            field="network_mode",
                            value=mode,
                            reason=f"unrecognized network_mode prefix {ref_kind!r}",
                            detector=self.name,
                        )
                    )
        return tuple(found), tuple(unresolved)


class SharedMountDetector:
    """Two services that bind-mount the same host path, same project or not, are coupled even
    with no `depends_on` and no shared namespace: `/media/youtube` going unreadable is exactly
    the storage-dependency failure mode SS9 of the Architecture Brief names explicitly
    (TubeArchivist writing into an empty directory instead of failing cleanly).

    Deliberately narrow for this first cut: only **bind mounts to an absolute host path** are
    indexed (`source: /…` in long form, or `/…:/…` in short form). A top-level named volume
    (`media-library:/data`) is not indexed -- compose scopes those to one project by default,
    so cross-project named-volume sharing is a real but rarer case, left for a later detector
    rather than guessed at here. There is nothing to leave `UnresolvedDependency` for: two
    sources either match exactly or they don't, there is no reference syntax to fail to resolve.
    """

    name = "shared_mount"

    def _bind_sources(self, svc: dict) -> set[str]:
        sources: set[str] = set()
        for entry in svc.get("volumes") or []:
            if isinstance(entry, str):
                source = entry.split(":", 1)[0]
            elif isinstance(entry, dict):
                source = entry.get("source") if entry.get("type", "bind") == "bind" else None
            else:
                continue
            if isinstance(source, str) and source.startswith("/") and len(source) > 1:
                sources.add(source)
        return sources

    def detect(
        self, stacks: tuple[ComposeStack, ...]
    ) -> tuple[tuple[Dependency, ...], tuple[UnresolvedDependency, ...]]:
        by_path: dict[str, list[str]] = {}
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                key = f"{stack.name}/{svc_name}"
                for path in self._bind_sources(svc):
                    by_path.setdefault(path, []).append(key)

        found: list[Dependency] = []
        for path, keys in by_path.items():
            ordered = sorted(set(keys))
            for i, source in enumerate(ordered):
                for target in ordered[i + 1 :]:
                    found.append(
                        Dependency(
                            kind="shared_mount",
                            source=source,
                            target=target,
                            cross_project=source.split("/", 1)[0] != target.split("/", 1)[0],
                            detector=self.name,
                            detail=path,
                        )
                    )
        return tuple(found), ()


_ROUTER_SERVICE_LABEL = re.compile(r"^traefik\.http\.routers\.([^.]+)\.service$")
_SERVICE_DECLARATION_LABEL = re.compile(r"^traefik\.http\.services\.([^.]+)\.loadbalancer\.")


class TraefikRouterDetector:
    """A router's labels can live on a different container than the service they route to --
    exactly the case `actions.py`'s `parse_router_owners()` docstring already names: "qbit's
    labels have to live on gluetun" (qbittorrent shares gluetun's namespace, so Traefik only
    sees gluetun's ports; its router labels have to be declared there instead). Compose-level
    equivalent of that same attribution problem, found here instead of from a live `docker
    inspect`: a router's labels name its target service explicitly
    (`traefik.http.routers.<r>.service=<name>`), and <name> is declared by *some* service's
    `traefik.http.services.<name>.loadbalancer...` labels, possibly in a different stack.

    A router with no explicit `.service=` label routes to a service of its own container's
    name (Traefik's own default) -- nothing cross-referenced, so no edge. An explicit
    `.service=` naming something no known service declares is unresolved, same policy as the
    namespace detector -- and so is a name two different services both declare: picking
    whichever was iterated last would make the graph depend on argument order, the exact
    silent-arbitrary-resolution this whole module exists to refuse. A router naming its own
    container's declared service is valid Traefik config but not a real inter-service edge,
    so it is skipped rather than reported as a self-dependency."""

    name = "traefik_router"

    @staticmethod
    def _labels(svc: dict) -> dict[str, str]:
        raw = svc.get("labels")
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, list):
            pairs = (str(item).split("=", 1) for item in raw if isinstance(item, str) and "=" in str(item))
            return {k: v for k, v in pairs}
        return {}

    def detect(
        self, stacks: tuple[ComposeStack, ...]
    ) -> tuple[tuple[Dependency, ...], tuple[UnresolvedDependency, ...]]:
        # A set, not a list: one service commonly carries several `.loadbalancer.*` labels
        # (server.port, passhostheader, ...), and counting each label as its own declarer would
        # falsely report a single real declarer as an ambiguous pair of itself.
        declares: dict[str, set[tuple[str, str]]] = {}
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                for label in self._labels(svc):
                    m = _SERVICE_DECLARATION_LABEL.match(label)
                    if m:
                        declares.setdefault(m.group(1), set()).add((stack.name, svc_name))

        found: list[Dependency] = []
        unresolved: list[UnresolvedDependency] = []
        for stack in stacks:
            for svc_name, svc in stack.services.items():
                if not isinstance(svc, dict):
                    continue
                labels = self._labels(svc)
                for label, target_service in labels.items():
                    m = _ROUTER_SERVICE_LABEL.match(label)
                    if not m:
                        continue
                    router = m.group(1)
                    owners = sorted(declares.get(target_service, set()))
                    source = f"{stack.name}/{svc_name}"
                    if not owners:
                        unresolved.append(
                            UnresolvedDependency(
                                stack=stack.name,
                                service=svc_name,
                                field=label,
                                value=target_service,
                                reason=(
                                    f"router {router!r} names service {target_service!r}, "
                                    "which no known service's labels declare"
                                ),
                                detector=self.name,
                            )
                        )
                        continue
                    if len(owners) > 1:
                        unresolved.append(
                            UnresolvedDependency(
                                stack=stack.name,
                                service=svc_name,
                                field=label,
                                value=target_service,
                                reason=(
                                    f"router {router!r} names service {target_service!r}, "
                                    f"which {len(owners)} different services declare: "
                                    + ", ".join(f"{s}/{n}" for s, n in owners)
                                ),
                                detector=self.name,
                            )
                        )
                        continue
                    owner_stack, owner_service = owners[0]
                    if (owner_stack, owner_service) == (stack.name, svc_name):
                        continue  # a router naming its own container's service: not an edge
                    found.append(
                        Dependency(
                            kind="traefik_router",
                            source=source,
                            target=f"{owner_stack}/{owner_service}",
                            cross_project=owner_stack != stack.name,
                            detector=self.name,
                            detail=f"router {router} -> service {target_service}",
                        )
                    )
        return tuple(found), tuple(unresolved)


DETECTORS: tuple[Detector, ...] = (
    DependsOnDetector(), NamespaceReferenceDetector(), SharedMountDetector(),
    TraefikRouterDetector(),
)


def discover(
    stacks: tuple[ComposeStack, ...], detectors: tuple[Detector, ...] = DETECTORS
) -> DependencyGraph:
    dependencies: list[Dependency] = []
    unresolved: list[UnresolvedDependency] = []
    for detector in detectors:
        found, unmatched = detector.detect(stacks)
        dependencies.extend(found)
        unresolved.extend(unmatched)
        for u in unmatched:
            log.warning(
                "unresolved dependency: %s/%s %s=%r (%s) [%s]",
                u.stack, u.service, u.field, u.value, u.reason, u.detector,
            )
    return DependencyGraph(dependencies=tuple(dependencies), unresolved=tuple(unresolved))
