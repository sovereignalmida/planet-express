"""
Allow/deny tests for casa_bender.py's read-only diagnostic allowlist
(the pre-planning tool loop Farnsworth uses to check real state before writing
a plan). Pure string/regex logic and a mocked command runner -- no real host,
no real subprocess execution.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import casa_bender as bender


def test_allows_docker_inspect(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "running", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    exit_code, stdout, stderr = bender.run_diagnostic(
        "docker inspect --format '{{.State.Status}}' CASA_GLUETON"
    )
    assert exit_code == 0
    assert stdout == "running"


def test_allows_journalctl(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    bender.run_diagnostic("journalctl -u casa-stacks.service -n 50 --no-pager")  # no raise


def test_allows_systemctl_status(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (3, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    bender.run_diagnostic("systemctl status casa-stacks.service")  # no raise


def test_denies_docker_restart(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("docker restart CASA_GLUETON")


def test_denies_sudo_smuggled_via_compound_command(monkeypatch):
    # Same class of attack _split_command_segments was built to catch for the sudo
    # allowlist: a read-only-looking first segment must not let a mutating second
    # segment through unchecked.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("docker ps && rm -rf /")


def test_denies_forbidden_stack_reference(monkeypatch):
    # Defense in depth: even a read-only-shaped command against a forbidden stack
    # must still be rejected by the normal _safety_check pass.
    monkeypatch.setattr(bender, "FORBIDDEN_STACKS", ["clawbot"])
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.SafetyError, match="Forbidden stack referenced"):
        bender.run_diagnostic("docker logs clawbot --tail 50")


def test_denies_env_file_read(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("cat network/.env")


def test_denies_redirection_smuggled_after_allowlisted_prefix(monkeypatch):
    # _split_command_segments doesn't split on redirection, so `docker ps > file`
    # would otherwise sail through as one allowlisted "docker ps ..." segment.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="redirection/substitution/background"):
        bender.run_diagnostic("docker ps > /tmp/pwned")


def test_denies_command_substitution(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="redirection/substitution/background"):
        bender.run_diagnostic("docker ps $(rm -rf /)")


def test_denies_backtick_substitution(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="redirection/substitution/background"):
        bender.run_diagnostic("docker ps `rm -rf /`")


def test_denies_backgrounded_second_command(monkeypatch):
    # A single `&` isn't split by _split_command_segments (only `&&` is), but the
    # shell still treats it as a job-control separator for a second command.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="redirection/substitution/background"):
        bender.run_diagnostic("docker ps & rm -rf /")


def test_allows_double_ampersand_compound_of_allowlisted_commands(monkeypatch):
    # Make sure the single-& check doesn't false-positive on the already-legitimate
    # && compound-command splitting path.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    bender.run_diagnostic("docker ps && docker logs CASA_GLUETON --tail 10")  # no raise


def test_denies_bare_docker_inspect_without_format(monkeypatch):
    # Bare `docker inspect` dumps the full container config, including Config.Env,
    # which routinely holds secrets -- must require a narrowing --format.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="must use --format"):
        bender.run_diagnostic("docker inspect CASA_GLUETON")


def test_denies_docker_inspect_format_referencing_env(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="known-safe"):
        bender.run_diagnostic("docker inspect --format '{{json .Config.Env}}' CASA_GLUETON")


def test_denies_docker_inspect_format_index_function_smuggling_config(monkeypatch):
    # `index . "Config"` reaches Config (and therefore Env) without the literal
    # substring ".Config" or "env" ever appearing -- a denylist of "dangerous"
    # substrings can't catch this, only an allowlist of exact safe field paths can.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="known-safe"):
        bender.run_diagnostic(
            'docker inspect --format \'{{json (index . "Config")}}\' CASA_GLUETON'
        )


def test_denies_journalctl_vacuum(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="state-mutating flag"):
        bender.run_diagnostic("journalctl --vacuum-time=1s -n 50")


def test_denies_journalctl_rotate(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("journalctl --rotate -n 50")


def test_denies_journalctl_abbreviated_rotate(monkeypatch):
    # GNU-style abbreviation of a long option (`--rot` for `--rotate`) must not
    # slip past a substring/exact-match check -- the allowlist-of-flags approach
    # rejects it because "--rot" itself isn't a recognized safe flag.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("journalctl --rot -n 50")


def test_denies_journalctl_follow(monkeypatch):
    # --follow never terminates -- would hang the bounded diagnostic call.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="not in the read-only allowlist"):
        bender.run_diagnostic("journalctl --follow -n 50")


def test_denies_journalctl_without_line_bound(monkeypatch):
    # No -n/--lines at all -- risks dumping the entire journal into memory.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="must use -n/--lines"):
        bender.run_diagnostic("journalctl -u casa-stacks.service --no-pager")


def test_denies_docker_logs_follow(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="never terminates"):
        bender.run_diagnostic("docker logs --follow --tail 50 CASA_GLUETON")


def test_denies_docker_logs_without_tail(monkeypatch):
    # No --tail at all -- risks dumping a container's entire log history into
    # memory before the later output-length slicing ever applies.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="must use --tail"):
        bender.run_diagnostic("docker logs CASA_GLUETON")


def test_denies_docker_inspect_format_dumping_whole_config(monkeypatch):
    # {{json .Config}} still exposes Config.Env even though it doesn't literally
    # contain the word "env" -- only matching against the small safe-field
    # allowlist (not a denylist of "dangerous" substrings) catches this.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="known-safe"):
        bender.run_diagnostic("docker inspect --format '{{json .Config}}' CASA_GLUETON")


def test_denies_docker_inspect_format_whole_object_dump(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="known-safe"):
        bender.run_diagnostic("docker inspect --format '{{json .}}' CASA_GLUETON")


def test_allows_docker_inspect_network_ip(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "172.20.0.5", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    bender.run_diagnostic(
        "docker inspect --format '{{.NetworkSettings.Networks.casa_net.IPAddress}}' "
        "CASA_GLUETON"
    )  # no raise


def test_allows_journalctl_since_with_value(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    bender.run_diagnostic("journalctl --since '1 hour ago' -n 50")  # no raise


def test_denies_docker_logs_follow_equals_form(monkeypatch):
    # `--follow=true` (or `-f=true`) is still the follow flag -- a bare `-f\s`
    # regex wouldn't catch the `=`-joined spelling.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="never terminates"):
        bender.run_diagnostic("docker logs --follow=true --tail 50 CASA_GLUETON")


def test_denies_docker_logs_tail_all(monkeypatch):
    # `--tail all` passes a presence-only check but still dumps the whole log.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("docker logs --tail all CASA_GLUETON")


def test_denies_docker_logs_tail_negative(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("docker logs --tail=-1 CASA_GLUETON")


def test_denies_journalctl_lines_all(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("journalctl -n all")


def test_denies_journalctl_lines_negative(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("journalctl --lines=-1")


def test_denies_docker_logs_tail_absurdly_large(monkeypatch):
    # A presence-only + "is it an integer" check would still accept an
    # unreasonably large --tail that dumps effectively the whole log anyway.
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("docker logs --tail 999999999999 CASA_GLUETON")


def test_denies_journalctl_lines_absurdly_large(monkeypatch):
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="integer between 1 and"):
        bender.run_diagnostic("journalctl -n 999999999999")


def test_denies_docker_inspect_decoy_safe_format_before_unsafe_one(monkeypatch):
    # docker uses the LAST --format value when the flag is repeated -- a safe
    # first --format can't be used as a decoy to sneak an unsafe second one past
    # a check that only looks at the first occurrence (or a raw-string scan that
    # doesn't parse shell quoting the way a real shell would).
    monkeypatch.setattr(bender, "_run_command", lambda cmd: (0, "", ""))
    monkeypatch.setattr(bender, "_log_step", lambda *a, **k: None)
    with pytest.raises(bender.DiagnosticNotAllowed, match="known-safe"):
        bender.run_diagnostic(
            "docker inspect --format '{{.State.Status}}' "
            "--format '{{json .Config.Env}}' CASA_GLUETON"
        )
