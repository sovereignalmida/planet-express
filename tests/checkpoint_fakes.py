"""Fakes for the checkpoint tests: a scripted Docker CLI and a ready-made service over a tmp stacks tree."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

from missions_helpers import make_store, new_mission, repair_grant

from planet_express.missions.checkpoint import (
    CheckpointScope,
    CheckpointService,
    CheckpointSettings,
    ConfiguredBackupFreshness,
    SystemdCheckpointProvider,
)
from planet_express.missions.checkpoint.backup import BackupSource
from planet_express.missions.store import MissionStore

COMPOSE = "services:\n  web:\n    image: nginx:1.27\n    labels:\n      a: b\n"
SECRET = "DB_PASSWORD=hunter2-very-secret\nTOKEN=abcdef0123456789\n"


def inspect_doc(*, image="sha256:aaa", env=("A=1",), labels=None, cmd=("nginx",), networks=("media_default",),
                restart="unless-stopped", health=None):
    return {"Image": image, "Config": {"Env": list(env), "Labels": dict(labels or {"a": "b"}), "Cmd": list(cmd),
                                       "Entrypoint": None, "Healthcheck": health},
            "HostConfig": {"RestartPolicy": {"Name": restart, "MaximumRetryCount": 0}},
            "Mounts": [{"Type": "bind", "Source": "/data/web", "Destination": "/data", "RW": True}],
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


class FakeDocker:
    """Answers the argv the providers send. `containers` maps (stack, service) to an inspect document."""

    def __init__(self):
        self.containers = {("media", "web"): inspect_doc()}
        self.baseline = {k: json.loads(json.dumps(v)) for k, v in self.containers.items()}
        self.calls: list[list[str]] = []
        self.recreated: list[str] = []
        self.fix_on_recreate = True
        self.recreate_ok = True
        self.ports = ["tcp 0.0.0.0:80"]
        self.projects: dict[str, str] = {}        # stack dir -> the name `compose config` reports
        self.config_ok = True
        self.config_output = None          # replace the `compose config` document

    def bounded(self, argv, timeout, max_bytes):
        rc, out, err = self(argv, timeout)
        return rc, out[-max_bytes:], err[-max_bytes:], len(out) > max_bytes or len(err) > max_bytes

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "ps", "-aq"]:
            project = next(a.split("=", 2)[2] for a in argv if a.startswith("label=com.docker.compose.project="))
            service = next(a.split("=", 2)[2] for a in argv if a.startswith("label=com.docker.compose.service="))
            return (0, f"cid-{project}-{service}", "") if (project, service) in self.containers else (0, "", "")
        if argv[:2] == ["docker", "inspect"]:
            _, project, service = argv[2].split("-", 2)
            return 0, json.dumps([self.containers[(project, service)]]), ""
        if argv[-3:] == ["config", "--format", "json"]:
            stack = Path(argv[argv.index("-f") + 1]).parent.name
            if not self.config_ok:
                return 1, "", "compose config failed"
            if self.config_output is not None:
                return 0, self.config_output, ""
            return 0, json.dumps({"name": self.projects.get(stack, stack.lower()), "services": {}}), ""
        if "up" in argv and "--force-recreate" in argv:
            if not self.recreate_ok:
                return 1, "", "boom"
            stack = Path(argv[argv.index("-f") + 1]).parent.name
            for name in argv[argv.index("--no-deps") + 1:]:
                self.recreated.append(f"{stack}/{name}")
                if self.fix_on_recreate and (stack, name) in self.baseline:
                    self.containers[(stack, name)] = json.loads(json.dumps(self.baseline[(stack, name)]))
            return 0, "", ""
        if argv[0] == "ss":
            return 0, "\n".join(f"{p.split()[0]} LISTEN 0 128 {p.split()[1]} 0.0.0.0:*" for p in self.ports), ""
        return 127, "", "unexpected argv"


class Env:
    pass


def make_env(tmp_path: Path, *, enabled=True, provider_cls=SystemdCheckpointProvider, grant=None, sources=(),
             settings=None, trace=None, hooks=None, mtime=None) -> Env:
    env = Env()
    env.tmp = tmp_path
    env.store, env.clock = make_store(tmp_path)
    env.ms = MissionStore(env.store)
    env.stacks = tmp_path / "stacks"
    (env.stacks / "media").mkdir(parents=True)
    env.compose = env.stacks / "media" / "docker-compose.yml"
    env.compose.write_text(COMPOSE)
    env.compose.chmod(0o640)
    env.dotenv = env.stacks / "media" / ".env"
    env.dotenv.write_text(SECRET)
    env.dotenv.chmod(0o600)
    env.docker = FakeDocker()
    env.provider = provider_cls(SimpleNamespace(stacks_root=env.stacks), run=env.docker, run_bounded=env.docker.bounded,
                                read_text=lambda path: (_ for _ in ()).throw(OSError(path)))
    env.backup = ConfiguredBackupFreshness([BackupSource(p, m) for p, m in sources], mtime=mtime)
    env.root = tmp_path / "state" / "checkpoints"
    env.flag = {"on": enabled}
    env.svc = CheckpointService(mission_store=env.ms, root=env.root, stacks_root=env.stacks, provider=env.provider,
                                backup=env.backup, settings=settings or CheckpointSettings(),
                                enabled=lambda: env.flag["on"], trace=trace, hooks=hooks)
    env.mission = new_mission(env.ms, grant or repair_grant())
    env.scope = CheckpointScope(stacks=("media",))
    return env


def tree(stacks: Path) -> dict[str, tuple[bytes, int, int, int]]:
    out = {}
    for base, _dirs, names in os.walk(stacks):
        for name in names:
            p = Path(base) / name
            st = p.lstat()
            out[str(p)] = (p.read_bytes(), st.st_mode & 0o7777, st.st_uid, st.st_gid)
    return out
