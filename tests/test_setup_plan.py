"""The setup core's plan step: invariants from docs/designs/setup-plan.md, one test each."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from planet_express.setup.answers import FORBIDDEN_RISKS_BY_TIER, SetupAnswers
from planet_express.setup.discover import discover
from planet_express.setup.plan import MASK, plan
from planet_express.setup.steps import CATALOGUE
from test_setup_discover import mos, ubuntu

REPO = str(Path(__file__).resolve().parent.parent)
TOKEN = "123456:TOKEN-VALUE-abc"
KEY = "sk-test-KEY-VALUE-999"
PASS = "correct horse battery staple"
TOTP = "JBSWY3DPEHPK3PXP"
SECRET_VALUES = (TOKEN, KEY, PASS, TOTP, "555000111")


def answers(**over):
    base = {"run_as": "pe", "install_dir": "/opt/pe", "stacks_root": "/home/pe/stacks",
            "telegram": {"token": TOKEN, "chat_id": "555000111"},
            "llm": {"provider": "openai", "api_key": KEY},
            "operator": {"name": "chris", "passphrase": PASS, "totp_secret": TOTP}}
    return SetupAnswers.model_validate({**base, **over})


def mos_answers(**over):
    base = {"run_as": "root", "accept_root_service": True, "install_dir": "/mnt/data/pe/planet-express",
            "stacks_root": "/mnt/data/stacks"}
    return answers(**{**base, **over})


def systemd_report(**kw):
    return discover(ubuntu(**kw), repo_root="/opt/pe")


def mos_report(**kw):
    return discover(mos(**kw), repo_root="/mnt/data/pe/planet-express")


def kinds(p):
    return [s.kind for s in p.steps]


def by_kind(p, kind):
    return [s for s in p.steps if s.kind == kind]


def test_a_fresh_observe_install_on_systemd_has_the_steps_of_deploy_sh_and_nothing_more():
    p = plan(systemd_report(), answers())
    assert p.applicable and not p.blocked
    assert kinds(p) == ["dir.ensure", "dir.ensure", "dir.ensure", "dir.ensure", "python.env", "file.write",
                        "file.write", "access.provision", "dashboard.init", "service.install", "service.install",
                        "service.enable", "service.enable", "verify.smoke"]
    units = {s.target for s in by_kind(p, "service.install")}
    assert units == {"/etc/systemd/system/casa-planetexpress.service", "/etc/systemd/system/casa-dashboard.service"}
    assert "sudoers.install" not in kinds(p)               # observe asks for no privileged commands


def test_every_step_is_a_catalogue_kind_with_known_dependencies_declared_earlier():
    p = plan(systemd_report(), answers(tier="stacks"))
    seen = set()
    for step in p.steps:
        assert step.kind in CATALOGUE and step.risk in ("R0", "R1", "R2", "R3")
        assert set(step.depends_on) <= seen, f"{step.id} depends on something not yet declared"
        seen.add(step.id)


def test_config_yaml_is_valid_secret_free_and_carries_the_tier_as_forbidden_risks():
    from config_schema import PlanetExpressConfig
    for tier, forbidden in FORBIDDEN_RISKS_BY_TIER.items():
        p = plan(systemd_report(), answers(tier=tier, ignored_stacks=["ai"]))
        config = next(s for s in p.steps if s.target == "/etc/planetexpress/config.yaml")
        loaded = PlanetExpressConfig(**yaml.safe_load(config.params["content"]))
        assert loaded.autonomy.forbidden_risks == forbidden
        assert loaded.forbidden_stacks == ["ai"] and str(loaded.stacks_root) == "/home/pe/stacks"
        assert not any(v in config.params["content"] for v in SECRET_VALUES)
        assert "TG_BOT_TOKEN" not in config.params["content"]


def test_no_secret_value_appears_anywhere_in_the_public_plan_but_the_values_are_kept_beside_it():
    p = plan(systemd_report(), answers())
    public = json.dumps(p.to_public(), ensure_ascii=False)
    for value in SECRET_VALUES:
        assert value not in public
    env = next(s for s in p.steps if s.target == "/etc/planetexpress.env")
    assert MASK in env.preview["content"] and "{{secret:telegram_token}}" in env.params["content"]
    assert env.params["mode"] == "0600" and env.params["has_secrets"] is True
    assert set(p.secrets.values()) == set(SECRET_VALUES)
    assert "TOKEN-VALUE" not in repr(p)                      # nor in a repr that might reach a log


def test_the_plan_id_is_deterministic_and_does_not_depend_on_secret_values():
    first = plan(systemd_report(), answers()).to_public()["plan_id"]
    assert first == plan(systemd_report(), answers()).to_public()["plan_id"]
    other_secret = answers(llm={"provider": "openai", "api_key": "sk-different-value"})
    assert first == plan(systemd_report(), other_secret).to_public()["plan_id"]
    assert first != plan(systemd_report(), answers(tier="stacks")).to_public()["plan_id"]


def test_the_service_never_runs_as_root_on_a_systemd_host():
    p = plan(systemd_report(), answers(run_as="root", accept_root_service=True))
    assert not p.applicable and p.steps == ()
    assert any("must not run as root" in reason for reason in p.blocked)


def test_on_mos_root_is_required_and_must_be_acknowledged():
    unacknowledged = plan(mos_report(), answers(run_as="root", install_dir="/mnt/data/pe/planet-express",
                                                stacks_root="/mnt/data/stacks"))
    assert not unacknowledged.applicable and "acknowledg" in unacknowledged.blocked[0]
    unprivileged = plan(mos_report(), answers(install_dir="/mnt/data/pe/planet-express", stacks_root="/mnt/data/stacks"))
    assert not unprivileged.applicable and "has to run as root" in unprivileged.blocked[0]
    ok = plan(mos_report(), mos_answers())
    assert ok.applicable and any("run as root" in w for w in ok.warnings)


def test_a_mos_plan_installs_on_the_pool_with_init_scripts_boot_hooks_and_no_sudo():
    p = plan(mos_report(), mos_answers(start_services=True))
    assert p.applicable
    assert "sudoers.install" not in kinds(p)
    access = by_kind(p, "access.provision")[0]
    assert access.params["method"] == "groups"
    assert {s.target for s in by_kind(p, "service.install")} == {"/etc/init.d/casa-planetexpress",
                                                                  "/etc/init.d/casa-dashboard"}
    hook = by_kind(p, "boot_hook.install")[0]
    assert hook.risk == "R3" and hook.target == "/boot/optional/scripts"
    assert "PE_HOME=/mnt/data/pe\n" in hook.params["hooks"]["post-start.sh"]
    assert [s.params["start"] for s in by_kind(p, "service.enable")] == [True, True]
    config = next(s for s in p.steps if s.target == "/mnt/data/pe/config.yaml")
    assert yaml.safe_load(config.params["content"])["host_control_provider"] == "mos"


def test_the_mos_boot_hook_follows_the_install_when_the_pool_has_another_name():
    report = mos_report(files={"/proc/mounts": "rootfs / rootfs rw 0 0\n/dev/vdb1 /mnt/tank ext4 rw 0 0\n"},
                        free={"/mnt/tank": 50.0})
    p = plan(report, answers(run_as="root", accept_root_service=True,
                             install_dir="/mnt/tank/pe/planet-express", stacks_root="/mnt/tank/stacks"))
    assert "PE_HOME=/mnt/tank/pe\n" in by_kind(p, "boot_hook.install")[0].params["hooks"]["post-start.sh"]


def test_mos_refuses_an_install_that_is_not_on_a_pool_because_root_is_ram():
    p = plan(mos_report(), mos_answers(install_dir="/opt/pe"))
    assert not p.applicable and any("not on one" in r for r in p.blocked)


def test_unit_control_is_unavailable_on_mos_because_it_goes_through_sudo():
    p = plan(mos_report(), mos_answers(tier="full"))
    assert not p.applicable and any("not available on MOS" in r for r in p.blocked)


def test_canary_updates_and_stack_boot_come_only_with_the_stacks_tier():
    for tier, wants_stacks_unit in (("observe", False), ("restart", False), ("stacks", True), ("full", True)):
        p = plan(systemd_report(), answers(tier=tier))
        assert ("casa-stacks" in {s.params["name"] for s in by_kind(p, "service.install")}) is wants_stacks_unit, tier
    adopted = plan(systemd_report(), answers(tier="stacks", story="adopt"))
    assert "casa-stacks" not in {s.params["name"] for s in by_kind(adopted, "service.install")}


def test_sudoers_is_written_only_for_the_full_tier_with_listed_units_and_is_r3():
    none_listed = plan(systemd_report(), answers(tier="full"))
    assert "sudoers.install" not in kinds(none_listed) and any("no sudo grant" in w for w in none_listed.warnings)
    p = plan(systemd_report(), answers(tier="full", sudo_units=[{"unit": "plex.service", "actions": ["restart"]}]))
    sudoers = by_kind(p, "sudoers.install")[0]
    assert sudoers.risk == "R3" and sudoers.needs_root and "plex.service" in sudoers.params["content"]
    assert sudoers.preview["content"] == sudoers.params["content"]


def test_system_paths_are_r2_and_need_root_but_files_under_the_install_do_not():
    p = plan(systemd_report(), answers())
    config = next(s for s in p.steps if s.target == "/etc/planetexpress/config.yaml")
    assert config.risk == "R2" and config.needs_root
    inside = plan(systemd_report(), answers(install_dir="/opt/pe")).steps[0]
    assert inside.kind == "dir.ensure" and inside.risk == "R1" and not inside.needs_root


def test_existing_files_are_kept_never_replaced_and_the_plan_says_so():
    report = systemd_report(files={"/etc/planetexpress.env": ""})
    p = plan(report, answers())
    env = next(s for s in p.steps if s.target == "/etc/planetexpress.env")
    assert env.params["if_exists"] == "keep"
    assert any("already installed" in w for w in p.warnings)
    assert any("existing /etc/planetexpress.env is kept" in line for line in p.will_not_touch)


def test_blocking_discovery_checks_block_the_plan_with_the_reason_and_no_steps():
    no_docker = discover(__import__("test_setup_discover").FakeEnv(
        files={"/etc/os-release": "ID=ubuntu\n", "/proc/mounts": "/dev/sda1 / ext4 rw 0 0\n"},
        dirs={"/run/systemd/system"}), repo_root="/opt/pe")
    p = plan(no_docker, answers())
    assert not p.applicable and p.steps == () and any("Docker" in r for r in p.blocked)


def test_missing_telegram_is_allowed_but_services_are_not_started_and_the_consequence_is_stated():
    p = plan(systemd_report(), answers(telegram=None, start_services=True))
    assert p.applicable
    assert all(s.params["start"] is False for s in by_kind(p, "service.enable"))
    assert any("No Telegram credentials" in w for w in p.warnings)
    assert any("cannot start without Telegram" in w for w in p.warnings)
    assert not any(s.params.get("has_secrets") and "TG_BOT_TOKEN" in s.params.get("content", "") for s in p.steps)


def test_will_not_touch_promises_the_things_setup_must_not_do():
    p = plan(systemd_report(), answers(ignored_stacks=["ai"]))
    joined = " ".join(p.will_not_touch)
    assert "No running container is stopped" in joined and "No compose file" in joined
    assert "Ignored stacks are never read, started or stopped: ai" in joined
    assert "No secret is written to config.yaml" in joined
    assert "forbidden from every action that changes something" in joined


def test_unsafe_answers_are_rejected_before_a_plan_exists():
    with pytest.raises(ValidationError):
        answers(install_dir="/opt/my pe")                       # whitespace breaks unit directives
    with pytest.raises(ValidationError):
        answers(install_dir="/opt/../etc")
    with pytest.raises(ValidationError):
        answers(operator={"name": "chris", "passphrase": "short", "totp_secret": TOTP})
    with pytest.raises(ValidationError):
        answers(telegram={"token": 'abc"; rm -rf /', "chat_id": "1"})
    with pytest.raises(ValidationError):
        answers(unknown_field=True)
    with pytest.raises(ValidationError):
        answers(dashboard_port=70000)


def test_the_cli_plans_from_files_and_never_imports_config(tmp_path):
    (tmp_path / "answers.json").write_text(json.dumps({
        "run_as": "pe", "install_dir": "/opt/pe", "stacks_root": "/home/pe/stacks"}))
    (tmp_path / "report.json").write_text(json.dumps(systemd_report()))
    code = ("import sys, json; from planet_express.setup.__main__ import main; "
            f"main(['plan', '--answers', r'{tmp_path / 'answers.json'}', '--discovery', r'{tmp_path / 'report.json'}']); "
            "sys.stderr.write('CONFIG_IMPORTED' if 'config' in sys.modules else 'clean')")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, check=True)
    assert out.stderr.strip() == "clean"
    assert json.loads(out.stdout)["applicable"] is True


# -- adopting a host that already has Planet Express: keep what is there, say only true things ---------

LIVE_UNIT = ("[Service]\nWorkingDirectory=/home/pe/apps/pe\n"
             "Environment=CASA_CONFIG=/home/pe/apps/pe/config.yaml\n")
LIVE_FILES = {"/etc/systemd/system/casa-planetexpress.service": LIVE_UNIT,
              "/etc/systemd/system/casa-dashboard.service": "", "/etc/systemd/system/casa-stacks.service": "",
              "/etc/planetexpress.env": "", "/etc/planetexpress-dashboard.env": "",
              "/etc/sudoers.d/planetexpress": "", "/home/pe/apps/pe/config.yaml": ""}


def live_plan(**over):
    report = discover(ubuntu(files=LIVE_FILES), repo_root="/home/pe/apps/pe")
    base = {"story": "adopt", "install_dir": "/home/pe/apps/pe", "telegram": None, "llm": None, "operator": None}
    return plan(report, answers(**{**base, **over}))


def test_an_existing_install_keeps_its_config_where_it_is_and_creates_no_second_one():
    p = live_plan()
    assert p.applicable
    config = next(s for s in p.steps if s.kind == "file.write")
    assert config.target == "/home/pe/apps/pe/config.yaml" and config.params["if_exists"] == "keep"
    assert config.preview["already_present"] is True and "already exists: kept" in config.title
    assert not any(s.target == "/etc/planetexpress" for s in p.steps)       # no stray second config directory
    assert not any(s.target == "/etc/planetexpress/config.yaml" for s in p.steps)


def test_existing_units_are_kept_not_overwritten():
    p = live_plan()
    units = by_kind(p, "service.install")
    assert units and all(s.params["if_exists"] == "keep" for s in units)
    assert any("existing service file /etc/systemd/system/casa-dashboard.service is kept" in line
               for line in p.will_not_touch)


def test_a_kept_config_means_the_chosen_tier_is_not_applied_and_the_plan_says_so_truthfully():
    p = live_plan(tier="observe")
    joined = " ".join(p.will_not_touch)
    assert "is kept unchanged. The power tier you chose is not applied to it" in joined
    assert "forbidden from every action that changes something" not in joined     # that would be false here
    assert any("is not applied to it" in w for w in p.warnings)


def test_a_new_config_does_apply_the_tier_and_promises_accordingly():
    joined = " ".join(plan(systemd_report(), answers(tier="observe")).will_not_touch)
    assert "forbidden from every action that changes something" in joined and "is kept unchanged" not in joined


def test_warnings_about_missing_secrets_are_dropped_when_those_files_already_exist():
    warnings = " ".join(live_plan().warnings)
    assert "No Telegram" not in warnings and "No LLM" not in warnings and "No dashboard operator" not in warnings
    fresh = " ".join(plan(systemd_report(), answers(telegram=None, llm=None, operator=None)).warnings)
    assert "No Telegram" in fresh and "No LLM" in fresh and "No dashboard operator" in fresh


def test_an_existing_dashboard_login_with_nothing_to_add_is_left_out_of_the_plan():
    assert "dashboard.init" not in kinds(live_plan())
    adding = live_plan(operator={"name": "chris", "passphrase": PASS, "totp_secret": TOTP})
    step = by_kind(adding, "dashboard.init")[0]
    assert "already exists and is kept" in step.preview["text"] and "chris" in step.preview["text"]
    assert not any(v in json.dumps(adding.to_public()) for v in (PASS, TOTP))


def test_existing_sudoers_and_casa_stacks_are_left_alone_and_not_claimed_absent():
    joined = " ".join(live_plan().will_not_touch)
    assert "existing sudoers grant is left as it is" in joined
    assert "casa-stacks.service is left as it is" in joined
    assert "No sudoers grant is written" not in joined and "casa-stacks is not installed" not in joined


def test_mos_creates_the_pool_home_before_anything_inside_it():
    p = plan(mos_report(), mos_answers())
    dirs = [s.target for s in by_kind(p, "dir.ensure")]
    assert dirs[0] == "/mnt/data/pe" and dirs.index("/mnt/data/pe") < dirs.index("/mnt/data/pe/state")


# -- Codex review of the plan step ------------------------------------------------------------------


def test_the_mos_boot_hook_names_the_real_checkout_directory_not_an_assumed_one():
    import re

    def assignments(p):
        hook = by_kind(p, "boot_hook.install")[0].params["hooks"]["post-start.sh"]
        return dict(re.findall(r"^\s*(PE_HOME|CHECKOUT)=(\S+)", hook, re.MULTILINE))

    assert assignments(plan(mos_report(), mos_answers())) == {"PE_HOME": "/mnt/data/pe", "CHECKOUT": "planet-express"}
    custom = plan(mos_report(), mos_answers(install_dir="/mnt/data/pe/custom"))
    assert assignments(custom) == {"PE_HOME": "/mnt/data/pe", "CHECKOUT": "custom"}


def test_a_preview_never_disagrees_with_its_steps_if_exists():
    """The review screen would otherwise claim an overwrite that apply will not perform."""
    plans = [plan(systemd_report(), answers()), plan(mos_report(), mos_answers()), live_plan(),
             plan(systemd_report(), answers(tier="full", sudo_units=[{"unit": "plex.service"}]))]
    checked = 0
    for p in plans:
        for step in p.steps:
            if step.preview.get("type") == "file" and "if_exists" in step.params:
                assert step.preview["if_exists"] == step.params["if_exists"], step.target
                checked += 1
    assert checked >= 10


def test_a_kept_config_means_no_sudoers_is_derived_from_the_answers():
    kept = discover(ubuntu(files={"/etc/planetexpress/config.yaml": ""}), repo_root="/opt/pe")
    p = plan(kept, answers(tier="full", sudo_units=[{"unit": "plex.service"}]))
    assert p.applicable and "sudoers.install" not in kinds(p)
    assert any("kept config decides which units" in w for w in p.warnings)
    fresh = plan(systemd_report(), answers(tier="full", sudo_units=[{"unit": "plex.service"}]))
    assert "sudoers.install" in kinds(fresh)


# -- A0: compare-and-swap expectations, the snapshot step, and a boot hook that merges ----------------


def file_params(p, target):
    return next(s for s in p.steps if s.target == target).params


def test_a_new_file_is_expected_to_stay_absent_and_an_existing_one_to_keep_its_hash():
    import hashlib
    p = plan(systemd_report(), answers())
    config = file_params(p, "/etc/planetexpress/config.yaml")
    assert config["expect_absent"] is True and "expected_sha256" not in config
    live = live_plan()
    kept = file_params(live, "/home/pe/apps/pe/config.yaml")
    assert kept["expected_sha256"] == hashlib.sha256(b"").hexdigest() and not kept.get("expect_absent")


def test_a_replace_is_always_bound_to_what_planning_saw_on_mos():
    for step in by_kind(plan(mos_report(), mos_answers()), "service.install"):
        assert step.params["if_exists"] == "replace"
        assert step.params.get("expect_absent") or step.params.get("expected_sha256")


def test_a_file_that_exists_but_cannot_be_hashed_is_kept_not_replaced_with_a_warning():
    report = mos_report(files={"/etc/init.d/casa-dashboard": "x"})
    report["existing_pe"]["sha256"].pop("/etc/init.d/casa-dashboard")
    p = plan(report, mos_answers())
    dashboard = next(s for s in by_kind(p, "service.install") if s.params["name"] == "casa-dashboard")
    assert dashboard.params["if_exists"] == "keep"
    assert any("could not be read to compare" in w for w in p.warnings)


def test_replacing_without_an_expectation_cannot_be_represented():
    from planet_express.setup.steps import FileWrite, ServiceInstall, SudoersInstall
    with pytest.raises(ValidationError, match="expectation"):
        FileWrite(path="/etc/x", content="", if_exists="replace")
    with pytest.raises(ValidationError, match="expectation"):
        SudoersInstall(path="/etc/sudoers.d/x", content="", run_user="pe", if_exists="replace")
    with pytest.raises(ValidationError, match="both absent and present"):
        FileWrite(path="/etc/x", content="", expect_absent=True, expected_sha256="0" * 64)
    assert FileWrite(path="/etc/x", content="", if_exists="replace", expect_absent=True).expect_absent


def test_an_existing_install_is_snapshotted_first_and_everything_waits_for_it():
    p = live_plan()
    assert p.steps[0].kind == "state.snapshot" and p.steps[0].risk == "R1"
    assert "pre-setup" not in json.dumps(p.steps[0].preview)             # a description, never a command
    assert all(p.steps[0].id in s.depends_on for s in p.steps[1:])
    assert "state.snapshot" not in kinds(plan(systemd_report(), answers()))


def test_the_boot_hook_is_a_merged_block_that_never_exits():
    hook = by_kind(plan(mos_report(), mos_answers()), "boot_hook.install")[0]
    assert hook.params["marker"] == "planetexpress"
    for name, body in hook.params["hooks"].items():
        assert not any(line.strip().startswith("exit") for line in body.splitlines()), name
        assert not body.startswith("#!"), "a shebang belongs to the file, not to a merged block"
    assert all("marked block" in f["merge"] for f in hook.preview["files"])
    from planet_express.setup.steps import BootHookInstall
    with pytest.raises(ValidationError, match="must not exit"):
        BootHookInstall(dest_dir="/boot/optional/scripts", hooks={"post-start.sh": "do_things\nexit 0\n"})


def test_an_existing_hook_file_is_reported_and_its_hash_is_carried_for_the_merge():
    existing = "#!/bin/sh\necho my own boot command\n"
    report = mos_report(files={"/boot/optional/scripts/post-start.sh": existing})
    hook = by_kind(plan(report, mos_answers()), "boot_hook.install")[0]
    import hashlib
    assert hook.params["expected_sha256"] == {"post-start.sh": hashlib.sha256(existing.encode()).hexdigest()}
    by_path = {f["path"]: f for f in hook.preview["files"]}
    assert by_path["/boot/optional/scripts/post-start.sh"]["already_present"] is True
    assert by_path["/boot/optional/scripts/shutdown.sh"]["already_present"] is False


def test_a_hook_file_that_is_exactly_an_old_copy_of_ours_is_recognised_and_anything_else_is_merged_into():
    from planet_express.setup.plan import LEGACY_HOOK_SHA256
    hook_path = "/boot/optional/scripts/post-start.sh"
    mine = mos_report(files={hook_path: "x"})
    mine["existing_pe"]["sha256"][hook_path] = LEGACY_HOOK_SHA256["post-start.sh"][1]     # byte-identical to a shipped copy
    ours = by_kind(plan(mine, mos_answers()), "boot_hook.install")[0]
    assert "older copy of ours" in {f["path"]: f for f in ours.preview["files"]}[hook_path]["merge"]
    theirs = by_kind(plan(mos_report(files={hook_path: "echo operator\n"}), mos_answers()), "boot_hook.install")[0]
    assert "marked block" in {f["path"]: f for f in theirs.preview["files"]}[hook_path]["merge"]
    assert theirs.params["legacy_sha256"] == LEGACY_HOOK_SHA256


def test_the_legacy_hashes_are_the_real_shipped_versions():
    """Guard against a typo in the list: the last whole-file post-start.sh and shutdown.sh are in it."""
    import hashlib, subprocess
    from planet_express.setup.plan import LEGACY_HOOK_SHA256
    for name, commit in (("post-start.sh", "19eb14d"), ("shutdown.sh", "f4e4f17")):
        shown = subprocess.run(["git", "show", f"{commit}:scripts/mos-boot/{name}"], cwd=REPO,
                               capture_output=True, check=False)
        if shown.returncode != 0:
            pytest.skip("git history not available")
        assert hashlib.sha256(shown.stdout).hexdigest() in LEGACY_HOOK_SHA256[name]


def test_a_checkout_whose_hook_script_still_exits_gives_a_blocked_plan_not_a_traceback():
    """Found on the real MOS VM, whose checkout predates the merge-safe hook scripts."""
    def old_checkout(rel):
        text = (Path(REPO) / rel).read_text()
        return text + "\nexit 0\n" if rel.endswith("post-start.sh") else text
    p = plan(mos_report(), mos_answers(), read=old_checkout)
    assert not p.applicable and p.steps == ()
    assert "cannot produce a valid plan" in p.blocked[0] and "must not exit" in p.blocked[0]
    assert "Update the checkout" in p.blocked[0]


def test_every_path_a_plan_makes_a_claim_about_was_actually_probed_by_discover():
    """Found on the real MOS VM: discover had not looked at default/casa-dashboard, yet the plan called it
    absent. A plan may only say a file is absent or unchanged if the scan checked that path."""
    live_report = discover(ubuntu(files=LIVE_FILES), repo_root="/home/pe/apps/pe")
    scenarios = [
        (systemd_report(), answers(tier="full", sudo_units=[{"unit": "plex.service"}])),
        (mos_report(), mos_answers()),
        (mos_report(files={"/mnt/data/pe/default/casa-dashboard": "old"}), mos_answers()),
        (live_report, answers(story="adopt", install_dir="/home/pe/apps/pe", telegram=None, llm=None, operator=None)),
    ]
    checked = 0
    for report, scenario_answers in scenarios:
        p = plan(report, scenario_answers)
        assert p.applicable
        probed = set(report["existing_pe"]["present"])
        for step in p.steps:
            if step.kind in ("file.write", "service.install", "sudoers.install"):
                if step.params.get("expect_absent") or step.params.get("expected_sha256"):
                    assert step.target in probed, step.target
                    checked += 1
    assert checked >= 12


def test_an_unprobed_path_can_only_be_kept_never_claimed_absent_or_replaced():
    report = mos_report()
    report["existing_pe"]["present"].pop("/etc/init.d/casa-dashboard")           # as if the scan never looked
    p = plan(report, mos_answers())
    unit = next(s for s in by_kind(p, "service.install") if s.params["name"] == "casa-dashboard")
    assert unit.params["if_exists"] == "keep" and not unit.params.get("expect_absent")
    assert any("was not checked by the scan" in w for w in p.warnings)


def test_discover_probes_the_default_files_the_mos_plan_writes():
    present = mos_report()["existing_pe"]["present"]
    for name in ("casa-planetexpress", "casa-dashboard"):
        assert f"/mnt/data/pe/default/{name}" in present and f"/etc/default/{name}" in present


def test_a_default_file_that_exists_on_mos_is_bound_to_its_hash_not_called_absent():
    report = mos_report(files={"/mnt/data/pe/default/casa-dashboard": "hand written\n"})
    p = plan(report, mos_answers())
    step = next(s for s in p.steps if s.target == "/mnt/data/pe/default/casa-dashboard")
    assert step.params["if_exists"] == "replace" and step.params["expected_sha256"] and not step.params.get("expect_absent")
