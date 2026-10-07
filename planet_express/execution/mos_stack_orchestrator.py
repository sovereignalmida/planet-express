"""MOS Stack Orchestrator - Docker Compose management via MOS REST API.

Implements StackOrchestrationProvider for MOS/sysvinit systems.
Provides feature parity with SystemdStackOrchestrator but uses MOS API instead of CLI.
"""

from dataclasses import dataclass
from typing import Protocol
import httpx
import logging
import json
from pathlib import Path

log = logging.getLogger("planetexpress.mos_orchestrator")


@dataclass
class StackActionResult:
    """Result of a stack operation."""
    ok: bool
    stack_name: str
    action: str  # "create", "start", "stop", "restart", "delete"
    before: str  # Stack state before action
    after: str  # Stack state after action
    effect: str  # "applied", "no-change", "error"
    error: str = ""  # Error message if ok=False

    def __str__(self) -> str:
        if self.ok:
            return f"{self.stack_name}: {self.action} {self.effect} ({self.before}→{self.after})"
        else:
            return f"{self.stack_name}: {self.action} failed - {self.error}"


class StackOrchestrationProvider(Protocol):
    """Protocol for stack orchestration (Docker Compose, etc)."""

    def list_stacks(self) -> list[str]:
        """List all available stacks."""
        ...

    def create_stack(self, name: str, compose_yaml: str) -> StackActionResult:
        """Create and deploy a new stack from docker-compose YAML."""
        ...

    def start_stack(self, name: str) -> StackActionResult:
        """Start an existing stack."""
        ...

    def stop_stack(self, name: str) -> StackActionResult:
        """Stop a running stack."""
        ...

    def restart_stack(self, name: str) -> StackActionResult:
        """Restart a stack."""
        ...

    def delete_stack(self, name: str) -> StackActionResult:
        """Delete a stack."""
        ...

    def get_stack_status(self, name: str) -> str:
        """Get stack status: 'running', 'stopped', 'not-found', 'error'."""
        ...

    def upgrade_stack(self, name: str) -> StackActionResult:
        """Upgrade all images in a stack."""
        ...


class MosStackOrchestrator:
    """Docker Compose management via MOS REST API."""

    def __init__(self, api_base_url: str = "http://localhost:998/api/v1", auth_token: str = None, stacks_root: Path = None):
        """Initialize MOS orchestrator.

        Args:
            api_base_url: MOS API base URL (default: local MOS)
            auth_token: MOS authentication token (optional, uses session auth if not provided)
            stacks_root: Root directory for stacks on MOS host (for local validation).
                        Defaults to config.STACKS_ROOT for consistency with PE.
        """
        self.api_base_url = api_base_url
        self.auth_token = auth_token

        if stacks_root is None:
            # Import here to avoid circular dependency
            import config
            stacks_root = config.STACKS_ROOT

        self.stacks_root = Path(stacks_root)
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)

    def _headers(self) -> dict:
        """Get request headers with authentication."""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _call_api(self, method: str, endpoint: str, json_data: dict = None) -> httpx.Response:
        """Call MOS API endpoint.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (e.g., "/docker/mos/compose/stacks")
            json_data: JSON request body

        Returns:
            HTTP response

        Raises:
            httpx.HTTPError: If API call fails
        """
        url = f"{self.api_base_url}{endpoint}"
        headers = self._headers()

        log.debug(f"MOS API: {method} {endpoint}")

        response = self.client.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
        )

        if response.status_code >= 400:
            log.error(f"MOS API error {response.status_code}: {response.text}")
            response.raise_for_status()

        return response

    def list_stacks(self) -> list[str]:
        """List all available Docker Compose stacks.

        Returns:
            List of stack names
        """
        try:
            response = self._call_api("GET", "/docker/mos/compose/stacks")
            stacks = response.json()

            # MOS API returns list of stack objects with "name" field
            if isinstance(stacks, list):
                return [s.get("name", s) if isinstance(s, dict) else s for s in stacks]
            return []
        except Exception as e:
            log.error(f"Failed to list stacks: {e}")
            return []

    def get_stack_status(self, name: str) -> str:
        """Get stack status.

        Args:
            name: Stack name

        Returns:
            Status: "running", "stopped", "not-found", "error"
        """
        try:
            response = self._call_api("GET", f"/docker/mos/compose/stacks/{name}")
            data = response.json()

            # MOS returns stack state; "up" = running
            return "running" if data.get("running") else "stopped"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return "not-found"
            log.error(f"Failed to get stack status: {e}")
            return "error"
        except Exception as e:
            log.error(f"Failed to get stack status: {e}")
            return "error"

    def create_stack(self, name: str, compose_yaml: str) -> StackActionResult:
        """Create and deploy a new stack.

        Args:
            name: Stack name
            compose_yaml: Docker Compose YAML content

        Returns:
            StackActionResult with before/after state
        """
        before = "not-found"
        try:
            before = self.get_stack_status(name)
        except Exception:
            pass

        try:
            # Parse YAML to validate it
            import yaml
            yaml.safe_load(compose_yaml)
        except Exception as e:
            log.error(f"Invalid docker-compose YAML: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="create",
                before=before,
                after="error",
                effect="error",
                error=f"Invalid YAML: {str(e)}"
            )

        try:
            # MOS API expects the YAML as a field in the request
            payload = {
                "name": name,
                "yaml": compose_yaml,
                "description": f"Stack created by Planet Express"
            }

            response = self._call_api("POST", "/docker/mos/compose/stacks", payload)

            # After creation, start the stack
            self.start_stack(name)

            after = self.get_stack_status(name)

            log.info(f"Created stack {name}: {before} → {after}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="create",
                before=before,
                after=after,
                effect="applied" if after == "running" else "created"
            )
        except Exception as e:
            log.error(f"Failed to create stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="create",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def start_stack(self, name: str) -> StackActionResult:
        """Start a stack.

        Args:
            name: Stack name

        Returns:
            StackActionResult
        """
        before = self.get_stack_status(name)

        try:
            self._call_api("POST", f"/docker/mos/compose/stacks/{name}/start")
            after = self.get_stack_status(name)

            effect = "applied" if after != before else "no-change"
            log.info(f"Started stack {name}: {before} → {after}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="start",
                before=before,
                after=after,
                effect=effect
            )
        except Exception as e:
            log.error(f"Failed to start stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="start",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def stop_stack(self, name: str) -> StackActionResult:
        """Stop a stack.

        Args:
            name: Stack name

        Returns:
            StackActionResult
        """
        before = self.get_stack_status(name)

        try:
            self._call_api("POST", f"/docker/mos/compose/stacks/{name}/stop")
            after = self.get_stack_status(name)

            effect = "applied" if after != before else "no-change"
            log.info(f"Stopped stack {name}: {before} → {after}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="stop",
                before=before,
                after=after,
                effect=effect
            )
        except Exception as e:
            log.error(f"Failed to stop stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="stop",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def restart_stack(self, name: str) -> StackActionResult:
        """Restart a stack.

        Args:
            name: Stack name

        Returns:
            StackActionResult
        """
        before = self.get_stack_status(name)

        try:
            self._call_api("POST", f"/docker/mos/compose/stacks/{name}/restart")
            after = self.get_stack_status(name)

            log.info(f"Restarted stack {name}: {before} → {after}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="restart",
                before=before,
                after=after,
                effect="applied"
            )
        except Exception as e:
            log.error(f"Failed to restart stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="restart",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def delete_stack(self, name: str) -> StackActionResult:
        """Delete a stack.

        Args:
            name: Stack name

        Returns:
            StackActionResult
        """
        before = self.get_stack_status(name)

        if before == "not-found":
            return StackActionResult(
                ok=True,
                stack_name=name,
                action="delete",
                before=before,
                after="not-found",
                effect="no-change"
            )

        try:
            self._call_api("DELETE", f"/docker/mos/compose/stacks/{name}")
            after = "not-found"

            log.info(f"Deleted stack {name}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="delete",
                before=before,
                after=after,
                effect="applied"
            )
        except Exception as e:
            log.error(f"Failed to delete stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="delete",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def upgrade_stack(self, name: str) -> StackActionResult:
        """Upgrade all images in a stack.

        Args:
            name: Stack name

        Returns:
            StackActionResult
        """
        before = self.get_stack_status(name)

        try:
            self._call_api("POST", f"/docker/mos/compose/stacks/{name}/upgrade")
            after = self.get_stack_status(name)

            log.info(f"Upgraded stack {name}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="upgrade",
                before=before,
                after=after,
                effect="applied"
            )
        except Exception as e:
            log.error(f"Failed to upgrade stack {name}: {e}")
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="upgrade",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )

    def __del__(self):
        """Cleanup HTTP client."""
        if hasattr(self, 'client'):
            self.client.close()
