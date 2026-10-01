import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_design_system", Path(__file__).resolve().parent.parent / "scripts/check_design_system.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_design_system_matches_cockpit_and_seal_intact():
    assert _mod.problems() == []
