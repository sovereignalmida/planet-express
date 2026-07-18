"""
Tests for scripts/setup_wizard.py's pure logic (config construction, sudoers.d
rendering) -- no interactive prompts, no real host, no real sudo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from pydantic import ValidationError

from setup_wizard import build_config, generate_sudoers_snippet, _path_completer, _prompt_list
from config_schema import SudoAllowlist, SudoGlobGrant, SudoUnitGrant


def test_minimal_answers_produce_valid_secure_by_default_config():
    cfg = build_config({"stacks_root": "/home/someuser/stacks"})
    assert cfg.stacks_root == Path("/home/someuser/stacks")
    assert cfg.forbidden_stacks == []
    assert cfg.paused_containers == []
    assert cfg.mounts == {}
    assert cfg.exclude_services == []
    assert cfg.sudo_allowlist.units == []
    assert cfg.sudo_allowlist.globs == []


def test_fully_populated_answers_round_trip():
    answers = {
        "stacks_root": "/home/someuser/stacks",
        "forbidden_stacks": ["clawbot", "ai"],
        "paused_containers": ["SOME_PAUSED"],
        "mounts": {"data.mount": "/data"},
        "exclude_services": [{"stack": "services", "service": "backend"}],
        "sudo_allowlist": {
            "units": [{"unit": "my-boot.service", "actions": ["start", "stop", "restart"]}],
            "globs": [{"glob": "*.mount", "actions": ["start", "stop"]}],
        },
    }
    cfg = build_config(answers)
    assert cfg.forbidden_stacks == ["clawbot", "ai"]
    assert cfg.mounts == {"data.mount": "/data"}
    assert cfg.exclude_services[0].stack == "services"
    assert cfg.sudo_allowlist.units[0].unit == "my-boot.service"
    assert cfg.sudo_allowlist.globs[0].glob == "*.mount"
    # Round-trips through the exact model config.py itself loads at import time.
    dumped = cfg.model_dump(mode="json")
    assert dumped["stacks_root"] == "/home/someuser/stacks"


def test_relative_stacks_root_rejected():
    with pytest.raises(ValidationError):
        build_config({"stacks_root": "relative/path"})


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        build_config({"stacks_root": "/home/someuser/stacks", "typo_field": True})


def test_sudoers_snippet_empty_allowlist():
    assert generate_sudoers_snippet("casaroot", SudoAllowlist()) == ""


def test_sudoers_snippet_matches_declared_grants():
    allowlist = SudoAllowlist(
        units=[SudoUnitGrant(unit="casa-startup.service", actions=["start", "stop", "restart"])],
        globs=[SudoGlobGrant(glob="*.mount", actions=["start", "stop"])],
    )
    snippet = generate_sudoers_snippet("casaroot", allowlist)
    assert "casaroot ALL=(root) NOPASSWD: /usr/bin/systemctl start casa-startup.service, " \
        "/usr/bin/systemctl stop casa-startup.service, /usr/bin/systemctl restart casa-startup.service" \
        in snippet
    assert "casaroot ALL=(root) NOPASSWD: /usr/bin/systemctl start *.mount, " \
        "/usr/bin/systemctl stop *.mount" in snippet


# ── Regression tests for bugs found during this spec's own live verification: an
# unregistered Tab key leaked a literal tab character into a path prompt (still passed
# the "must be absolute" check, silently), and typing "none" instead of pressing Enter
# produced a real garbage list/dict entry. ─────────────────────────────────────────

def test_prompt_list_treats_none_as_blank(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "none")
    assert _prompt_list("Any mounts to track") == []


def test_prompt_list_treats_none_case_insensitively(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "None")
    assert _prompt_list("Any mounts to track") == []


def test_prompt_list_still_parses_a_real_item_named_none_adjacent_text(monkeypatch):
    # "none" only short-circuits when it's the *entire* answer -- a real comma-separated
    # list still parses normally even if one entry happens to contain "none".
    monkeypatch.setattr("builtins.input", lambda _: "nonet.mount, data.mount")
    assert _prompt_list("Any mounts to track") == ["nonet.mount", "data.mount"]


def test_path_completer_completes_directory_entries(tmp_path):
    (tmp_path / "stacks").mkdir()
    (tmp_path / "stacksfile.txt").write_text("x")
    prefix = str(tmp_path / "stack")
    matches = set()
    state = 0
    while True:
        m = _path_completer(prefix, state)
        if m is None:
            break
        matches.add(m)
        state += 1
    assert str(tmp_path / "stacks") + "/" in matches
    assert str(tmp_path / "stacksfile.txt") in matches
