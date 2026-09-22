"""Proposal-time target bindings for runbook steps (slice 5b, design §4.2).

A binding records *which* thing a step was approved against — the compose file (by path and content
hash), the Compose project and service, and for container steps the container's name **and id** —
so the engine can refuse a step whose target drifted between approval and execution (compose file
edited, container recreated, stack set changed). Bindings are part of the hashed runbook document.

Every Docker call here goes through `bender.run_argv` directly, never through a caller's injected
runner: binding is bookkeeping, not an action, and must not appear in an action's argv trail.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path

import casa_bender as bender
from planet_express.execution import actions


class BindingError(actions.TargetError):
    """The target cannot be bound (file unreadable, container vanished, services unreadable)."""


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BindingError(f"cannot read {path}: {exc.strerror or exc}") from None


class Binder:
    def __init__(self, run_argv: Callable | None = None):
        self._run_argv = run_argv or bender.run_argv

    def _checked(self, argv: list[str], timeout: float, what: str) -> str:
        if timeout <= 0:
            raise actions.TargetTimeout("host slow, retry")
        rc, out, _err = self._run_argv(argv, timeout=timeout)
        if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
            raise actions.TargetTimeout("host slow, retry")
        if rc != 0:
            raise BindingError(f"could not read {what}")
        return out

    def container_id(self, container: str, *, timeout: float) -> str:
        out = self._checked(
            ["docker", "inspect", "--format", "{{.Id}}", container], timeout, f"the id of {container}",
        ).strip()
        if not out:
            raise BindingError(f"{container} has no id")
        return out

    def services(self, stack: str, *, timeout: float) -> list[str]:
        out = self._checked(
            ["docker", "compose", "-f", str(actions.compose_file(stack)), "config", "--services"],
            timeout, f"the services of {stack}",
        )
        services = sorted({line.strip() for line in out.splitlines() if line.strip()})
        if not services:
            raise BindingError(f"{stack} declares no services")
        return services

    def service(self, target: actions.Target, *, timeout: float) -> dict:
        compose = actions.compose_file(target.stack)
        return {
            "compose_path": str(compose),
            "compose_sha256": _file_sha256(compose),
            "project": target.stack,
            "service": target.service,
            "container": target.container,
            "container_id": self.container_id(target.container, timeout=timeout),
        }

    def stack(self, stack: str, *, timeout: float) -> dict:
        compose = actions.compose_file(stack)
        return {
            "compose_path": str(compose),
            "compose_sha256": _file_sha256(compose),
            "project": stack,
            "services": self.services(stack, timeout=timeout),
        }

    def stack_set(self, stacks: list[str], *, timeout: float) -> dict:
        return {"stacks": [{"stack": stack, **self.stack(stack, timeout=timeout)} for stack in stacks]}

    def stack_containers(self, stack: str, services: list[str], *, timeout: float) -> list[dict]:
        """`stack.up`'s outputs: exactly one container per approved service (design §4.2 staged
        bindings); a missing or duplicated service fails rather than guessing."""
        out = self._checked(
            ["docker", "compose", "-f", str(actions.compose_file(stack)), "ps", "-a",
             "--format", "{{.Service}}\t{{.Name}}\t{{.ID}}"],
            timeout, f"the containers of {stack}",
        )
        found: dict[str, list[tuple[str, str]]] = {}
        for line in out.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 3:
                found.setdefault(parts[0], []).append((parts[1], parts[2]))
        result = []
        for service in services:
            matches = found.get(service, [])
            if len(matches) != 1:
                raise BindingError(f"{stack}/{service} has {len(matches)} containers after up")
            name, _short_id = matches[0]
            result.append({
                "project": stack, "service": service, "container_name": name,
                "container_id": self.container_id(name, timeout=timeout),
            })
        return result
