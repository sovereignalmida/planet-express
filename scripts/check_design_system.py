#!/usr/bin/env python3
"""Drift check: the design-system copy must agree with the repo, never the reverse.

static/cockpit.css is canonical. docs/designs/planet_express_design_v23/design-system/ is the
same CSS split by section for Claude Design. This fails if
  1. the sealed first 582 lines of static/cockpit.css (or the design package's copy) change,
  2. a rule in static/cockpit.css is missing from the split, or
  3. the split carries anything the repo does not (beyond the structural wrappers the split adds).

When it fails, change the DESIGN copy to match the repo. Do not edit static/cockpit.css to make
this pass: that silently breaks the seal.
"""
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "docs/designs/planet_express_design_v23"
SEAL_LINES, SEAL_SHA = 582, "aa913168cde1ff5d"
# Lines the split legitimately adds: :root wrappers around pulled-out tokens, the font import.
STRUCTURAL = re.compile(r"^(:root \{|\}|@import url\('https://fonts\.googleapis\.com/.*\);|--pe-[\w-]+: [^;]+;|@media .*\{ :root \{.*\} \})$")


def seal(path: Path) -> str:
    head = "".join(path.read_text().splitlines(keepends=True)[:SEAL_LINES])
    return hashlib.sha256(head.encode()).hexdigest()[:16]


def lines(css: str) -> Counter:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return Counter(re.sub(r"\s+", " ", l).strip() for l in css.splitlines() if l.strip())


def problems() -> list[str]:
    out = []
    for p in (ROOT / "static/cockpit.css", PKG / "system/cockpit.css"):
        if seal(p) != SEAL_SHA:
            out.append(f"seal broken: first {SEAL_LINES} lines of {p.relative_to(ROOT)} != {SEAL_SHA}")
    ds = PKG / "design-system"
    names = re.findall(r"@import url\('([^']+)'\)", (ds / "styles.css").read_text())
    split = lines("".join((ds / n).read_text() for n in names if not n.startswith("http")))
    repo = lines((ROOT / "static/cockpit.css").read_text())
    out += [f"in static/cockpit.css, missing from design-system: {l}" for l in (repo - split).elements()]
    out += [f"in design-system, not in static/cockpit.css: {l}"
            for l in (split - repo).elements() if not STRUCTURAL.match(l)]
    return out


if __name__ == "__main__":
    found = problems()
    print("\n".join(found) or "design-system matches static/cockpit.css; seal intact")
    sys.exit(1 if found else 0)
