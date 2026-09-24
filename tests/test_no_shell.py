"""Planet Express 2.0's central claim, as a test: there is no shell.

Slice 5b-5 deleted the shell executor and the pattern list that guarded it. These names coming back
would not be a style regression — it would be the unenforced boundary returning, which is the thing
CLAUDE.md's second-review gate exists to watch. A deliberate reintroduction has to delete this test
first, and explain why in the diff.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))

import casa_bender as bender
import casa_farnsworth as fw
import config

# Product modules only: the tests directory may legitimately name these while explaining their
# absence, and the docs record the history.
PRODUCT_FILES = sorted(
    path for path in ROOT.glob("*.py")
) + sorted((ROOT / "planet_express").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))

GONE_FROM_BENDER = ["_run_command", "_safety_check", "_step_succeeded", "FORBIDDEN_COMMANDS",
                    "execute", "execute_rollback", "run_safe_prune", "propose_compose_diff",
                    "apply_pending_diff", "get_pending_diff", "discard_pending_diff",
                    "PENDING_DIFFS_FILE"]
GONE_FROM_FARNSWORTH = ["plan", "save_plans", "load_pending_plan", "_execute_plan", "_do_rollback",
                        "_investigate_failure"]
GONE_FROM_CONFIG = ["STATE_PLAN", "ROLLBACK_CANDIDATES_FILE", "LEGACY_PLANS_ENABLED"]


@pytest.mark.parametrize("name", GONE_FROM_BENDER)
def test_the_shell_executor_is_gone_from_bender(name):
    assert not hasattr(bender, name), f"casa_bender.{name} is back"


@pytest.mark.parametrize("name", GONE_FROM_FARNSWORTH)
def test_the_shell_plan_flow_is_gone_from_farnsworth(name):
    assert not hasattr(fw, name), f"casa_farnsworth.{name} is back"


@pytest.mark.parametrize("name", GONE_FROM_CONFIG)
def test_the_retired_state_files_and_switch_are_gone(name):
    assert not hasattr(config, name), f"config.{name} is back"


def test_no_product_module_passes_shell_true_to_subprocess():
    """Not a grep for the string: an AST walk, so a comment mentioning it is fine and a keyword
    argument spelled any other way is not."""
    offenders = []
    for path in PRODUCT_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"shell=True is back in: {offenders}"


def test_only_the_argv_runners_touch_subprocess():
    """Every `subprocess` call in Bender lives in a runner that takes a list. A call built from a
    string would be a shell command in all but name."""
    tree = ast.parse((ROOT / "casa_bender.py").read_text())
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name) and inner.func.value.id == "subprocess"):
                callers.add(node.name)
    assert callers <= {"run_argv", "run_argv_bounded", "core_service_active", "_read_stream"}, (
        f"casa_bender has a new subprocess caller: {sorted(callers)}")
