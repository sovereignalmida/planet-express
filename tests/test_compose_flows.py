"""`/install` and Amy's edits as typed runbooks (slice 5b-4): one approval covering the write and
bringing the stack up, with the legacy diff path still available behind the switch."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
import config
from notifier import FakeNotifier
from planet_express.execution import compose_files as cf

CONTENT = """networks:
  casaproxy:
    external: true

services:
  app:
    image: nginx:1.27-alpine
    container_name: CASA_APP
    labels:
      - traefik.http.routers.app-lan.rule=Host(`app.casalan.com`)
"""

RESOLUTION = {
    "project_name": "app", "repo_url": "https://example.com/app", "image": "nginx:1.27-alpine",
    "ports": [{"container_port": 80, "purpose": "web"}], "primary_port": 80,
    "volumes": [], "required_env": [], "sufficient_context": True,
    "needs_docker_socket": False, "has_own_reverse_proxy": False,
    "requires_companion_services": False, "has_extra_directives": False,
    "named_volumes_need_special_config": False, "notes": "",
}


class Commands:
    def __init__(self, ok=True, reason="awaiting approval"):
        self.proposed = []
        self.events = []
        self.result = SimpleNamespace(ok=ok, reason=reason, approval_id="a" * 12, created=True)

    def propose_plan(self, runbook, *, requested_by=None, finding_ids=None, origin="planner",
                     preface=None):
        self.proposed.append((runbook, origin, requested_by, preface))
        return self.result

    def record_event(self, kind, **fields):
        self.events.append((kind, fields))


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    root.mkdir()
    monkeypatch.setattr(config, "STACKS_ROOT", root)
    monkeypatch.setattr(config, "LAN_ONLY_DOMAIN", "casalan.com")
    monkeypatch.setattr(config, "FORBIDDEN_STACKS", ["ai"])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    return root


def test_install_proposes_one_runbook_for_the_write_and_the_start(host):
    commands, notifier = Commands(), FakeNotifier()
    fw._process_fry_resolution(notifier, "app", "https://example.com/app", "app.casalan.com",
                               RESOLUTION, commands)
    assert len(commands.proposed) == 1
    runbook, origin, requested_by, preface = commands.proposed[0]
    assert [s.type for s in runbook.steps] == ["compose.write", "stack.up"]
    assert (origin, requested_by) == ("install", "Fry")
    assert runbook.steps[0].params["expected_absent"] is True
    assert "app.casalan.com" in next(iter(runbook.artifacts.values()))
    # the diff travels with the card, so it is handed to propose_plan rather than sent separately
    assert preface and "docker-compose.yml" in preface
    assert not any("docker-compose.yml" in message for message in notifier.notifications)


def test_a_refused_install_is_recorded_and_explained(host, monkeypatch):
    monkeypatch.setattr(config, "FORBIDDEN_STACKS", ["app"])
    commands, notifier = Commands(), FakeNotifier()
    fw._process_fry_resolution(notifier, "app", "https://example.com/app", "app.casalan.com",
                               RESOLUTION, commands)
    assert commands.proposed == []
    assert commands.events[0][0] == "compose.refused"
    assert any("forbidden" in message for message in notifier.notifications)


# ── Amy's edits ─────────────────────────────────────────────────────────────────
DIAGNOSIS = {
    "summary": "the port is wrong",
    "proposed_remediation": {
        "requires_compose_edit": True, "summary": "point it at 8080",
        "proposed_service_yaml": "  app:\n    image: nginx:1.28-alpine\n",
    },
}

EVENT = {"execution_id": "e1", "title": "Restart media/app", "failed_step": 1,
         "type": "service.restart", "reason": "healthcheck failing",
         "binding": {"project": "media", "service": "app", "container": "CASA_APP"}}


@pytest.fixture
def amy(monkeypatch, host):
    (host / "media").mkdir()
    cf.compose_path_for(host, "media").write_text(CONTENT.replace("  app:", "  app:", 1))
    monkeypatch.setattr(fw.bender, "read_service_block",
                        lambda stack, service: (cf.compose_path_for(host, "media").read_text(),
                                                "  app:\n    image: nginx:1.27-alpine\n"))
    monkeypatch.setattr(fw.bender, "run_argv", lambda argv, timeout=None: (0, "logs", ""))
    monkeypatch.setattr(fw.TelegramClient, "fmt_diagnosis", staticmethod(lambda *a: "diagnosis"))
    monkeypatch.setattr(fw.amy, "diagnose", lambda **kwargs: DIAGNOSIS)


def test_amys_compose_edit_becomes_its_own_runbook(host, amy):
    commands, notifier = Commands(), FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, EVENT)
    assert len(commands.proposed) == 1
    runbook, origin, requested_by, preface = commands.proposed[0]
    assert [s.type for s in runbook.steps] == ["compose.write", "stack.up"]
    assert (origin, requested_by) == ("amy", "Amy")
    assert preface and "docker-compose.yml" in preface
    assert runbook.steps[0].params["expected_old_sha256"] == cf.sha256_text(
        cf.compose_path_for(host, "media").read_text())
    assert "nginx:1.28-alpine" in next(iter(runbook.artifacts.values()))


def test_a_diagnosis_with_no_edit_proposes_nothing(host, amy, monkeypatch):
    monkeypatch.setattr(fw.amy, "diagnose",
                        lambda **kwargs: {"proposed_remediation": {"requires_compose_edit": False}})
    commands, notifier = Commands(), FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, EVENT)
    assert commands.proposed == []


def test_a_diagnosis_without_yaml_asks_for_a_human(host, amy, monkeypatch):
    monkeypatch.setattr(fw.amy, "diagnose", lambda **kwargs: {"proposed_remediation": {
        "requires_compose_edit": True, "compose_edit_description": "add a healthcheck"}})
    commands, notifier = Commands(), FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, EVENT)
    assert commands.proposed == []
    assert any("needs a human" in m or "needs a compose-file edit" in m
               for m in notifier.notifications)


def test_a_failure_with_no_container_never_calls_amy(host, monkeypatch):
    called = []
    monkeypatch.setattr(fw.amy, "diagnose", lambda **kwargs: called.append(1))
    commands, notifier = Commands(), FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, {"binding": {}, "reason": "x"})
    assert called == [] and commands.proposed == []


def test_amys_crash_never_escapes_the_hook(host, amy, monkeypatch):
    monkeypatch.setattr(fw.amy, "diagnose", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    commands, notifier = Commands(), FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, EVENT)
    assert any("crashed" in m for m in notifier.notifications)


def test_a_refused_proposal_never_shows_a_diff_for_a_change_on_no_offer(host, amy):
    """The diff is handed to `propose_plan`, which sends it only immediately before a card it is
    actually going to deliver — a refusal shows the reason and nothing else (Codex, T43)."""
    commands = Commands(ok=False, reason="cooling down: last attempt 2m ago")
    notifier = FakeNotifier()
    fw._investigate_typed_failure(notifier, commands, EVENT)
    text = "\n".join(notifier.notifications)
    assert "cooling down" in text
    assert "docker-compose.yml" not in text and "---" not in text


# The legacy diff-approval path behind `legacy_plans_enabled` is gone (slice 5b-5).
