"""The release-proof map (docs/handoff/v4/release-proof.md) is complete and everything it references exists."""
import re

from release_proof.support import ROOT

MAP = ROOT / "docs/handoff/v4/release-proof.md"
STATUSES = {"AUTOMATED", "AUTOMATED + HOST", "SCRIPT", "PENDING", "DONE"}
REF = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sh|md))(?:::([A-Za-z0-9_]+))?`")


def table_rows():
    rows = {}
    for line in MAP.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 3 and cells[0].isdigit():
            rows[int(cells[0])] = (cells[1], cells[2])
    return rows


def test_every_item_one_to_ten_is_mapped_with_a_known_status_and_evidence():
    rows = table_rows()
    assert sorted(rows) == list(range(1, 11))
    for item, (status, evidence) in rows.items():
        assert status in STATUSES, (item, status)
        assert REF.search(evidence), f"item {item} names no test or script"


def test_every_referenced_file_and_test_function_exists():
    text = MAP.read_text()
    refs = REF.findall(text)
    assert refs
    for path, name in refs:
        assert "/" in path or path in ("INSTALL.md", "CHANGELOG.md"), f"{path}: write the full repo path"
        target = ROOT / path
        assert target.is_file(), f"{path} does not exist"
        if name:
            assert re.search(rf"^\s*def {re.escape(name)}\(", target.read_text(), re.MULTILINE), f"{path}::{name} does not exist"


def test_pending_items_are_listed_as_open_with_what_is_missing():
    text = MAP.read_text()
    open_items = text[text.index("## Open items"):].lower()
    for item, (status, evidence) in table_rows().items():
        if status in ("PENDING", "SCRIPT"):
            assert f"item {item}" in open_items, f"item {item} is {status} but not listed under Open items"


def test_each_automated_module_carries_its_item_number_and_quotes_the_item():
    for n in (1, 2, 3, 4, 5, 6, 8, 10):
        matches = sorted((ROOT / "tests/release_proof").glob(f"test_item{n:02d}_*.py"))
        assert len(matches) == 1, n
        assert f"Release proof item {n}:" in matches[0].read_text().split('"""')[1]
