"""A deterministic Binder for tests that fake the host (slice 5b-1, T39).

Binding reads the compose file and asks Docker for container ids; tests that fake target resolution
and the runner inject this instead, so bindings are stable and no host is touched. Tests that need
drift set `compose_sha` / `container_ids` / `stack_shas` between proposal and execution.
"""

from planet_express.execution import actions


class FakeBinder:
    def __init__(self):
        self.compose_sha = "a" * 64
        self.stack_shas: dict[str, str] = {}
        self.container_ids: dict[str, str] = {}
        self.services_by_stack: dict[str, list[str]] = {}

    def container_id(self, container, *, timeout):
        return self.container_ids.get(container, "0123456789ab")

    def _services(self, stack):
        return self.services_by_stack.get(stack, ["web"])

    def service(self, target, *, timeout):
        return {
            "compose_path": str(actions.compose_file(target.stack)),
            "compose_sha256": self.stack_shas.get(target.stack, self.compose_sha),
            "project": target.stack,
            "service": target.service,
            "container": target.container,
            "container_id": self.container_id(target.container, timeout=timeout),
        }

    def stack(self, stack, *, timeout):
        return {
            "compose_path": str(actions.compose_file(stack)),
            "compose_sha256": self.stack_shas.get(stack, self.compose_sha),
            "project": stack,
            "services": self._services(stack),
        }

    def stack_set(self, stacks, *, timeout):
        return {"stacks": [{"stack": stack, **self.stack(stack, timeout=timeout)} for stack in stacks]}

    def stack_containers(self, stack, services, *, timeout):
        return [
            {"project": stack, "service": service, "container_name": f"{stack}-{service}-1",
             "container_id": "0123456789ab"}
            for service in services
        ]
