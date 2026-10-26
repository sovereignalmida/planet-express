"""Runs the node tests for static/console.js (tests/js/console.test.js): the pure helpers, and the whole page drawn against a fake DOM,
including the property that matters most, that agent and server text is only ever drawn as text. Skipped where node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_console_js_passes_its_node_tests():
    run = subprocess.run(["node", str(ROOT / "tests/js/console.test.js")], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "all passed" in run.stdout


def test_the_console_js_never_builds_markup_from_text():
    source = (ROOT / "static/console.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in source, forbidden
