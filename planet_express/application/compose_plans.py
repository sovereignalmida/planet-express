"""Turning a compose edit into a runbook (slice 5b-4, design §4.2 and §4.6).

`/install` and Amy's edits both end here: the content they synthesized becomes one approval that
covers the write **and** bringing the stack up, instead of a diff approval followed by an unrelated
second approval to start it. Everything this module refuses, it refuses on the **parsed artifact** —
what will actually be written — not on the prose that produced it.
"""

import logging
import re
from pathlib import Path

import yaml

import config
from planet_express.execution import actions, compose_files, runbook as runbooks

log = logging.getLogger("planetexpress.compose_plans")

# A new stack's name is a fresh identifier with no existing path to anchor it (the live host has
# stacks whose names predate this rule), so it is held to a safe character set up front.
NEW_STACK_NAME = r"[a-z0-9][a-z0-9-]{0,63}"


class ComposePlanRefused(Exception):
    """The edit cannot be proposed. The reason is shown to the operator and recorded."""


def parse_services(content: str) -> list[str]:
    """The service names the approved artifact declares, for steps that bind to a file that does
    not exist yet (design §4.2). Raises ComposePlanRefused for anything that is not a compose file
    with at least one service."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ComposePlanRefused(f"the content is not valid YAML: {exc}") from None
    if not isinstance(document, dict):
        raise ComposePlanRefused("the content is not a compose document")
    services = document.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposePlanRefused("the content declares no services")
    names = list(services)
    if not all(isinstance(name, str) and name for name in names):
        raise ComposePlanRefused("the content has a service with no name")
    return names


def check_lan_only(content: str, *, lan_only_domain: str) -> None:
    """D35: `/install` only ever writes LAN-only routers, so every Traefik host rule in the
    artifact must name this deployment's LAN-only domain. Checked on the parsed document, because
    that is what the router will actually read."""
    for rule in _router_rules(content):
        hosts = _hosts_in(rule)
        if not hosts:
            raise ComposePlanRefused(f"a Traefik rule names no host: {rule}")
        for host in hosts:
            lowered = host.lower()
            if lowered != lan_only_domain.lower() and not lowered.endswith("." + lan_only_domain.lower()):
                raise ComposePlanRefused(
                    f"{host} is outside this host's LAN-only domain ({lan_only_domain}); "
                    f"a public-facing router has to be set up by hand"
                )


def _router_rules(content: str) -> list[str]:
    document = yaml.safe_load(content)
    rules = []
    for service in (document.get("services") or {}).values():
        labels = (service or {}).get("labels") or []
        if isinstance(labels, dict):
            labels = [f"{key}={value}" for key, value in labels.items()]
        for label in labels:
            text = str(label)
            if ".rule=" in text and "traefik.http.routers." in text:
                rules.append(text.split(".rule=", 1)[1])
    return rules


def _hosts_in(rule: str) -> list[str]:
    hosts = []
    for part in rule.split("Host(")[1:]:
        inner = part.split(")", 1)[0]
        hosts += [piece.strip().strip("`'\" ") for piece in inner.split(",") if piece.strip()]
    return [host for host in hosts if host]


def _check_target(stack: str, *, new_stack: bool) -> Path:
    if stack in config.FORBIDDEN_STACKS:
        raise ComposePlanRefused(f"{stack} is a forbidden stack")
    if actions.is_ingress_stack(stack):
        raise ComposePlanRefused(f"{stack} is the ingress stack; it is edited by hand")
    path = compose_files.compose_path_for(config.STACKS_ROOT, stack)
    try:
        compose_files.check_path(config.STACKS_ROOT, path)
    except compose_files.ComposeWriteError as exc:
        raise ComposePlanRefused(str(exc)) from None
    if new_stack and path.exists():
        raise ComposePlanRefused(f"{stack} already exists; propose an edit against the current file")
    if not new_stack and not path.is_file():
        raise ComposePlanRefused(f"{stack} has no compose file to edit")
    return path


def _write_step(stack: str, path: Path, content: str, *, expected_old: str | None) -> dict:
    params = {"stack": stack, "content_sha256": compose_files.sha256_text(content)}
    if expected_old is None:
        params["expected_absent"] = True
    else:
        params["expected_old_sha256"] = expected_old
    return {"type": "compose.write", "params": params,
            "binding": {"compose_path": str(path), "project": stack,
                        "directory_existed": path.parent.exists()}}


def _staged_stack_binding(stack: str, path: Path, content: str, services: list[str]) -> dict:
    """A stack step for a file that does not exist yet: the fingerprint is the artifact's, and the
    services are the ones the artifact declares. At execution the engine re-reads the file and
    compares it against exactly this, so a write that landed something else fails the next step
    (design §4.2)."""
    return {"compose_path": str(path), "compose_sha256": compose_files.sha256_text(content),
            "project": stack, "services": services}


def install_runbook(stack: str, content: str, *, domain: str, lan_only_domain: str | None = None,
                    title: str | None = None) -> runbooks.Runbook:
    """`/install`: write a brand-new stack and bring it up, under one approval (D35)."""
    if not re.fullmatch(NEW_STACK_NAME, stack):
        raise ComposePlanRefused(f"{stack!r} is not a valid new stack name")
    path = _check_target(stack, new_stack=True)
    services = parse_services(content)
    check_lan_only(content, lan_only_domain=lan_only_domain or config.LAN_ONLY_DOMAIN)
    steps = [
        _write_step(stack, path, content, expected_old=None),
        {"type": "stack.up", "params": {"stack": stack},
         "binding": _staged_stack_binding(stack, path, content, services)},
    ]
    return runbooks.Runbook.model_validate({
        "title": title or f"Install {stack} ({domain})",
        "steps": steps,
        "artifacts": {compose_files.sha256_text(content): content},
    })


def edit_runbook(stack: str, content: str, *, current_content: str, expect: list[str] | None = None,
                 title: str | None = None) -> runbooks.Runbook:
    """Amy's compose edit: write the file, then `stack.up`, which recreates whatever the edit
    changed. `expect` names services the caller believes the edit is about; they are checked
    against the proposed file so an edit that silently drops or renames the service it was supposed
    to fix is refused here rather than discovered afterwards."""
    path = _check_target(stack, new_stack=False)
    if content == current_content:
        raise ComposePlanRefused("the proposed content is identical to the current file")
    services = parse_services(content)
    for name in expect or []:
        if name not in services:
            raise ComposePlanRefused(f"{name} is not a service in the proposed file")
    steps = [
        _write_step(stack, path, content,
                    expected_old=compose_files.sha256_text(current_content)),
        {"type": "stack.up", "params": {"stack": stack},
         "binding": _staged_stack_binding(stack, path, content, services)},
    ]
    return runbooks.Runbook.model_validate({
        "title": title or f"Edit {stack}/{compose_files.COMPOSE_FILENAME}",
        "steps": steps,
        "artifacts": {compose_files.sha256_text(content): content},
    })
