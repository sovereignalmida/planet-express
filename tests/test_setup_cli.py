"""The apply command line: mostly about what it refuses."""
import json
import os

import pytest

from planet_express.setup import __main__ as cli
from planet_express.setup.plan import plan
from planet_express.setup.answers import SetupAnswers
from test_setup_discover import ubuntu
from planet_express.setup.discover import discover

ANSWERS = {"run_as": "pe", "install_dir": "/opt/pe", "stacks_root": "/home/pe/stacks"}


@pytest.fixture
def report():
    return discover(ubuntu(), repo_root="/opt/pe")


@pytest.fixture
def wired(monkeypatch, report, tmp_path):
    monkeypatch.setattr(cli, "discover", lambda **kw: report)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(ANSWERS))
    path.chmod(0o600)
    plan_id = plan(report, SetupAnswers.model_validate(ANSWERS), repo_root=os.getcwd()).to_public()["plan_id"]
    return str(path), plan_id


def test_apply_needs_root_unless_it_is_a_dry_run(wired, capsys):
    answers, plan_id = wired
    if os.geteuid() == 0:
        pytest.skip("this test is about the non-root refusal")
    assert cli.main(["apply", "--answers", answers, "--plan-id", plan_id]) == 2
    assert "has to run as root" in capsys.readouterr().err


def test_a_plan_id_that_is_not_what_the_host_would_run_now_is_refused(wired, capsys):
    answers, _ = wired
    assert cli.main(["apply", "--answers", answers, "--plan-id", "0" * 16, "--dry-run"]) == 2
    assert "not 0000000000000000" in capsys.readouterr().err


def test_an_answers_file_with_secrets_that_others_can_read_is_refused(tmp_path, monkeypatch, report, capsys):
    monkeypatch.setattr(cli, "discover", lambda **kw: report)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({**ANSWERS, "telegram": {"token": "123456:abc", "chat_id": "1"}}))
    path.chmod(0o644)
    assert cli.main(["apply", "--answers", str(path), "--plan-id", "x", "--dry-run"]) == 2
    assert "chmod 600" in capsys.readouterr().err


def test_dry_run_prints_what_each_check_says_and_changes_nothing(wired, capsys, tmp_path):
    answers, plan_id = wired
    journal = tmp_path / "journal"
    code = cli.main(["apply", "--answers", answers, "--plan-id", plan_id, "--dry-run", "--journal-dir", str(journal)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "dry_run" and not journal.exists()
    kinds = {c["kind"]: c["check"] for c in out["checks"]}
    assert kinds["python.env"] == "not implemented"      # this build cannot apply every kind yet


def test_the_journal_lives_on_the_pool_on_mos_and_in_var_lib_elsewhere():
    assert cli.default_journal_root({"host": {"init_system": "mos"}}, "/mnt/data/pe/planet-express") == "/mnt/data/pe/setup"
    assert cli.default_journal_root({"host": {"init_system": "systemd"}}, "/opt/pe") == "/var/lib/planetexpress-setup"
