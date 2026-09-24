"""planet_express/application/compose_plans.py (slice 5b-4): what `/install` and Amy's edits are
allowed to propose, checked on the parsed artifact rather than on the prose that produced it."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import config
from planet_express.application import compose_plans as cp
from planet_express.execution import compose_files as cf

LAN = "casalan.com"
CONTENT = """networks:
  casaproxy:
    external: true

services:
  app:
    image: nginx:1.27-alpine
    container_name: CASA_APP
    labels:
      - traefik.enable=true
      - traefik.http.routers.app-lan.rule=Host(`app.casalan.com`)
"""


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    root.mkdir()
    monkeypatch.setattr(config, "STACKS_ROOT", root)
    monkeypatch.setattr(config, "FORBIDDEN_STACKS", ["ai", "clawbot"])
    monkeypatch.setattr(config, "LAN_ONLY_DOMAIN", LAN)
    return root


def existing(root, stack="media", content=CONTENT):
    (root / stack).mkdir(exist_ok=True)
    path = cf.compose_path_for(root, stack)
    path.write_text(content)
    return path


def test_install_builds_one_approval_for_the_write_and_the_start(host):
    plan = cp.install_runbook("app", CONTENT, domain="app.casalan.com")
    assert [s.type for s in plan.steps] == ["compose.write", "stack.up"]
    write = plan.steps[0]
    assert write.params["expected_absent"] is True
    assert write.params["content_sha256"] == cf.sha256_text(CONTENT)
    assert plan.artifacts == {cf.sha256_text(CONTENT): CONTENT}
    # the stack step binds to what the artifact says, since the file does not exist yet
    assert plan.steps[1].binding["services"] == ["app"]
    assert plan.steps[1].binding["compose_sha256"] == cf.sha256_text(CONTENT)


def test_install_refuses_a_domain_outside_the_lan_only_convention(host):
    content = CONTENT.replace("app.casalan.com", "app.example.com")
    with pytest.raises(cp.ComposePlanRefused, match="LAN-only"):
        cp.install_runbook("app", content, domain="app.example.com")


def test_install_refuses_a_second_host_smuggled_into_the_rule(host):
    content = CONTENT.replace("Host(`app.casalan.com`)",
                              "Host(`app.casalan.com`, `app.example.com`)")
    with pytest.raises(cp.ComposePlanRefused, match="example.com"):
        cp.install_runbook("app", content, domain="app.casalan.com")


@pytest.mark.parametrize("stack", ["ai", "clawbot"])
def test_a_forbidden_stack_is_never_written(host, stack):
    with pytest.raises(cp.ComposePlanRefused, match="forbidden"):
        cp.install_runbook(stack, CONTENT, domain="app.casalan.com")


def test_install_refuses_a_stack_that_already_exists(host):
    existing(host, "app")
    with pytest.raises(cp.ComposePlanRefused, match="already exists"):
        cp.install_runbook("app", CONTENT, domain="app.casalan.com")


@pytest.mark.parametrize("name", ["Nope", "has space", "../escape", "-leading", "a" * 65])
def test_a_new_stack_name_outside_the_safe_set_is_refused(host, name):
    with pytest.raises(cp.ComposePlanRefused, match="valid new stack name"):
        cp.install_runbook(name, CONTENT, domain="app.casalan.com")


def test_a_symlinked_stack_directory_is_refused(host, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (host / "app").symlink_to(elsewhere)
    with pytest.raises(cp.ComposePlanRefused, match="symlink"):
        cp.install_runbook("app", CONTENT, domain="app.casalan.com")


@pytest.mark.parametrize("content", ["not: a: compose", "services: {}", "[]", "services:\n  '': {}"])
def test_content_that_is_not_a_compose_file_is_refused(host, content):
    with pytest.raises(cp.ComposePlanRefused):
        cp.install_runbook("app", content, domain="app.casalan.com")


# ── edits ───────────────────────────────────────────────────────────────────────
def test_an_edit_carries_the_current_file_as_its_expectation(host):
    existing(host)
    new = CONTENT.replace("nginx:1.27-alpine", "nginx:1.28-alpine")
    plan = cp.edit_runbook("media", new, current_content=CONTENT)
    assert [s.type for s in plan.steps] == ["compose.write", "stack.up"]
    assert plan.steps[0].params["expected_old_sha256"] == cf.sha256_text(CONTENT)
    assert plan.steps[0].params["content_sha256"] == cf.sha256_text(new)


def test_an_edit_of_a_stack_with_no_compose_file_is_refused(host):
    with pytest.raises(cp.ComposePlanRefused, match="no compose file"):
        cp.edit_runbook("media", CONTENT, current_content=CONTENT)


def test_an_edit_that_changes_nothing_is_refused(host):
    existing(host)
    with pytest.raises(cp.ComposePlanRefused, match="identical"):
        cp.edit_runbook("media", CONTENT, current_content=CONTENT)


def test_an_edit_may_not_name_a_service_the_file_does_not_declare(host):
    existing(host)
    new = CONTENT.replace("nginx:1.27-alpine", "nginx:1.28-alpine")
    with pytest.raises(cp.ComposePlanRefused, match="not a service"):
        cp.edit_runbook("media", new, current_content=CONTENT, expect=["ghost"])


def test_an_edit_may_keep_a_public_router_it_did_not_write(host):
    """The LAN-only rule is /install's (D35): an existing stack's own routers are not this
    installer's business, and refusing them would make ordinary edits impossible."""
    public = CONTENT.replace("app.casalan.com", "media.example.com")
    existing(host, content=public)
    new = public.replace("nginx:1.27-alpine", "nginx:1.28-alpine")
    plan = cp.edit_runbook("media", new, current_content=public)
    assert plan.steps[0].params["expected_old_sha256"] == cf.sha256_text(public)
