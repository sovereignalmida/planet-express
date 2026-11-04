"""`tests/launcher_core.py` runs only under the root-only privileged suites, which skip everywhere else: its lazy imports would
rot unseen (one still named the deleted `missions.protected` after trust mode). Every `planet_express`/`config_schema` name it
imports must still be defined at the top of its module (read from the source: some of them load the host config on import)."""
import ast
import importlib.util
from pathlib import Path


def _top_level_names(path: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


def test_every_name_launcher_core_imports_still_exists():
    tree = ast.parse((Path(__file__).parent / "launcher_core.py").read_text())
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ("planet_express", "config_schema"):
            spec = importlib.util.find_spec(node.module)
            assert spec is not None and spec.origin, f"no module {node.module}"
            defined = _top_level_names(spec.origin)
            for alias in node.names:
                assert alias.name in defined, f"{node.module}.{alias.name}"
                checked += 1
    assert checked > 5
