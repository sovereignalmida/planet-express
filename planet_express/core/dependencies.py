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
    the Gluetun incident needed and didn't have."""

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
        """Dependencies where `<stack>/<service>` is the source -- what must be healthy before
        this one starts. The hook `casa_boot.py` (or its successor) calls at boot time."""
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


DETECTORS: tuple[Detector, ...] = (DependsOnDetector(), NamespaceReferenceDetector())


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
