"""Shared path setup for the release-proof modules. Importing this puts tests/ and the repo root on sys.path."""
import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent
ROOT = TESTS.parent
FIXTURES = TESTS / "fixtures" / "v4"
for p in (str(TESTS), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))
