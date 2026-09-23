"""
Regression: casa_zoidberg compared `docker compose images -q` (bare hex image ID) with
`docker image inspect --format {{.Id}}` ("sha256:<hex>"), so the IDs never matched and
every service looked updated. The canary pass recreated all of them, and crash-looping
services raised false "rollback also failed" alerts. Found by the Planet Express test
homelab (Docker Compose 2.40.3, Engine 29.1.3).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_zoidberg as zoidberg

HEX = "65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
OTHER_HEX = "9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"
STACK = Path("/home/casaroot/stacks/healthy")


@pytest.mark.parametrize("raw", [HEX, f"sha256:{HEX}", f"  sha256:{HEX}\n"])
def test_normalize_image_id_strips_prefix_and_whitespace(raw):
    assert zoidberg._normalize_image_id(raw) == HEX


def _fake_docker(running_id: str, local_id: str, calls: list):
    """subprocess.run stand-in for the canary path up to the change decision."""

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "images -q" in joined:
            out = running_id
        elif "config --images" in joined:
            out = "nginx:1.27-alpine"
        elif " pull " in f" {joined} ":
            out = ""
        elif "image inspect" in joined:
            out = local_id
        else:
            raise AssertionError(f"unexpected command: {cmd!r}")
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    return fake_run


def test_same_image_with_and_without_sha256_prefix_is_no_change(monkeypatch):
    """The dry-run report, which still reads the ids itself."""
    calls = []
    monkeypatch.setattr("subprocess.run", _fake_docker(HEX, f"sha256:{HEX}", calls))

    result = zoidberg.canary_update_service(STACK, "web", tg=None, dry_run=True)

    assert result["status"] == "no_change"
    assert not any("up" in c and "-d" in c for c in calls), "an unchanged service must not be recreated"


def test_the_typed_canary_step_normalizes_both_sides_too(tmp_path):
    """The same regression where a real pass now lives: `update.canary` on the engine compares
    `compose images -q` (bare hex) with `image inspect` (`sha256:<hex>`), so it must normalise both
    or every service looks updated again (slice 5b-3)."""
    from planet_express.execution import engine, runbook as runbooks
    from tests.canary_fakes import CanarySvc, canary_step, execution

    svc = CanarySvc(tmp_path, running=HEX, pulled=HEX)
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    result = engine.RunbookEngine(svc).run(ex, plan, origin="zoidberg")

    step = svc._store.list_steps(ex)[0]
    assert result.status == "passed" and step["effect"] == "not_applied"
    assert not any("up -d" in " ".join(argv) for argv in svc.argv)
    assert step["output"]["old_image_id"] == step["output"]["new_image_id"] == HEX


def test_a_different_image_is_still_seen_as_an_update(tmp_path):
    from planet_express.execution import engine, runbook as runbooks
    from tests.canary_fakes import CanarySvc, canary_step, execution

    svc = CanarySvc(tmp_path, running=HEX, pulled=OTHER_HEX)
    ex = execution(svc)
    plan = runbooks.Runbook.model_validate(
        {"title": "t", "steps": [canary_step(svc)], "artifacts": {}})
    assert engine.RunbookEngine(svc).run(ex, plan, origin="zoidberg").status == "passed"
    step = svc._store.list_steps(ex)[0]
    assert step["effect"] == "applied"
    assert step["output"] == {"image_reference": "nginx:1.27-alpine",
                              "old_image_id": HEX, "new_image_id": OTHER_HEX}
