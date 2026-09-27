"""A fake host for `update.canary` tests: every docker/compose argv the step runs, answered from a
dict of image ids, with the calls recorded. No Docker, no LLM, real Store."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import Store
from planet_express.execution import actions
from tests.binding_fakes import FakeBinder

STACK, SERVICE = "media", "sonarr"
CONTAINER = f"{STACK}-{SERVICE}-1"
OLD = "1" * 64
NEW = "2" * 64
REFERENCE = "nginx:1.27-alpine"


class CanarySvc:
    """Stands in for CommandService: only what the engine's canary path touches."""

    def __init__(self, tmp_path, *, reference=REFERENCE, running=OLD, pulled=NEW):
        self._store = Store(tmp_path / "db.sqlite", clock=lambda: 1_000_000.0)
        self._store.init()
        self._binder = FakeBinder()
        self._clock = lambda: 1_000_000.0
        self.argv = []
        self.reference = reference
        self._container_reference = None
        self._compose_images = None
        self._compose_config = None
        self.running = running          # image the container runs
        self.reference_id = running     # image the reference points at
        self.pulled = pulled            # what a pull makes the reference point at
        self.fail = {}                  # argv marker -> exit code
        self.watch_result = (True, "healthy for 90s")          # the new image
        self.rollback_watch_result = (True, "healthy for 30s")  # the restored one
        self.watched = []
        self.deploy_breaks = False      # `up` succeeds but the container runs something else

    # What the CONTAINER was created from. The canary's reference comes from here, because a
    # container has exactly one image reference and it is its own. Follows `reference` unless a
    # test deliberately pulls them apart.
    @property
    def container_reference(self):
        return self.reference if self._container_reference is None else self._container_reference

    @container_reference.setter
    def container_reference(self, value):
        self._container_reference = value

    # What compose lists for this SERVICE -- more than one line on a real host, and in a
    # non-deterministic order. The network stack's gluetun lists gluetun's image and traefik's.
    @property
    def compose_images(self):
        return [self.reference] if self._compose_images is None else self._compose_images

    @compose_images.setter
    def compose_images(self, value):
        self._compose_images = value

    # What compose CONFIGURES, per service. This is what the canary resolves against; the
    # service under test is SERVICE, and anything else here is a related service sharing the
    # file. Follows `reference` unless a test pulls them apart.
    @property
    def compose_config(self):
        if self._compose_config is not None:
            return self._compose_config
        return {SERVICE: self.reference}

    @compose_config.setter
    def compose_config(self, value):
        self._compose_config = value

    # ── the engine's ports ──────────────────────────────────────────────────
    def _run_argv(self, argv, timeout=None):
        self.argv.append(argv)
        joined = " ".join(argv)
        for marker, code in self.fail.items():
            if marker in joined:
                return code, "", f"{marker} failed"
        if "config --format json" in joined:
            # Compose's resolved config: a MAP keyed by service, including the related ones.
            # The related set is modelled because flattening it into lines and taking the
            # first is what made a gluetun canary retag traefik on 2026-09-27.
            import json as _json
            services = {name: {"image": image} for name, image in self.compose_config.items()}
            return 0, _json.dumps({"services": services}), ""
        if "config --images" in joined:
            return 0, "\n".join(self.compose_images) + "\n", ""
        if "images -q" in joined:
            return 0, self.running + "\n", ""
        if argv[:3] == ["docker", "image", "inspect"]:
            return (0, f"sha256:{self.reference_id}\n", "") if self.reference_id else (1, "", "no such image")
        if argv[:3] == ["docker", "inspect", "--format"]:
            # Two different questions about a container: which reference it was created from,
            # and which image id it is actually running.
            if "{{.Config.Image}}" in joined:
                return 0, self.container_reference + "\n", ""
            return 0, f"sha256:{self.running}\n", ""
        if " pull " in f" {joined} ":
            self.reference_id = self.pulled
            return 0, "", ""
        if "docker tag" in joined or argv[:2] == ["docker", "tag"]:
            self.reference_id = argv[2]
            return 0, "", ""
        if "up -d" in joined:
            if self.deploy_breaks:
                self.deploy_breaks = False      # only the first deploy lands the wrong image
                self.running = "9" * 64
            else:
                self.running = self.reference_id
            return 0, "", ""
        raise AssertionError(f"unexpected argv: {argv!r}")

    def _resolve(self, stack, service, for_mutation=True, timeout=None):
        return actions.Target(stack, service, CONTAINER)

    def _watch(self, container, seconds, **kwargs):
        self.watched.append(seconds)
        return self.watch_result if len(self.watched) == 1 else self.rollback_watch_result

    def _verify(self, container, baseline):
        return (True, "healthy")

    def _read_health(self, container):
        raise AssertionError("the canary step does not read health directly")

    def _restart_count(self, container):
        return 0


def canary_step(svc=None):
    target = actions.Target(STACK, SERVICE, CONTAINER)
    return {"type": "update.canary", "params": {"stack": STACK, "service": SERVICE},
            "binding": (svc._binder if svc else FakeBinder()).service(target, timeout=4)}


def execution(svc, target_key=f"{STACK}/{SERVICE}"):
    approval, _ = svc._store.propose(
        action="docker.restart_service", target_key=target_key,
        target={"stack": STACK, "service": SERVICE, "container": CONTAINER},
        risk="R1", requested_via="telegram", requested_by="t")
    return svc._store.approve_and_create_execution(approval["id"], decided_by="t", arrived_at=1)["id"]
