"""setup.sh: refuses clearly, and its lock file is hash-pinned (A4e)."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_script_is_valid_posix_sh_and_executable():
    assert subprocess.run(["sh", "-n", str(ROOT / "setup.sh")]).returncode == 0
    assert os.access(ROOT / "setup.sh", os.X_OK)
    assert (ROOT / "setup.sh").read_text().startswith("#!/bin/sh")


@pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root")
def test_it_refuses_without_root_and_says_what_to_do():
    result = subprocess.run(["sh", str(ROOT / "setup.sh")], capture_output=True, text=True)
    assert result.returncode == 1 and "sudo ./setup.sh" in result.stderr


def test_every_requirement_in_the_bootstrap_lock_is_pinned_with_hashes():
    text = (ROOT / "requirements-bootstrap.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\", text, re.M)
    assert {"pydantic", "flask", "cryptography", "segno", "pyyaml"} <= {p.lower() for p in pins}
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text)
    assert all("--hash=sha256:" in b for b in blocks if "==" in b.split("\n")[0])
