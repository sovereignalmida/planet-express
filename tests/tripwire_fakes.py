"""A fake host and a ready-made executing mission for the tripwire tests. Files are real (under a tmp fs_root);
Docker, `ss`, the mount table and the firewall are scripted."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkpoint_fakes import make_env
from missions_helpers import DEFAULT_PLAN, advance_to, token, with_default_plan

from planet_express.missions.approvals import record_approval
from planet_express.missions.containment import FakeContainment
from planet_express.missions.grants import with_site_protected
from planet_express.missions.launcher.spec import LaunchSpec
from planet_express.missions.models import Grant
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.tripwire import ScanEnv, Scanner, TripwireMonitor
from planet_express.missions.tripwire.signals import volume_paths


def doc(name, *, project=None, service=None, image="sha256:aaa", labels=None, mounts=(), networks=("media_default",)):
    lab = dict(labels or {})
    if project:
        lab["com.docker.compose.project"] = project
        lab["com.docker.compose.service"] = service
    return {"Id": (name.encode().hex() * 4)[:64], "Name": "/" + name, "Image": image,
            "Config": {"Labels": lab},
            "Mounts": [{"Type": t, "Source": s, "Destination": d, "RW": True} for t, s, d in mounts],
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}


class Host:
    def __init__(self):
        self.containers = {
            "media-qbit-1": doc("media-qbit-1", project="media", service="qbit",
                                mounts=[("bind", "{stacks}/media/config", "/config")]),
            "media-traefik-1": doc("media-traefik-1", project="media", service="traefik",
                                   labels={"traefik.enable": "true"}),
            "other-db-1": doc("other-db-1", project="other", service="db", image="sha256:bbb",
                              networks=("other_default",)),
        }
        self.ports = ["tcp 0.0.0.0:443", "tcp 127.0.0.1:8080"]
        self.mounts = ["/dev/sda1 / ext4 rw 0 0", "/dev/sdb1 /mnt/data ext4 rw 0 0", "proc /proc proc rw 0 0"]
        self.firewall = "table inet filter { chain input { policy drop; } }\n"
        self.fail: dict[str, Exception] = {}
        self.ss_rc = 0
        self.mounts_error: OSError | None = None
        self.stacks = "/srv/stacks"

    def _docs(self):
        text = json.dumps(list(self.containers.values())).replace("{stacks}", self.stacks)
        return json.loads(text)

    def runner(self, argv, timeout):
        if argv[:3] == ["docker", "ps", "-aq"]:
            return 0, "\n".join(d["Id"] for d in self._docs()), ""
        if argv[0] == "ss":
            if "ports" in self.fail:
                raise self.fail["ports"]
            lines = [f"{p.split()[0]} LISTEN 0 128 {p.split()[1]} 0.0.0.0:*" for p in self.ports]
            return self.ss_rc, "\n".join(lines), ""
        if argv[:2] == ["nft", "list"]:
            return 0, self.firewall, ""
        if argv[0] == "iptables-save":
            return 127, "", ""
        return 127, "", "unexpected argv"

    def bounded(self, argv, timeout, max_bytes):
        if argv[:2] == ["docker", "inspect"]:
            wanted = set(argv[2:])
            out = json.dumps([d for d in self._docs() if d["Id"] in wanted])
            return 0, out, "", False
        return 127, "", "unexpected argv", False

    def read_mounts(self):
        if self.mounts_error:
            raise self.mounts_error
        return "\n".join(self.mounts)


class Rig:
    pass


def make_rig(tmp_path, *, grant_over=None, monitor_kw=None, scan_kw=None, with_tree=False, enabled=True):
    rig = Rig()
    rig.host = Host()
    fs = tmp_path / "root"
    rig.fs = fs
    (fs / "etc" / "traefik").mkdir(parents=True)
    (fs / "etc" / "traefik" / "dynamic.yml").write_text("http: {}\n")
    (fs / "etc" / "sudoers.d").mkdir()
    (fs / "etc" / "hosts").write_text("127.0.0.1 localhost\n")
    (fs / "etc" / "shadow").write_text("root:x:1\n")
    (fs / "etc" / "docker").mkdir()
    (fs / "etc" / "docker" / "daemon.json").write_text("{}\n")
    (fs / "etc" / "cron.d").mkdir()
    (fs / "etc" / "exports").write_text("/srv/nas *(rw)\n")
    (fs / "proc" / "sys" / "kernel" / "random").mkdir(parents=True)
    rig.boot_id = fs / "proc" / "sys" / "kernel" / "random" / "boot_id"
    rig.boot_id.write_text("boot-aaaa\n")
    rig.uptime = fs / "proc" / "uptime"
    rig.uptime.write_text("1000.55 2000.00\n")
    (fs / "var" / "lib" / "dpkg").mkdir(parents=True)
    rig.dpkg = fs / "var" / "lib" / "dpkg" / "status"
    rig.dpkg.write_text("Package: a\n")
    data = fs / "mnt" / "data"
    data.mkdir(parents=True)
    for i in range(40):
        (data / f"f{i}.bin").write_bytes(b"x" * 1000)
    rig.data = data
    rig.tree = fs / "home" / "repo"
    if with_tree:
        git = rig.tree / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "remotes" / "origin").mkdir(parents=True)
        (git / "logs" / "refs" / "remotes" / "origin").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
        (git / "logs").mkdir(exist_ok=True)
        (git / "logs" / "HEAD").write_text("0 a commit: init\n")
        (git / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@example.org:x.git\n')
        (rig.tree / "app.py").write_text("print(1)\n")

    env = make_env(tmp_path)
    rig.env = env
    rig.host.stacks = str(env.stacks)
    discovery = Discovery(stacks_root=str(env.stacks), storage_mounts=["/mnt/data"],
                          live_trees=["/home/repo"] if with_tree else [],
                          cross_host=[("nas", "/etc/exports"), ("router", None)])
    rig.protected = build_protected(site=["service:media/traefik"], discovery=discovery)
    body = {"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]},
            "verification": {"checks": ["http"]}, "backup": {"covered": True}}
    body.update(grant_over or {})
    grant = with_site_protected(with_default_plan(Grant.model_validate(body)), rig.protected)
    # replace the S5 draft mission by one carrying this grant
    rig.mission = env.ms.create_mission(title="t", origin="operator", grant=grant, goal="g", success_criteria="s",
                                        runtime="fake")
    rig.mission = env.ms.set_verification_plan(rig.mission.id, DEFAULT_PLAN)
    env.mission = rig.mission
    scan_env = ScanEnv(runner=rig.host.runner, bounded=rig.host.bounded, read_mounts=rig.host.read_mounts, protected=rig.protected,
                       fs_root=str(fs), volume_paths=volume_paths(rig.protected), clock=env.clock)
    rig.scan_env = scan_env
    rig.scanner = Scanner(scan_env, signal_timeout_s=(scan_kw or {}).get("signal_timeout_s", 3.0),
                          deadline_s=(scan_kw or {}).get("deadline_s", 10.0), clock=env.clock)
    rig.cont = FakeContainment()
    kw = dict(missions=env.ms, containment=rig.cont, checkpoint=env.svc, scanner=rig.scanner,
              state_dir=str(tmp_path / "tripwire"), enabled=lambda: enabled, stacks_root=str(env.stacks),
              interval_s=15, clock=env.clock)
    kw.update(monitor_kw or {})
    rig.monitor = TripwireMonitor(**kw)
    rig.revoked = []
    return rig


def go_executing(rig, *, checkpoint=True, baseline=True):
    env, m = rig.env, rig.mission
    advance_to(env.ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(env.store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    if checkpoint:
        env.svc.create(m.id, env.scope)
    env.ms.start_session(m.id, kind="local_cgroup", token_hash=token(), session_ttl_s=3600, lease_ttl_s=600,
                         cgroup_path=rig.cont.cgroup_path("S-1"), session_uid=60001, session_id="S-1", state="live")
    rig.cs = rig.cont.launch("S-1", LaunchSpec(uid=60001, cwd="/tmp", argv=("/bin/true",)))
    if baseline:
        rig.monitor.begin(m.id)
    return env.ms.get_mission(m.id)


def status(rig):
    return rig.env.ms.get_mission(rig.mission.id).status


def paused_event(rig):
    for e in reversed(rig.env.ms.events(rig.mission.id)):
        if e.kind == "tripwire" and json.loads(e.body_json).get("event") == "tripwire.paused":
            return json.loads(e.body_json)
    return None


def frozen(rig):
    return rig.cont._groups["S-1"].frozen


def touch(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, None)
