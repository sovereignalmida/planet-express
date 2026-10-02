"""v3 Phase 1 dry-run: log what the dependency graph sees at boot, without changing what boots.

`casa_boot.py` calls `summarize()` once, after it has already decided stack order, and only
prints the lines this returns -- nothing here can change a return code or a stack's order.
That's deliberate: wiring the graph into actual ordering is a separate, later change, reviewed
once this has run against the real fleet (`docs/designs/phase-1-state-model.md`, non-goals).
"""

from collections.abc import Sequence
from pathlib import Path

from planet_express.core import dependencies as deps

TAG = "[dry-run graph]"


def summarize(stacks: Sequence[Path]) -> list[str]:
    """One line of diagnostic text per thing worth knowing. A stack whose compose file can't be
    read (including invalid encoding) or parsed becomes a line, not an exception -- this runs
    inside the boot path and must not be able to break it. Not provably exception-free for every
    pathological input the detectors in `core/dependencies.py` might someday meet, which is why
    `casa_boot.py` also wraps the call itself rather than relying on this docstring alone."""
    stack_order = {stack_dir.name: i for i, stack_dir in enumerate(stacks)}
    parsed: list[deps.ComposeStack] = []
    lines: list[str] = []

    for stack_dir in stacks:
        compose_file = stack_dir / "docker-compose.yml"
        try:
            content = compose_file.read_text()
        except (OSError, UnicodeDecodeError) as error:
            lines.append(f"{TAG} cannot read {compose_file}: {error}")
            continue
        try:
            parsed.append(deps.load_compose_stack(stack_dir.name, content))
        except deps.ComposeParseError as error:
            lines.append(f"{TAG} cannot parse {compose_file}: {error}")

    graph = deps.discover(tuple(parsed))
    ordering_edges = [d for d in graph.dependencies if d.kind in ("depends_on", "namespace")]
    cross_project_ordering = [d for d in ordering_edges if d.cross_project]
    lines.append(
        f"{TAG} {len(graph.dependencies)} dependency edge(s) found "
        f"({len(ordering_edges)} ordering, {len(graph.dependencies) - len(ordering_edges)} "
        f"informational), {len(graph.unresolved)} unresolved"
    )
    for u in graph.unresolved:
        lines.append(f"{TAG} unresolved: {u.stack}/{u.service} {u.field}={u.value!r} ({u.reason})")

    violations = set(graph.ordering_violations(stack_order))
    for edge in cross_project_ordering:
        source_stack, target_stack = edge.source.split("/", 1)[0], edge.target.split("/", 1)[0]
        if edge in violations:
            lines.append(
                f"{TAG} WARNING: {edge.source} ({edge.kind}) needs {edge.target}, but stack "
                f"{target_stack!r} is ordered after {source_stack!r} in this boot"
            )
        elif source_stack in stack_order and target_stack in stack_order:
            lines.append(f"{TAG} ok: {edge.source} ({edge.kind}) -> {edge.target}, order already satisfied")

    return lines
