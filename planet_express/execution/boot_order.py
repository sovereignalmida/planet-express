"""Deriving stack boot order from the dependency graph -- the change the v3 Phase 1 dry run
(`dependency_dryrun.py`) existed to earn before anything was allowed to act on it.

`order_stacks()` reads each active stack's real compose file, builds the graph, reduces it to
stack-level precedence, and returns a reordering of the caller's own baseline (the existing
network-first-then-alphabetical rule in `casa_boot.py`) that satisfies every cross-project
`depends_on`/`namespace` edge found. It never adds, drops, or duplicates a stack, and it never
raises: any failure to read, parse, order, or even reassemble the result falls back to the
baseline unchanged, with a line explaining why. A boot that can't compute a smarter order still
boots in the order it always did -- that guarantee is enforced *inside* this function, not left
to whatever happens to catch an exception at the call site."""

from collections.abc import Sequence
from pathlib import Path

from planet_express.core import dependencies as deps

TAG = "[boot-order]"


def order_stacks(stacks: Sequence[Path]) -> tuple[list[Path], list[str]]:
    """`stacks` is both the set to order and the baseline order (tie-break) -- the caller's
    already-decided network-first-then-alphabetical list. Returns (order, log_lines): `order`
    is `list(stacks)` unchanged whenever nothing qualifies for reordering, which includes every
    failure case below, not just the "no constraints found" case."""
    lines: list[str] = []
    baseline = list(stacks)

    parsed: list[deps.ComposeStack] = []
    for stack_dir in stacks:
        compose_file = stack_dir / "docker-compose.yml"
        try:
            content = compose_file.read_text()
        except (OSError, UnicodeDecodeError) as error:
            lines.append(f"{TAG} cannot read {compose_file}: {error} -- using baseline order")
            return baseline, lines
        try:
            parsed.append(deps.load_compose_stack(stack_dir.name, content))
        except deps.ComposeParseError as error:
            lines.append(f"{TAG} cannot parse {compose_file}: {error} -- using baseline order")
            return baseline, lines

    # Everything past this point is graph construction and pure reasoning over it -- no I/O,
    # but not proven exception-free for every input a future detector might meet either (codex
    # review: a scalar `volumes: 1` reaches SharedMountDetector, fixed separately in
    # dependencies.py, but this function doesn't rely on every detector having anticipated
    # every malformed shape). One try/except around all of it, not just the file reads above.
    try:
        graph = deps.discover(tuple(parsed))
        baseline_names = [p.name for p in baseline]
        must_precede = graph.stack_precedence(set(baseline_names))
        if not must_precede:
            return baseline, lines

        ordered_names = deps.stable_topological_order(baseline_names, must_precede)
        if ordered_names is None:
            lines.append(
                f"{TAG} cross-project dependency cycle in {sorted(must_precede)} -- "
                "using baseline order unchanged"
            )
            return baseline, lines

        if ordered_names == baseline_names:
            return baseline, lines

        # Defense in depth against a basename collision (two stack directories that happen
        # to share a `.name`, which `config.active_stack_dirs()` shouldn't produce -- they all
        # live under one stacks_root -- but this function doesn't trust that either): rebuild
        # by position in `baseline_names`, then verify the result is exactly a permutation of
        # `baseline`, never a list with a path missing or repeated.
        position = {name: i for i, name in enumerate(baseline_names)}
        reordered = [baseline[position[name]] for name in ordered_names]
        if sorted(reordered, key=str) != sorted(baseline, key=str):
            lines.append(
                f"{TAG} reordering did not produce exactly the original stack set -- "
                "using baseline order unchanged"
            )
            return baseline, lines
    except Exception as error:  # noqa: BLE001 -- see module docstring: never raise, ever
        lines.append(f"{TAG} could not compute an order ({error}) -- using baseline order")
        return baseline, lines

    lines.append(
        f"{TAG} reordered to satisfy cross-project dependencies: "
        f"{', '.join(baseline_names)} -> {', '.join(ordered_names)}"
    )
    return reordered, lines
