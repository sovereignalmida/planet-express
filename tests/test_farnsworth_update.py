"""`/update <stack> <service>`, and which image references a canary may move (untagged means :latest)."""
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
import casa_zoidberg as zoidberg
from notifier import FakeNotifier
from planet_express.application.command_service import ProposeResult
from planet_express.execution import actions


class FakeTg:
    chat_id = "42"


class FakeCommands:
    def __init__(self, result=None):
        self.plans: list = []
        self.result = result or ProposeResult(True, "ab12cd34ef56", True, "awaiting approval")

    def propose_plan(self, runbook, *, requested_by=None, origin="planner", **_):
        self.plans.append((runbook, requested_by, origin))
        return self.result


class RecordingThread:
    started: ClassVar[list] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        RecordingThread.started.append((self.target, self.args))


def _send(text, commands, monkeypatch, sender=None):
    RecordingThread.started = []
    monkeypatch.setattr(fw.threading, "Thread", RecordingThread)
    n = FakeNotifier()
    msg = {"text": text, "chat": {"id": 42}, **({"from": sender} if sender else {})}
    fw.handle_message({"message": msg}, FakeTg(), n, fw.PipelineState(), commands)
    return n


# -- image references ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reference, eligible", [
    ("nginx:1.27-alpine", True), ("amir20/dozzle:latest", True), ("ghcr.io/linuxserver/qbittorrent:5", True),
    ("redis", True), ("bbilly1/tubearchivist", True), ("golift/unpackerr", True),            # no tag: :latest to Docker
    ("ghcr.io/linuxserver/qbittorrent", True), ("localhost:5000/app", True),                  # a registry port is not a tag
    ("localhost:5000/app:2", True),
    ("docker.io/redis:6.2-alpine@sha256:" + "a" * 64, False), ("redis@sha256:" + "a" * 64, False),   # a digest pin never moves
    ("", False), (None, False), ("a", False), ("bad..ref", False), ("-leading-dash", False), ("has space", False),
    ("x" * 600, False)])
def test_which_references_a_canary_may_move(reference, eligible):
    assert actions.is_canary_reference(reference) is eligible


def test_the_pre_check_reads_this_services_own_image_not_a_neighbours(monkeypatch):
    """`compose config --images svc` also prints the dependencies' images in no fixed order."""
    document = '{"services": {"tubearchivist": {"image": "bbilly1/tubearchivist"}, "redis": {"image": "redis"}}}'
    monkeypatch.setattr(zoidberg, "_run", lambda cmd, timeout=120: (0, document, ""))
    assert zoidberg.service_image_ref(Path("/stacks/media"), "tubearchivist") == "bbilly1/tubearchivist"
    assert zoidberg.service_image_ref(Path("/stacks/media"), "redis") == "redis"
    assert zoidberg.service_image_ref(Path("/stacks/media"), "missing") is None
    monkeypatch.setattr(zoidberg, "_run", lambda cmd, timeout=120: (1, "", "boom"))
    assert zoidberg.service_image_ref(Path("/stacks/media"), "x") is None


def test_the_words_of_the_message_are_validated_before_they_become_a_path_or_a_command(monkeypatch):
    ran = []
    monkeypatch.setattr(zoidberg, "_run", lambda cmd, timeout=120: ran.append(cmd) or (0, "", ""))
    commands, n = FakeCommands(), FakeNotifier()
    fw._run_update_request(commands, n, "x -f /etc/evil", "svc", "1001")
    fw._run_update_request(commands, n, "media", "svc; rm -rf /", "1001")
    fw._run_update_request(commands, n, "../etc", "svc", "1001")
    assert ran == [] and commands.plans == [] and len([m for m in n.notifications if "refused" in m]) == 3


# -- /update ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def known_stack(monkeypatch):
    """The stack and service exist and are unguarded; what each test varies is what comes after."""
    monkeypatch.setattr(fw.actions, "resolve_target", lambda stack, service, **k: object())
    monkeypatch.setattr(fw.actions, "compose_file", lambda stack: Path(f"/stacks/{stack}/docker-compose.yml"))


def test_update_usage_and_unavailable(monkeypatch):
    assert any("Usage: `/update <stack> <service>`" in m for m in _send("/update media", FakeCommands(), monkeypatch).notifications)
    assert any("unavailable" in m for m in _send("/update media redis", None, monkeypatch).notifications)
    assert RecordingThread.started == []


def test_update_runs_off_the_poll_thread_with_the_sender(monkeypatch):
    commands = FakeCommands()
    n = _send("/update media redis", commands, monkeypatch, sender={"id": 1001, "username": "chris"})
    assert RecordingThread.started == [(fw._run_update_request, (commands, n, "media", "redis", "@chris (1001)"))]
    assert commands.plans == []


def test_update_proposes_the_same_one_step_canary_the_weekly_pass_runs_and_asks_for_approval(monkeypatch, known_stack):
    runbook = object()
    monkeypatch.setattr(zoidberg, "build_canary_runbook", lambda stack_dir, service, commands: (runbook, None))
    commands, n = FakeCommands(), FakeNotifier()
    fw._run_update_request(commands, n, "media", "redis", "1001")
    assert commands.plans == [(runbook, "1001", "telegram")]          # a human origin: it needs approval like any other change
    assert any("Approval card sent" in m and "rolls back by itself" in m for m in n.notifications)


def test_update_reports_an_existing_proposal_and_a_policy_refusal(monkeypatch, known_stack):
    monkeypatch.setattr(zoidberg, "build_canary_runbook", lambda *a: (object(), None))
    n = FakeNotifier()
    fw._run_update_request(FakeCommands(ProposeResult(True, "abc", False, "already")), n, "media", "redis", "x")
    assert any("already awaiting approval" in m for m in n.notifications)
    n = FakeNotifier()
    fw._run_update_request(FakeCommands(ProposeResult(False, None, False, "R2: never allowed")), n, "media", "redis", "x")
    assert any("refused" in m and "never allowed" in m for m in n.notifications)


def test_a_service_that_cannot_be_canaried_is_refused_with_what_to_do(monkeypatch, known_stack):
    monkeypatch.setattr(zoidberg, "build_canary_runbook", lambda *a: (None, "x@sha256:abc is not a canary-eligible image reference"))
    commands, n = FakeCommands(), FakeNotifier()
    fw._run_update_request(commands, n, "media", "pinned", "x")
    assert commands.plans == [] and any("pinned by digest" in m for m in n.notifications)


def test_adguard_and_traefik_are_never_updated_from_a_one_line_command(monkeypatch, known_stack):
    monkeypatch.setattr(zoidberg, "build_canary_runbook",
                        lambda *a: (None, "network/adguard is network-guarded (Traefik/AdGuard); use a reviewed plan"))
    commands, n = FakeCommands(), FakeNotifier()
    fw._run_update_request(commands, n, "network", "adguard", "x")
    assert commands.plans == [] and any("carries your network" in m and "by hand" in m for m in n.notifications)
