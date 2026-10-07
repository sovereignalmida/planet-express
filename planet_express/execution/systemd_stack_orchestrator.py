"""Systemd Stack Orchestrator - Docker Compose management via CLI.

Implements StackOrchestrationProvider for systemd systems (Ubuntu, etc).
Uses docker compose CLI for stack management.
"""

from dataclasses import dataclass
from typing import Protocol
import subprocess
import logging
from pathlib import Path
import tempfile
import yaml

log = logging.getLogger("planetexpress.systemd_orchestrator")


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


class SystemdStackOrchestrator:
    """Docker Compose management via CLI for systemd systems."""

    def __init__(self, stacks_root: Path = None):
        """Initialize Systemd orchestrator.

        Args:
            stacks_root: Root directory for docker-compose stacks (default: config.STACKS_ROOT)
        """
        if stacks_root is None:
            # Import here to avoid circular dependency
            import config
            stacks_root = config.STACKS_ROOT

        self.stacks_root = Path(stacks_root)
        if not self.stacks_root.exists():
            self.stacks_root.mkdir(parents=True, exist_ok=True)

    def _compose_file(self, name: str) -> Path:
        """Get compose file path for a stack."""
        return self.stacks_root / name / "docker-compose.yml"

    def _stack_dir(self, name: str) -> Path:
        """Get stack directory."""
        return self.stacks_root / name

    def _run_compose(self, name: str, *args, timeout: int = 180) -> tuple[bool, str]:
        """Run docker compose command.

        Args:
            name: Stack name
            args: Additional arguments for docker compose
            timeout: Command timeout in seconds

        Returns:
            (success: bool, output: str)
        """
        compose_file = self._compose_file(name)

        if not compose_file.exists():
            return False, f"Compose file not found: {compose_file}"

        import config

        cmd = [*config.compose_argv(), "-f", str(compose_file), *args]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

            output = (result.stdout + result.stderr).strip()

            if result.returncode == 0:
                return True, output
            else:
                log.error(f"docker compose failed: {output}")
                return False, output
        except subprocess.TimeoutExpired:
            return False, f"Command timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

    def list_stacks(self) -> list[str]:
        """List all available Docker Compose stacks.

        Returns:
            List of stack names (directories with docker-compose.yml)
        """
        try:
            stacks = []
            for path in sorted(self.stacks_root.glob("*/docker-compose.yml")):
                stacks.append(path.parent.name)
            return stacks
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
        compose_file = self._compose_file(name)

        if not compose_file.exists():
            return "not-found"

        try:
            success, output = self._run_compose(name, "ps", "--format=json")

            if not success:
                return "error"

            # If ps returns empty, stack is down
            if not output or output == "[]":
                return "stopped"

            # Check if any services are running
            import json
            try:
                services = json.loads(output)
                if services:
                    return "running"
            except json.JSONDecodeError:
                pass

            return "stopped"
        except Exception as e:
            log.error(f"Failed to get stack status: {e}")
            return "error"

    def create_stack(self, name: str, compose_yaml: str) -> StackActionResult:
        """Create and deploy a new stack.

        Args:
            name: Stack name
            compose_yaml: Docker Compose YAML content

        Returns:
            StackActionResult
        """
        before = "not-found"
        stack_dir = self._stack_dir(name)
        compose_file = self._compose_file(name)

        # Validate YAML
        try:
            yaml.safe_load(compose_yaml)
        except Exception as e:
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
            # Create stack directory and write compose file
            stack_dir.mkdir(parents=True, exist_ok=True)
            compose_file.write_text(compose_yaml)

            log.info(f"Created docker-compose.yml for stack {name}")

            # Start the stack
            return self.start_stack(name)
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
            success, output = self._run_compose(name, "up", "-d")

            if not success:
                return StackActionResult(
                    ok=False,
                    stack_name=name,
                    action="start",
                    before=before,
                    after="error",
                    effect="error",
                    error=output
                )

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
            success, output = self._run_compose(name, "down")

            if not success:
                return StackActionResult(
                    ok=False,
                    stack_name=name,
                    action="stop",
                    before=before,
                    after="error",
                    effect="error",
                    error=output
                )

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
            success, output = self._run_compose(name, "restart")

            if not success:
                return StackActionResult(
                    ok=False,
                    stack_name=name,
                    action="restart",
                    before=before,
                    after="error",
                    effect="error",
                    error=output
                )

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
        stack_dir = self._stack_dir(name)

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
            # Stop the stack first
            self.stop_stack(name)

            # Remove the stack directory
            import shutil
            if stack_dir.exists():
                shutil.rmtree(stack_dir)

            log.info(f"Deleted stack {name}")

            return StackActionResult(
                ok=True,
                stack_name=name,
                action="delete",
                before=before,
                after="not-found",
                effect="applied"
            )
        except Exception as e:
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
            # Pull latest images
            success, output = self._run_compose(name, "pull")

            if not success:
                return StackActionResult(
                    ok=False,
                    stack_name=name,
                    action="upgrade",
                    before=before,
                    after="error",
                    effect="error",
                    error=output
                )

            # Recreate services with new images
            success, output = self._run_compose(name, "up", "-d")

            if not success:
                return StackActionResult(
                    ok=False,
                    stack_name=name,
                    action="upgrade",
                    before=before,
                    after="error",
                    effect="error",
                    error=output
                )

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
            return StackActionResult(
                ok=False,
                stack_name=name,
                action="upgrade",
                before=before,
                after="error",
                effect="error",
                error=str(e)
            )
