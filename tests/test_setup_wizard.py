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
from setup_wizard import (
    _collect_mounts,
    _discover_mount_units,
    _docker_root_dir,
    _path_completer,
    _prompt_actions,
    _prompt_list,
    _prompt_unit_name,
    build_config,
    generate_sudoers_snippet,
)

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


def test_sudoers_snippet_unit_grant():
    allowlist = SudoAllowlist(
        units=[SudoUnitGrant(unit="casa-stacks.service", actions=["start", "stop", "restart"])],
    )
    snippet = generate_sudoers_snippet("casaroot", allowlist)
    assert "casaroot ALL=(root) NOPASSWD: /usr/bin/systemctl start casa-stacks.service, " \
        "/usr/bin/systemctl stop casa-stacks.service, /usr/bin/systemctl restart casa-stacks.service" \
        in snippet


def _rule_portions(snippet: str) -> list[str]:
    """The sudoers-meaningful part of each rule line, with the (inert, comment-only)
    trailing '# matched glob ...' annotation stripped off."""
    return [
        line.split("  #", 1)[0]
        for line in snippet.splitlines()
        if line.startswith("casaroot ")
    ]


def test_sudoers_snippet_expands_glob_to_discovered_exact_units():
    allowlist = SudoAllowlist(globs=[SudoGlobGrant(glob="*.mount", actions=["start", "stop"])])
    snippet = generate_sudoers_snippet(
        "casaroot", allowlist, discovered_units=["data.mount", "backup.mount", "not-a-mount.service"]
    )
    assert "casaroot ALL=(root) NOPASSWD: /usr/bin/systemctl start data.mount, " \
        "/usr/bin/systemctl stop data.mount" in snippet
    assert "casaroot ALL=(root) NOPASSWD: /usr/bin/systemctl start backup.mount, " \
        "/usr/bin/systemctl stop backup.mount" in snippet
    assert "not-a-mount.service" not in snippet
    # No raw wildcard ever reaches a sudoers-meaningful rule (the actual command list
    # sudo matches against) -- only an inert trailing comment may mention the glob text.
    assert all("*" not in rule for rule in _rule_portions(snippet))


def test_sudoers_snippet_glob_matching_nothing_produces_no_rule():
    allowlist = SudoAllowlist(globs=[SudoGlobGrant(glob="*.mount", actions=["start", "stop"])])
    assert generate_sudoers_snippet("casaroot", allowlist, discovered_units=[]) == ""
    assert generate_sudoers_snippet("casaroot", allowlist, discovered_units=["other.service"]) == ""


def test_sudoers_snippet_never_contains_a_bare_wildcard_rule():
    # Regression test for a real bypass an independent Codex review found: sudoers
    # matches '*' via fnmatch() against the *entire* remaining command-line string,
    # crossing whitespace -- a literal "/usr/bin/systemctl stop *.mount" rule would
    # also match "systemctl stop ssh.service data.mount", since the tail still
    # satisfies "*.mount". Glob grants must always be expanded to exact unit names,
    # never written as a literal wildcard into the sudoers file.
    allowlist = SudoAllowlist(globs=[SudoGlobGrant(glob="*.mount", actions=["stop"])])
    snippet = generate_sudoers_snippet("casaroot", allowlist, discovered_units=["data.mount"])
    assert all("*" not in rule for rule in _rule_portions(snippet))
    assert "stop data.mount" in snippet


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


# ── Regression tests for real issues an independent Codex review found in the
# unit/glob-grant collection flow itself, not just the pure generate_sudoers_snippet()
# logic already covered above. ──────────────────────────────────────────────────────

def test_prompt_unit_name_rejects_sudoers_metacharacters(monkeypatch):
    # A real bypass: the unit-name prompt had no validation at all, so a value like
    # "foo.service, /bin/bash" (comma starts a second Cmnd in sudoers syntax) or
    # "*.service" (an actual wildcard) would flow straight into a NOPASSWD rule --
    # granting far more than intended, and invisibly to visudo -c since it's
    # syntactically valid sudoers either way.
    responses = iter(["foo.service, /bin/bash", "*.service", "casa-stacks.service"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert _prompt_unit_name("Unit name") == "casa-stacks.service"


def test_prompt_unit_name_accepts_blank_to_stop():
    import builtins
    orig_input = builtins.input
    try:
        builtins.input = lambda _: ""
        assert _prompt_unit_name("Unit name") == ""
    finally:
        builtins.input = orig_input


def test_prompt_actions_rejects_empty_and_reprompts(monkeypatch):
    # A real gap: answering with only separators (e.g. ",") produced an empty actions
    # list that passed config validation (no min_length constraint) but then produced
    # an empty sudoers command list, which visudo rejects -- *after* config.yaml had
    # already been written, leaving a partial install. Note a bare Enter (blank raw)
    # isn't the case being tested here -- _prompt()'s own default-substitution already
    # turns that into the (non-empty) default before _prompt_actions ever sees it;
    # "," and ",," are non-blank raw input that still parse to zero real actions.
    responses = iter([",", ",,", "start,stop"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert _prompt_actions("Actions", ["start"]) == ["start", "stop"]


def test_sudoers_snippet_escapes_colon_in_unit_name():
    # A real gap: ':' is valid per _UNIT_NAME_RE and real systemd unit-name syntax
    # (template/instance units), but sudoers(5) treats an unescaped ':' as a command
    # delimiter -- an independent Codex review found this broke the generated file
    # (visudo would reject it) *after* config.yaml had already been written.
    allowlist = SudoAllowlist(units=[SudoUnitGrant(unit="getty@:1.service", actions=["start"])])
    snippet = generate_sudoers_snippet("casaroot", allowlist)
    assert "getty@\\:1.service" in snippet
    assert "getty@:1.service" not in _rule_portions(snippet)[0]  # unescaped form absent


def test_sudoers_snippet_with_colon_passes_visudo(tmp_path):
    import subprocess
    allowlist = SudoAllowlist(units=[SudoUnitGrant(unit="getty@:1.service", actions=["start", "stop"])])
    snippet = generate_sudoers_snippet("casaroot", allowlist)
    snippet_file = tmp_path / "sudoers-snippet"
    snippet_file.write_text(snippet)
    result = subprocess.run(
        ["visudo", "-c", "-f", str(snippet_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


# ── _discover_mount_units() ──────────────────────────────────────────────────────────

def _fake_subprocess_run(list_units_stdout: str, where_by_unit: dict, docker_root: str = "/var/lib/docker"):
    """Sequential fake for the three subprocess.run() calls _discover_mount_units() makes:
    `docker info --format {{.DockerRootDir}}`, then `systemctl list-units --type=mount`,
    then a single batched `systemctl show <units...> -p Where`."""
    import subprocess as _subprocess

    def fake_run(cmd, **kwargs):
        if cmd[0] == "docker":
            return _subprocess.CompletedProcess(cmd, 0, stdout=f"{docker_root}\n", stderr="")
        if cmd[1] == "list-units":
            return _subprocess.CompletedProcess(cmd, 0, stdout=list_units_stdout, stderr="")
        # cmd == ["systemctl", "show", "-p", "Where", "--value", "--", *units]
        # Real systemctl separates each unit's property block with a blank line when
        # multiple units are queried at once -- reproduced here rather than a plain
        # newline join, since that's the exact quirk _discover_mount_units() must handle.
        units = cmd[6:]
        stdout = "\n\n".join(where_by_unit.get(u, "") for u in units) + "\n"
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return fake_run


def test_discover_mount_units_excludes_docker_managed_mounts(monkeypatch):
    # Regression test: on a real Docker host, "systemctl list-units --type=mount" returns
    # not just genuine host mounts but hundreds of Docker-internal netns/overlay2/volume
    # bookkeeping mounts that churn on every container restart -- these must never reach
    # the discovered-units list the "*.mount" sudo glob gets expanded against.
    list_units_stdout = (
        "urphotos.mount loaded active mounted Mount urphotos\n"
        "run-docker-netns-abc123.mount loaded active mounted /run/docker/netns/abc123\n"
        "var-lib-docker-overlay2-def456-merged.mount loaded active mounted /var/lib/docker/overlay2/def456/merged\n"
    )
    where_by_unit = {
        "urphotos.mount": "/urphotos",
        "run-docker-netns-abc123.mount": "/run/docker/netns/abc123",
        "var-lib-docker-overlay2-def456-merged.mount": "/var/lib/docker/overlay2/def456/merged",
    }
    monkeypatch.setattr(
        "subprocess.run", _fake_subprocess_run(list_units_stdout, where_by_unit)
    )
    assert _discover_mount_units() == ["urphotos.mount"]


def test_discover_mount_units_handles_root_mount_unit_name(monkeypatch):
    # Regression test: the root filesystem's mount unit is literally named "-.mount".
    # Without a "--" separator before the unit list, systemctl parses that leading "-"
    # as an (invalid) option and the whole batched "systemctl show" call fails outright
    # -- silently falling back to the unfiltered (Docker-mount-polluted) list on every
    # real host, since "-.mount" is present on virtually every Linux system.
    import subprocess as _subprocess

    list_units_stdout = (
        "-.mount loaded active mounted Root Mount\n"
        "urphotos.mount loaded active mounted Mount urphotos\n"
        "run-docker-netns-abc123.mount loaded active mounted /run/docker/netns/abc123\n"
    )
    where_by_unit = {
        "-.mount": "/",
        "urphotos.mount": "/urphotos",
        "run-docker-netns-abc123.mount": "/run/docker/netns/abc123",
    }
    seen_show_cmd = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "docker":
            return _subprocess.CompletedProcess(cmd, 0, stdout="/var/lib/docker\n", stderr="")
        if cmd[1] == "list-units":
            return _subprocess.CompletedProcess(cmd, 0, stdout=list_units_stdout, stderr="")
        seen_show_cmd.extend(cmd)
        if "--" not in cmd:
            # What real systemctl does when a unit name starting with "-" is passed
            # without an option/argument separator: refuses to parse it as a unit at
            # all, so the whole call errors out instead of yielding per-unit Where values.
            return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="invalid option -- '.'")
        units = cmd[cmd.index("--") + 1:]
        stdout = "\n\n".join(where_by_unit.get(u, "") for u in units) + "\n"
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _discover_mount_units() == ["-.mount", "urphotos.mount"]
    assert "--" in seen_show_cmd


def test_discover_mount_units_falls_back_to_unfiltered_on_where_lookup_mismatch(monkeypatch):
    import subprocess as _subprocess

    list_units_stdout = (
        "urphotos.mount loaded active mounted Mount urphotos\n"
        "mnt-casabu.mount loaded active mounted Mount casabu\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "docker":
            return _subprocess.CompletedProcess(cmd, 0, stdout="/var/lib/docker\n", stderr="")
        if cmd[1] == "list-units":
            return _subprocess.CompletedProcess(cmd, 0, stdout=list_units_stdout, stderr="")
        # Only one block for two discovered units -- a shape this code didn't
        # anticipate, so it must fail open to the unfiltered list rather than guess.
        return _subprocess.CompletedProcess(cmd, 0, stdout="/urphotos\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _discover_mount_units() == ["urphotos.mount", "mnt-casabu.mount"]


def test_discover_mount_units_excludes_docker_mounts_under_a_non_default_data_root(monkeypatch):
    # Regression test for a Codex-flagged P2: Docker's daemon.json "data-root" option can
    # point anywhere, e.g. /mnt/docker -- a hardcoded "/var/lib/docker/" prefix would then
    # miss every overlay2/volume mount unit entirely and leak them into the sudoers rules.
    list_units_stdout = (
        "urphotos.mount loaded active mounted Mount urphotos\n"
        "mnt-docker-overlay2-def456-merged.mount loaded active mounted /mnt/docker/overlay2/def456/merged\n"
    )
    where_by_unit = {
        "urphotos.mount": "/urphotos",
        "mnt-docker-overlay2-def456-merged.mount": "/mnt/docker/overlay2/def456/merged",
    }
    monkeypatch.setattr(
        "subprocess.run",
        _fake_subprocess_run(list_units_stdout, where_by_unit, docker_root="/mnt/docker"),
    )
    assert _discover_mount_units() == ["urphotos.mount"]


def test_docker_root_dir_reports_docker_info(monkeypatch):
    import subprocess as _subprocess

    def fake_run(cmd, **kwargs):
        assert cmd == ["docker", "info", "--format", "{{.DockerRootDir}}"]
        return _subprocess.CompletedProcess(cmd, 0, stdout="/mnt/docker\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _docker_root_dir() == "/mnt/docker"


def test_docker_root_dir_falls_back_to_default_on_error(monkeypatch):
    import subprocess as _subprocess

    def fake_run(cmd, **kwargs):
        return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _docker_root_dir() == "/var/lib/docker"


def test_docker_root_dir_falls_back_to_default_when_docker_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _docker_root_dir() == "/var/lib/docker"


def test_collect_mounts_expands_tilde_and_rejects_relative(monkeypatch):
    # A real gap: a mount path typed as "~/nas" was stored literally in config.yaml.
    # casa_leela.py's mount check calls os.listdir() directly on that string, and
    # unlike a shell, os.listdir() never expands '~' -- a real, working mount would be
    # reported unreachable. Also verifies a genuinely relative path (no '~' involved)
    # is rejected and re-prompted rather than silently stored.
    import os

    import setup_wizard
    monkeypatch.setattr(setup_wizard, "_discover_mount_units", lambda: ["data.mount"])
    monkeypatch.setattr(setup_wizard, "_mount_where", lambda unit: "")
    responses = iter(["data.mount", "relative/path", "~/nas"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    mounts = _collect_mounts()
    assert mounts == {"data.mount": os.path.expanduser("~/nas")}
    assert os.path.isabs(mounts["data.mount"])
