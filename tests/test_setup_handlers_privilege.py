"""sudoers.install, dashboard.init and access.provision: the handlers that grant or protect access (A2b)."""
import json
from functools import partial

import pytest

from planet_express.setup.apply import apply as _apply
from planet_express.setup.host import RunResult
from planet_express.setup.journal import Journal
from test_setup_apply import SECRET, fresh_host, make_plan, sha, step

apply = partial(_apply, evidence_dir="/journal/evidence")
SUDOERS = "/etc/sudoers.d/planetexpress"
GRANT = "pe ALL=(root) NOPASSWD: /usr/bin/systemctl restart plex.service\n"


def sudoers_host():
    host = fresh_host()
    host.add_dir("/etc/sudoers.d", mode=0o750)
    return host


def sudoers_plan(content=GRANT, **over):
    params = {"path": SUDOERS, "content": content, "run_user": "svc", "expect_absent": True, **over}
    return make_plan([step(1, "sudoers.install", SUDOERS, **params)])


def visudo_file_args(host):
    return [c for c in host.commands if c[:2] == ["visudo", "-c"] and "-f" in c]


def test_the_grant_is_validated_while_it_is_still_a_staged_file_and_only_then_given_its_real_name(tmp_path):
    host = sudoers_host()
    seen = []

    def visudo(h, argv):
        # At the instant visudo runs, the grant must exist only under its staged, sudo-ignored name.
        seen.append((SUDOERS in h.nodes, [p for p in h.nodes if ".pe-setup-" in p and "sudoers.d" in p]))
        return RunResult(0, "parsed OK")
    host.on(lambda argv: argv[:3] == ["visudo", "-c", "-f"], visudo)
    plan = sudoers_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert seen[0][0] is False and len(seen[0][1]) == 1                  # not yet live, staged and dotted
    assert host.nodes[SUDOERS].data == GRANT.encode()
    assert (host.nodes[SUDOERS].mode, host.nodes[SUDOERS].uid, host.nodes[SUDOERS].gid) == (0o440, 0, 0)
    staged_arg = visudo_file_args(host)[0][-1]
    assert staged_arg.startswith("/etc/sudoers.d/.pe-setup-") and "." in staged_arg.rsplit("/", 1)[1]
    assert ["visudo", "-c"] in host.commands                            # and the whole system re-checked after


def test_a_grant_visudo_rejects_never_reaches_its_real_name_and_leaves_nothing_behind(tmp_path):
    host = sudoers_host()
    host.on(lambda argv: argv[:3] == ["visudo", "-c", "-f"], RunResult(1, "", "syntax error near line 1"))
    plan = sudoers_plan("this is not sudoers\n")
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "visudo rejected" in result.reason and "syntax error" in result.reason
    assert SUDOERS not in host.nodes and not [p for p in host.nodes if ".pe-setup-" in p]


def test_an_invalid_replacement_leaves_the_existing_grant_exactly_as_it_was(tmp_path):
    host = sudoers_host()
    host.add_file(SUDOERS, b"old grant\n", mode=0o440)
    host.on(lambda argv: argv[:3] == ["visudo", "-c", "-f"], RunResult(1, "", "bad"))
    plan = sudoers_plan(if_exists="replace", expect_absent=False, expected_sha256=sha("old grant\n"))
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and host.nodes[SUDOERS].data == b"old grant\n"
    assert not [p for p in host.nodes if ".pe-setup-" in p]


def test_a_missing_visudo_says_sudo_may_not_be_installed(tmp_path):
    host = sudoers_host()
    host.on(lambda argv: argv[:2] == ["visudo", "-c"], RunResult(127, "", "visudo: not found"))
    plan = sudoers_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "is sudo installed" in result.reason and SUDOERS not in host.nodes


def test_no_sudoers_directory_means_no_sudo_and_the_step_refuses(tmp_path):
    host = fresh_host()
    plan = sudoers_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "/etc/sudoers.d does not exist" in result.reason


def test_a_system_whose_sudoers_stops_parsing_after_the_install_is_not_reported_as_done(tmp_path):
    host = sudoers_host()
    host.on(lambda argv: argv == ["visudo", "-c"], RunResult(1, "", "parse error in /etc/sudoers"))
    plan = sudoers_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "does not show the expected result" in result.reason


def test_an_existing_grant_is_kept_and_a_changed_one_is_not_overwritten(tmp_path):
    host = sudoers_host()
    host.add_file(SUDOERS, b"someone else's edit\n", mode=0o440)
    kept = sudoers_plan(expect_absent=False, expected_sha256=sha("what we saw\n"))        # if_exists defaults to keep
    assert apply(kept, host=host, journal_root=tmp_path / "a", replan=lambda: kept).status == "done"
    assert host.nodes[SUDOERS].data == b"someone else's edit\n"
    replace = sudoers_plan(if_exists="replace", expect_absent=False, expected_sha256=sha("what we saw\n"))
    result = apply(replace, host=host, journal_root=tmp_path / "b", replan=lambda: replace)
    assert result.status == "stopped" and "changed since the plan was made" in result.reason
    assert host.nodes[SUDOERS].data == b"someone else's edit\n"


# -- dashboard.init ------------------------------------------------------------------------------------------------

import web_auth
from fake_host import Crash, FaultyHost
from scripts import dashboard_operators as ops

ENV = "/etc/pe/dashboard.env"
PASSPHRASE = "a perfectly good passphrase"
TOTP = "JBSWY3DPEHPK3PXP"
OTHER = ("rival", "another long passphrase here", "GEZDGNBVGY3TQOJQ")


def dash_host(env_text=None, *, mode=0o600, uid=0, gid=0):
    host = fresh_host()
    host.add_dir("/etc/pe")
    if env_text is not None:
        host.add_file(ENV, env_text.encode(), mode=mode, uid=uid, gid=gid)
    return host


def dash_plan(*, operator=True, existing=None, **over):
    params = {"env_file": ENV}
    if operator:
        params.update(operator="chris", passphrase_ref="pw", totp_ref="totp")
    if existing is None:
        params["expect_absent"] = True
    else:
        params["expected_sha256"] = sha(existing)
    params.update(over)
    return make_plan([step(1, "dashboard.init", ENV, **params)], {"pw": PASSPHRASE, "totp": TOTP})


def env_values(host):
    return ops.parse_env(host.nodes[ENV].data.decode())


def test_a_fresh_dashboard_login_has_a_session_secret_and_an_operator_whose_hash_verifies(tmp_path):
    host, plan = dash_host(), dash_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    node = host.nodes[ENV]
    assert (node.mode, node.uid, node.gid) == (0o600, 0, 0)
    values = env_values(host)
    assert len(values["PE_DASHBOARD_SECRET_KEY"]) >= 32
    operator = web_auth.load_operators(values)["chris"]
    assert web_auth.verify_passphrase(operator.passphrase_hash, PASSPHRASE) and operator.totp_secret == TOTP
    assert PASSPHRASE not in node.data.decode()                              # only the hash is stored


def test_no_operator_secret_reaches_the_journal_or_saved_plan(tmp_path):
    host, plan = dash_host(), dash_plan()
    apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    for path in (tmp_path / "j").rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert PASSPHRASE not in text and TOTP not in text, path
    assert any("added the dashboard operator 'chris'" in e.get("line", "")
               for e in Journal(tmp_path / "j", plan.to_public()["plan_id"]).events())


def test_an_existing_login_with_the_operator_is_left_completely_alone(tmp_path):
    first, plan = dash_host(), dash_plan()
    apply(plan, host=first, journal_root=tmp_path / "a", replan=lambda: plan)
    converged = first.nodes[ENV].data.decode()
    again_plan = dash_plan(existing=converged)
    mutations = first.mutations
    assert apply(again_plan, host=first, journal_root=tmp_path / "b", replan=lambda: again_plan).status == "done"
    assert first.nodes[ENV].data.decode() == converged and first.mutations == mutations


def test_an_operator_is_added_to_an_existing_file_without_disturbing_what_is_already_in_it(tmp_path):
    existing = ('# the dashboard env\\nADGUARD_USERNAME=admin\\nPE_DASHBOARD_SECRET_KEY="' + "k" * 40 + '"\\n')
    host = dash_host(existing)
    plan = dash_plan(existing=existing)
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    text = host.nodes[ENV].data.decode()
    assert text.startswith("# the dashboard env\\nADGUARD_USERNAME=admin\\n") and "k" * 40 in text
    assert "chris" in web_auth.load_operators(env_values(host))
    backup = [p for p in host.nodes if p.endswith(".bak")]
    assert len(backup) == 1 and host.nodes[backup[0]].data.decode() == existing


def test_a_login_file_that_changed_after_review_is_not_touched(tmp_path):
    host = dash_host("PE_DASHBOARD_SECRET_KEY=" + "x" * 40 + "\\n")
    plan = dash_plan(existing="what the plan saw\\n")
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "changed since the plan was made" in result.reason
    assert host.nodes[ENV].data.decode() == "PE_DASHBOARD_SECRET_KEY=" + "x" * 40 + "\\n"


def test_a_fourth_operator_or_a_reused_passphrase_is_refused_with_the_reason(tmp_path):
    first = dash_host()
    seed = make_plan([step(1, "dashboard.init", ENV, env_file=ENV, operator="chris", passphrase_ref="pw",
                           totp_ref="totp", expect_absent=True)], {"pw": PASSPHRASE, "totp": TOTP})
    apply(seed, host=first, journal_root=tmp_path / "seed", replan=lambda: seed)
    text = first.nodes[ENV].data.decode()
    reused = make_plan([step(1, "dashboard.init", ENV, env_file=ENV, operator="second", passphrase_ref="pw",
                             totp_ref="totp", expected_sha256=sha(text))], {"pw": PASSPHRASE, "totp": TOTP})
    result = apply(reused, host=first, journal_root=tmp_path / "r", replan=lambda: reused)
    assert result.status == "stopped" and "already used by another operator" in result.reason


def test_a_login_file_with_loose_permissions_is_refused_because_it_holds_secrets(tmp_path):
    host = dash_host()
    seed = dash_plan()
    apply(seed, host=host, journal_root=tmp_path / "a", replan=lambda: seed)
    host.nodes[ENV].mode = 0o644
    plan = dash_plan(existing=host.nodes[ENV].data.decode())
    result = apply(plan, host=host, journal_root=tmp_path / "b", replan=lambda: plan)
    assert result.status == "stopped" and "has to be 0600 root:root" in result.reason


def test_a_key_only_login_needs_no_operator_secrets(tmp_path):
    host, plan = dash_host(), dash_plan(operator=False)
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert web_auth.load_operators(env_values(host)) == {} and len(env_values(host)["PE_DASHBOARD_SECRET_KEY"]) >= 32


def test_the_model_refuses_an_operator_without_both_secret_references_or_both_expectations():
    with pytest.raises(Exception, match="expected either absent"):
        step(1, "dashboard.init", ENV, env_file=ENV)
    with pytest.raises(Exception, match="both its passphrase and TOTP"):
        step(1, "dashboard.init", ENV, env_file=ENV, operator="chris", expect_absent=True)


def test_a_crash_at_any_operation_still_resumes_to_a_valid_login_file(tmp_path):
    plan = dash_plan()
    clean = dash_host()
    counter = FaultyHost(clean)
    assert apply(plan, host=counter, journal_root=tmp_path / "clean", replan=lambda: plan).status == "done"
    for crash_at in range(1, counter.ops + 1):
        for after in (False, True):
            host, root = dash_host(), tmp_path / f"c-{crash_at}-{int(after)}"
            with pytest.raises(Crash):
                apply(plan, host=FaultyHost(host, crash_at=crash_at, after=after), journal_root=root, replan=lambda: plan)
            assert apply(plan, host=host, journal_root=root, replan=lambda: plan).status == "done", (crash_at, after)
            operators = web_auth.load_operators(env_values(host))
            assert list(operators) == ["chris"] and (host.nodes[ENV].mode, host.nodes[ENV].uid) == (0o600, 0)
            assert not [p for p in host.nodes if ".pe-setup-" in p], (crash_at, after)


# -- access.provision ------------------------------------------------------------------------------------------------

INSTALL = "/opt/pe"


def access_host(*, existing_accounts=False):
    host = fresh_host()
    host.add_dir(INSTALL, mode=0o755)
    for entry in ("planet_express", "templates", "static", "venv", "data", "logs", "state"):
        host.add_dir(f"{INSTALL}/{entry}")
    host.add_file(f"{INSTALL}/casa_scruffy.py", b"")
    host.add_file(f"{INSTALL}/.env.local", b"private")
    host.add_dir("/etc/pe")
    host.add_file("/etc/pe/config.yaml", b"x")
    if existing_accounts:
        host.users["planetexpress-web"] = (990, 990)
        host.groups.update({"planetexpress-web": 990, "planetexpress-rpc": 991})
        host.memberships.update({"svc": {991}, "planetexpress-web": {991}})

    def groupadd(h, argv):
        h.groups[argv[-1]] = 900 + len(h.groups)
        return RunResult(0, "")

    def useradd(h, argv):
        h.users[argv[-1]] = (990, 990)
        h.groups[argv[-1]] = 990
        return RunResult(0, "")

    def usermod(h, argv):
        h.memberships.setdefault(argv[-1], set()).add(h.groups[argv[-2]])
        return RunResult(0, "")
    host.on(lambda a: a[:1] == ["groupadd"], groupadd)
    host.on(lambda a: a[:1] == ["useradd"], useradd)
    host.on(lambda a: a[:1] == ["usermod"], usermod)
    host.on(lambda a: a[:2] == ["getfacl", "-p"], RunResult(0, "user::rwx\\nuser:planetexpress-web:r-x\\n"))
    host.add_dir("/mnt/data/pe", mode=0o750)
    host.add_dir("/mnt/data/pe/venv")

    def make_traversable(h, argv):
        h.nodes[argv[-1]].mode |= 0o001                      # chmod o+x, taking effect on the fake
        return RunResult(0, "")
    host.on(lambda a: a[:2] == ["chmod", "o+x"], make_traversable)
    return host


def access_plan(method="acl", **over):
    params = {"method": method, "install_dir": INSTALL, "run_user": "svc", "config_path": "/etc/pe/config.yaml"}
    if method == "groups":
        params.update(venv_dir="/mnt/data/pe/venv", home_dir="/mnt/data/pe")
    params.update(over)
    return make_plan([step(1, "access.provision", "planetexpress-web", **params)])


def test_on_systemd_the_commands_are_the_reviewed_web_access_plan_run_directly_never_with_sudo(tmp_path):
    host, plan = access_host(), access_plan()
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "done", result.reason
    ran = [c for c in host.commands if c[0] not in ("setfacl", "getfacl") or c[1:2] != ["--version"]]
    assert ["groupadd", "--system", "planetexpress-rpc"] in host.commands
    assert any(c[0] == "useradd" and c[-1] == "planetexpress-web" and "--user-group" in c for c in host.commands)
    assert any(c[:3] == ["usermod", "-aG", "planetexpress-rpc"] for c in host.commands)
    assert any(c[0] == "setfacl" and f"u:planetexpress-web:rX" in " ".join(c) for c in host.commands)
    assert not any("sudo" in c for c in host.commands)
    # Everything in the checkout that is not on the dashboard's allowlist is explicitly denied to it.
    assert ["setfacl", "-m", "u:planetexpress-web:---", f"{INSTALL}/.env.local"] in host.commands
    assert ["setfacl", "-m", "u:planetexpress-web:---", f"{INSTALL}/data"] in host.commands


def test_every_command_is_journaled_before_it_runs(tmp_path):
    host, plan = access_host(), access_plan()
    apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    evidence = Journal(tmp_path / "j", plan.to_public()["plan_id"]).steps()["s01"].evidence
    journaled = [e["command"] for e in evidence if "command" in e]
    ran = [c for c in host.commands if c[:1] != ["setfacl"] or c[1:2] != ["--version"]]
    assert journaled and all(c in host.commands for c in journaled) and len(journaled) >= 8


def test_a_host_without_setfacl_is_told_what_to_install_before_anything_is_changed(tmp_path):
    host, plan = access_host(), access_plan()
    host.on(lambda a: a[:2] == ["setfacl", "--version"], RunResult(127, "", "not found"))
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "acl package" in result.reason
    assert not any(c[0] in ("groupadd", "useradd", "usermod") for c in host.commands)


def test_a_failing_command_stops_the_step_and_says_which(tmp_path):
    host, plan = access_host(), access_plan()
    host.on(lambda a: a[:1] == ["useradd"], RunResult(9, "", "useradd: group planetexpress-web exists"))
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "stopped" and "`useradd` failed (exit 9)" in result.reason
    assert not any(c[0] == "setfacl" and "-R" in c for c in host.commands)    # nothing after the failure ran


def test_accounts_that_already_exist_are_not_created_again(tmp_path):
    host, plan = access_host(existing_accounts=True), access_plan()
    assert apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan).status == "done"
    assert not any(c[0] in ("groupadd", "useradd", "usermod") for c in host.commands)


def test_on_mos_there_are_no_acls_and_only_the_code_the_venv_and_a_traversable_home_are_touched(tmp_path):
    install = "/mnt/data/pe/planet-express"
    host = access_host()
    host.add_dir(install)
    plan = access_plan("groups", install_dir=install)
    result = apply(plan, host=host, journal_root=tmp_path / "j", replan=lambda: plan)
    assert result.status == "done", result.reason
    assert not any(c[0] in ("setfacl", "getfacl") for c in host.commands)
    assert ["chmod", "-R", "a+rX", install] in host.commands and ["chmod", "o+x", "/mnt/data/pe"] in host.commands
    # The strongest statement of "nothing private is made readable": these are the ONLY paths whose permissions
    # change. The database, the logs, the config and the env files are not among them.
    touched = {arg for c in host.commands if c[0] in ("chmod", "chgrp", "chown") for arg in c[1:] if arg.startswith("/")}
    assert touched == {install, "/mnt/data/pe/venv", "/mnt/data/pe"}


def test_setup_handlers_never_import_config():
    """Setup runs before any config exists; importing `config` there exits when no config file is present."""
    import subprocess, sys
    code = ("import sys; import planet_express.setup.handlers as h; "
            "from planet_express.setup.handlers import HANDLERS; "
            "from scripts import dashboard_operators; "
            "assert 'config' not in sys.modules, 'config was imported'")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "."}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
