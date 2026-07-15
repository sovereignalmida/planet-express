"""
Allow/deny tests for casa_bender.py's sudo-scope enforcement (Spec 4). Pure
string/regex logic -- no real host, no real sudo, no real config.yaml needed.
Each test monkeypatches bender.SUDO_ALLOWLIST to a controlled fixture rather than
depending on whatever this host's real config.yaml happens to declare.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import casa_bender as bender
from config import SudoAllowlist, SudoGlobGrant, SudoUnitGrant


@pytest.fixture
def allowlist(monkeypatch):
    fixture = SudoAllowlist(
        units=[SudoUnitGrant(unit="casa-startup.service", actions=["start", "stop", "restart"])],
        globs=[SudoGlobGrant(glob="*.mount", actions=["start", "stop"])],
    )
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", fixture)
    return fixture


def test_allowed_unit_restart(allowlist):
    bender._safety_check("sudo systemctl restart casa-startup.service", plan={})  # no raise


def test_allowed_glob_start(allowlist):
    bender._safety_check("sudo systemctl start data.mount", plan={})  # no raise


def test_allowed_glob_stop(allowlist):
    bender._safety_check("sudo systemctl stop data.mount", plan={})  # no raise


def test_denies_restart_on_glob_grant_without_that_action(allowlist):
    # The *.mount glob only grants start/stop, not restart.
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._safety_check("sudo systemctl restart data.mount", plan={})


def test_denies_undeclared_unit(allowlist):
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._safety_check("sudo systemctl restart some-other.service", plan={})


def test_denies_non_systemctl_sudo_command(allowlist):
    # A real historical near-miss: Farnsworth once proposed `sudo mount -a` for a
    # tripped automount, which Bender has no grant for and this must reject outright.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check("sudo mount -a", plan={})


def test_denies_sudo_docker(allowlist):
    # docker needs no sudo -- any sudo-prefixed docker invocation is unexpected.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check("sudo docker ps", plan={})


def test_denies_smuggled_forbidden_command_in_compound(allowlist):
    # The first segment alone would be allowed -- only the second (unrelated sudo
    # mount -a) should trip the check, proving segments are validated independently
    # rather than only the whole string being pattern-matched.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check(
            "sudo systemctl start data.mount && sudo mount -a", plan={}
        )


def test_plain_docker_command_unaffected(allowlist):
    bender._safety_check("docker ps -a", plan={})  # no raise, not sudo-prefixed


def test_empty_allowlist_denies_everything(monkeypatch):
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", SudoAllowlist())
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._safety_check("sudo systemctl restart casa-startup.service", plan={})


# ── Bypass cases an independent Codex review caught before this shipped: a
# prefix-only ("does the segment start with sudo") check missed sudo invoked via a
# shell wrapper or on a later line of a multi-line command. ────────────────────────

def test_denies_sudo_wrapped_in_env(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check("env sudo mount -a", plan={})


def test_denies_sudo_wrapped_in_sh_c(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check("sh -c 'sudo mount -a'", plan={})


def test_denies_sudo_on_later_line(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check("echo hi\nsudo mount -a", plan={})


def test_denies_command_substitution_disguised_as_path_prefix(allowlist):
    # A real PoC an independent Codex review used to break an earlier version of this
    # check: an absolute-path allowance (`\S*/`) let `\S` match shell metacharacters
    # too, so a command substitution dressed up as a "path prefix" satisfied the
    # regex while the shell (shell=True) would still execute the embedded sudo call.
    # No path-prefix tolerance exists anymore -- this must be flatly rejected.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check(
            "$(sudo${IFS}mount${IFS}-a)/sudo systemctl restart casa-startup.service",
            plan={},
        )


def test_denies_command_substitution_disguised_as_unit_name(allowlist):
    # A second, distinct PoC an independent Codex review caught: `\S+` for the unit
    # name has no literal whitespace, so `$(sudo${IFS}mount${IFS}-a)data.mount`
    # matched the regex AND passed fnmatch("*.mount") since it happens to end in
    # ".mount" -- while the shell still executes the embedded substitution. The
    # strict systemd-unit-name character class must reject this outright.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check(
            "sudo systemctl start $(sudo${IFS}mount${IFS}-a)data.mount", plan={}
        )
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._safety_check(
            "sudo systemctl start `sudo${IFS}mount${IFS}-a`data.mount", plan={}
        )
