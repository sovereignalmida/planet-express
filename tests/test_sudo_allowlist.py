"""
Allow/deny tests for casa_bender.py's sudo-scope enforcement (Spec 4). Pure string/regex logic --
no real host, no real sudo, no real config.yaml needed. Since slice 5b-5 deleted `_safety_check`'s
pattern list, `_check_sudo_allowlist` *is* the enforcement, and these call it directly.
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
        units=[SudoUnitGrant(unit="casa-stacks.service", actions=["start", "stop", "restart"])],
        globs=[SudoGlobGrant(glob="*.mount", actions=["start", "stop"])],
    )
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", fixture)
    # Explicit, not assumed: this fixture's claim to be "config-independent" only holds if
    # HOST_CONTROL_PROVIDER is pinned to the shape every test below actually exercises --
    # an ambient "mos" value (e.g. from a hand-edited config.yaml on the machine running
    # these tests) would otherwise silently flip which regex these commands are checked
    # against, and every "no raise" test below would start raising for the wrong reason.
    monkeypatch.setattr(bender, "HOST_CONTROL_PROVIDER", "systemd")
    return fixture


def test_allowed_unit_restart(allowlist):
    bender._check_sudo_allowlist("sudo systemctl restart casa-stacks.service")  # no raise


def test_allowed_glob_start(allowlist):
    bender._check_sudo_allowlist("sudo systemctl start data.mount")  # no raise


def test_allowed_glob_stop(allowlist):
    bender._check_sudo_allowlist("sudo systemctl stop data.mount")  # no raise


def test_denies_restart_on_glob_grant_without_that_action(allowlist):
    # The *.mount glob only grants start/stop, not restart.
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._check_sudo_allowlist("sudo systemctl restart data.mount")


def test_denies_undeclared_unit(allowlist):
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._check_sudo_allowlist("sudo systemctl restart some-other.service")


def test_denies_non_systemctl_sudo_command(allowlist):
    # A real historical near-miss: Farnsworth once proposed `sudo mount -a` for a
    # tripped automount, which Bender has no grant for and this must reject outright.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo mount -a")


def test_denies_sudo_docker(allowlist):
    # docker needs no sudo -- any sudo-prefixed docker invocation is unexpected.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo docker ps")


def test_denies_smuggled_forbidden_command_in_compound(allowlist):
    # The first segment alone would be allowed -- only the second (unrelated sudo
    # mount -a) should trip the check, proving segments are validated independently
    # rather than only the whole string being pattern-matched.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist(
            "sudo systemctl start data.mount && sudo mount -a")


def test_plain_docker_command_unaffected(allowlist):
    bender._check_sudo_allowlist("docker ps -a")  # no raise, not sudo-prefixed


def test_empty_allowlist_denies_everything(monkeypatch):
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", SudoAllowlist())
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._check_sudo_allowlist("sudo systemctl restart casa-stacks.service")


# ── Bypass cases an independent Codex review caught before this shipped: a
# prefix-only ("does the segment start with sudo") check missed sudo invoked via a
# shell wrapper or on a later line of a multi-line command. ────────────────────────

def test_denies_sudo_wrapped_in_env(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("env sudo mount -a")


def test_denies_sudo_wrapped_in_sh_c(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sh -c 'sudo mount -a'")


def test_denies_sudo_on_later_line(allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("echo hi\nsudo mount -a")


def test_denies_command_substitution_disguised_as_path_prefix(allowlist):
    # A real PoC an independent Codex review used to break an earlier version of this
    # check: an absolute-path allowance (`\S*/`) let `\S` match shell metacharacters
    # too, so a command substitution dressed up as a "path prefix" satisfied the
    # regex while the shell (shell=True, deleted in slice 5b-5) would still execute the embedded
    # sudo call. No path-prefix tolerance exists anymore -- this must be flatly rejected, and the
    # check stays strict even with no shell behind it: it decides what sudo may do.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist(
            "$(sudo${IFS}mount${IFS}-a)/sudo systemctl restart casa-stacks.service")


def test_denies_command_substitution_disguised_as_unit_name(allowlist):
    # A second, distinct PoC an independent Codex review caught: `\S+` for the unit
    # name has no literal whitespace, so `$(sudo${IFS}mount${IFS}-a)data.mount`
    # matched the regex AND passed fnmatch("*.mount") since it happens to end in
    # ".mount" -- while the shell (since deleted) still executed the embedded substitution. The
    # strict systemd-unit-name character class must reject this outright.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist(
            "sudo systemctl start $(sudo${IFS}mount${IFS}-a)data.mount")
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist(
            "sudo systemctl start `sudo${IFS}mount${IFS}-a`data.mount")


# ── MOS: a different command family under HOST_CONTROL_PROVIDER == "mos" ───────────────
# (docs/designs/mos-sudo-gate-scoping.md) -- unit/action swap position relative to
# systemctl's shape, and only one shape is ever accepted per install.


@pytest.fixture
def mos_allowlist(monkeypatch):
    fixture = SudoAllowlist(
        units=[SudoUnitGrant(unit="docker", actions=["start", "stop", "restart"])],
        globs=[SudoGlobGrant(glob="cron*", actions=["start", "stop"])],
    )
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", fixture)
    monkeypatch.setattr(bender, "HOST_CONTROL_PROVIDER", "mos")
    return fixture


def test_mos_allowed_unit_restart(mos_allowlist):
    bender._check_sudo_allowlist("sudo service docker restart")  # no raise


def test_mos_allowed_glob_start(mos_allowlist):
    bender._check_sudo_allowlist("sudo service cron-watchdog start")  # no raise


def test_mos_denies_restart_on_glob_grant_without_that_action(mos_allowlist):
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._check_sudo_allowlist("sudo service cron-watchdog restart")


def test_mos_denies_undeclared_unit(mos_allowlist):
    with pytest.raises(bender.SafetyError, match="not declared"):
        bender._check_sudo_allowlist("sudo service ssh restart")


def test_mos_rejects_systemctl_shaped_command_even_if_declared(mos_allowlist):
    """An install running "mos" must not also accept the systemd shape -- that would
    grant a command surface this host's actual provider never issues, for a unit the
    operator only ever reasoned about in `service`-command terms."""
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo systemctl restart docker")


def test_systemd_install_rejects_service_shaped_command(allowlist):
    """The reverse of the above: a default ("systemd") install must not accept the
    `service`-command shape even for an otherwise-declared unit name."""
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo service casa-stacks.service restart")


def test_mos_rejects_systemd_unit_charset(mos_allowlist):
    """A unit name containing '.', valid for systemd, is not a valid /etc/init.d/*
    script name -- the MOS regex's narrower charset must reject it outright rather
    than silently matching a unit that could never exist on a sysvinit host."""
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo service docker.service restart")


def test_mos_rejects_reversed_argument_order(mos_allowlist):
    """`sudo service <action> <unit>` (systemctl's order) must not parse as the MOS
    shape just because both tokens happen to look unit-shaped."""
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo service restart docker")


def test_mos_denies_command_substitution_disguised_as_unit_name(mos_allowlist):
    # Same PoC class an independent Codex review caught for the systemd regex, re-run
    # against the MOS one: the narrower [A-Za-z0-9_-]+ charset has no '$', '(', ')', or
    # whitespace in it at all, so this must be flatly rejected.
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist(
            "sudo service $(sudo${IFS}mount${IFS}-a)docker restart")


def test_mos_denies_smuggled_forbidden_command_in_compound(mos_allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo service docker restart && sudo mount -a")


def test_mos_denies_sudo_on_later_line(mos_allowlist):
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("echo hi\nsudo mount -a")


def test_mos_denies_extra_trailing_argument(mos_allowlist):
    """The pattern is fully anchored ($ at the end) -- a plausible-looking command with
    one extra token must not match just because it starts right."""
    with pytest.raises(bender.SafetyError, match="not in the declared allowlist"):
        bender._check_sudo_allowlist("sudo service docker restart now")
