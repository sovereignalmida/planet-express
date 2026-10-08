"""The setup core's discover step, against a fake host so no real machine is needed."""
import json
import subprocess
import sys
from pathlib import Path

from planet_express.setup.discover import discover
from planet_express.setup.env import RunResult

OS_UBUNTU = 'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n'
OS_MOS = 'PRETTY_NAME="Devuan GNU/Linux 6 (excalibur)"\nID=devuan\nVERSION_ID="6"\n'
MOUNTS_UBUNTU = "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /mnt/media ext4 rw 0 0\n"
MOUNTS_MOS = "rootfs / rootfs rw 0 0\n/dev/vdb1 /mnt/data ext4 rw 0 0\n/dev/sda /boot vfat rw 0 0\n"
DOCKER_UP = {("docker", "--version"): (0, "Docker version 29.7.2, build abc\n"),
             ("docker", "info", "--format", "{{.DockerRootDir}}"): (0, "/var/lib/docker\n"),
             ("docker", "ps", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'): (0, "web-web-1\tweb\nloose\t\n")}


class FakeEnv:
    def __init__(self, files=None, dirs=(), runs=None, euid=0, free=None, machine="x86_64"):
        self.files = dict(files or {})
        self.dirs = set(dirs)
        self.runs = dict(runs or {})
        self._euid = euid
        self.free = free or {}
        self._machine = machine

    def read(self, path):
        return self.files.get(path)

    def exists(self, path):
        return path in self.files or path in self.dirs

    def is_dir(self, path):
        return path in self.dirs

    def glob(self, pattern):
        import fnmatch
        pool = sorted(set(self.files) | self.dirs)
        return [p for p in pool if fnmatch.fnmatchcase(p, pattern)]

    def run(self, argv, timeout=10):
        rc, out = self.runs.get(tuple(argv), (127, ""))
        return RunResult(rc, out)

    def euid(self):
        return self._euid

    def hostname(self):
        return "testhost"

    def machine(self):
        return self._machine

    def free_gib(self, path):
        return self.free.get(path)


def ubuntu(**kw):
    runs = {**DOCKER_UP, ("docker", "compose", "version"): (0, "Docker Compose version v2\n"), **kw.pop("runs", {})}
    files = {"/etc/os-release": OS_UBUNTU, "/proc/mounts": MOUNTS_UBUNTU, **kw.pop("files", {})}
    return FakeEnv(files=files, dirs={"/run/systemd/system", *kw.pop("dirs", ())}, runs=runs, **kw)


def mos(**kw):
    runs = {**DOCKER_UP, ("docker-compose", "version"): (0, "Docker Compose version v5.4.0\n"), **kw.pop("runs", {})}
    files = {"/etc/os-release": OS_MOS, "/proc/mounts": MOUNTS_MOS,
             "/usr/local/bin/mos-start": "", "/etc/init.d/start-mos": "", **kw.pop("files", {})}
    return FakeEnv(files=files, dirs=set(kw.pop("dirs", ())), runs=runs, free=kw.pop("free", {"/mnt/data": 36.8}), **kw)


def checks(report):
    return {c["id"]: c for c in report["checks"]}


def test_systemd_host_reports_plugin_compose_and_a_persistent_root():
    report = discover(ubuntu(), repo_root="/opt/pe")
    assert report["host"]["init_system"] == "systemd" and report["host"]["os"].startswith("Ubuntu")
    assert report["docker"] == {"installed": True, "version": "29.7.2", "daemon_running": True,
                                "compose_flavour": "plugin", "root_dir": "/var/lib/docker"}
    assert report["storage"]["persistent"] and report["storage"]["candidate_install_paths"] == ["/opt/pe"]
    assert report["storage"]["pools"] == []           # a pool is a MOS concept
    assert [m["mount"] for m in report["storage"]["mounts"]] == ["/mnt/media"]
    assert report["summary"] == {"ok": 8, "warn": 0, "blocked": 0, "can_continue": True}


def test_mos_host_installs_on_a_pool_because_root_is_ram():
    report = discover(mos(), repo_root="/mnt/data/pe/planet-express")
    assert report["host"]["init_system"] == "mos"
    assert report["docker"]["compose_flavour"] == "standalone"
    assert report["storage"]["root_fstype"] == "rootfs" and not report["storage"]["persistent"]
    assert report["storage"]["candidate_install_paths"] == ["/mnt/data/pe"]
    assert [p["mount"] for p in report["storage"]["pools"]] == ["/mnt/data"]
    assert checks(report)["storage"]["status"] == "ok"


def test_mos_without_a_pool_is_blocked_and_says_how_to_fix_it():
    report = discover(mos(files={"/proc/mounts": "rootfs / rootfs rw 0 0\n"}))
    storage = checks(report)["storage"]
    assert storage["status"] == "blocked" and "Pools" in storage["fix"]
    assert report["summary"]["can_continue"] is False


def test_a_pool_too_small_to_hold_the_install_does_not_qualify():
    report = discover(mos(free={"/mnt/data": 0.5}))
    assert report["storage"]["candidate_install_paths"] == []
    assert checks(report)["storage"]["status"] == "blocked"


def test_unknown_init_system_is_blocked():
    env = FakeEnv(files={"/etc/os-release": OS_UBUNTU, "/proc/mounts": MOUNTS_UBUNTU})
    report = discover(env)
    assert report["host"]["init_system"] == "unknown" and checks(report)["init_system"]["status"] == "blocked"


def test_no_docker_is_blocked_and_planet_express_guides_rather_than_installs():
    report = discover(FakeEnv(files={"/etc/os-release": OS_UBUNTU, "/proc/mounts": MOUNTS_UBUNTU},
                              dirs={"/run/systemd/system"}))
    docker = checks(report)["docker"]
    assert docker["status"] == "blocked" and "does not install Docker" in docker["fix"]
    assert "compose" not in checks(report)         # no point judging compose without docker
    assert report["containers_running"] == []


def test_docker_installed_but_daemon_down_is_blocked():
    env = ubuntu(runs={("docker", "info", "--format", "{{.DockerRootDir}}"): (1, "")})
    report = discover(env)
    assert report["docker"]["installed"] and not report["docker"]["daemon_running"]
    assert checks(report)["docker"]["status"] == "blocked"
    assert report["containers_running"] == []


def test_missing_compose_is_blocked():
    env = ubuntu(runs={("docker", "compose", "version"): (1, "")})
    assert checks(discover(env))["compose"]["status"] == "blocked"


def test_non_root_without_sudo_is_blocked_but_passwordless_sudo_is_only_a_note():
    assert checks(discover(ubuntu(euid=1000)))["privileges"]["status"] == "blocked"
    sudo_ok = ubuntu(euid=1000, runs={("sudo", "-n", "true"): (0, "")})
    assert checks(discover(sudo_ok))["privileges"]["status"] == "warn"


def test_stacks_are_found_under_the_usual_roots_and_ingress_is_flagged():
    env = ubuntu(files={f"/home/me/stacks/{n}/docker-compose.yml": "" for n in ("media", "network", "ai")},
                 dirs={"/home/me/stacks", "/opt/stacks"})
    report = discover(env)
    assert report["stacks_roots"][0] == {"path": "/home/me/stacks", "count": 3}
    by_name = {s["name"]: s for s in report["stacks"]}
    assert set(by_name) == {"media", "network", "ai"} and by_name["network"]["ingress_suggested"]
    assert not by_name["media"]["ingress_suggested"]
    assert [c["name"] for c in report["containers_running"]] == ["web-web-1", "loose"]
    assert report["containers_running"][1]["project"] is None


def test_an_explicit_stacks_root_replaces_the_search():
    env = ubuntu(files={"/srv/x/a/docker-compose.yml": "", "/home/me/stacks/b/docker-compose.yml": ""},
                 dirs={"/srv/x", "/home/me/stacks"})
    assert [r["path"] for r in discover(env, stacks_root="/srv/x")["stacks_roots"]] == ["/srv/x"]


def test_listening_ports_come_from_proc_net_tcp():
    # st 0A is LISTEN; the port is the hex after the colon (0x0050 = 80, 0x20E4 = 8420).
    tcp = ("  sl  local_address rem_address   st\n"
           "   0: 0100007F:0050 00000000:0000 0A 0\n"       # 80, listening
           "   1: 0100007F:01BB 0100007F:9999 01 0\n")      # 443, but only an established connection
    tcp6 = "  sl\n   0: 00000000000000000000000000000000:20E4 00000000000000000000000000000000:0000 0A 0\n"
    report = discover(ubuntu(files={"/proc/net/tcp": tcp, "/proc/net/tcp6": tcp6}))
    assert report["ports_in_use"] == {"80": True, "443": False, "8420": True, "8421": False}


def test_the_dashboard_port_being_busy_is_a_note_unless_pe_is_already_installed():
    tcp = "  sl\n   0: 00000000:20E4 00000000:0000 0A 0\n"
    busy = discover(ubuntu(files={"/proc/net/tcp": tcp}))
    assert checks(busy)["dashboard_port"]["status"] == "warn" and checks(busy)["dashboard_port"]["overridable"]
    installed = discover(ubuntu(files={"/proc/net/tcp": tcp, "/etc/planetexpress.env": ""}))
    assert checks(installed)["dashboard_port"]["status"] == "ok"


def test_existing_install_is_located_from_the_unit_file_not_a_default_path():
    unit = ("[Service]\nWorkingDirectory=/home/me/apps/pe\n"
            "Environment=CASA_CONFIG=/home/me/apps/pe/config.yaml\n")
    env = ubuntu(files={"/etc/systemd/system/casa-planetexpress.service": unit,
                        "/home/me/apps/pe/config.yaml": ""})
    existing = discover(env)["existing_pe"]
    assert existing["installed"] and existing["systemd_unit"] and existing["config"]
    assert existing["install_dir"] == "/home/me/apps/pe"
    assert existing["config_path"] == "/home/me/apps/pe/config.yaml"
    assert checks(discover(env))["existing_pe"]["status"] == "warn"


def test_existing_install_on_mos_is_located_from_etc_default():
    env = mos(files={"/etc/init.d/casa-planetexpress": "",
                     "/etc/default/casa-planetexpress": "DAEMON_DIR=/mnt/data/pe/planet-express\n"
                                                        "CASA_CONFIG=/mnt/data/pe/config.yaml\n"})
    existing = discover(env)["existing_pe"]
    assert existing["install_dir"] == "/mnt/data/pe/planet-express" and existing["init_script"]


def test_docker_bridges_outside_rfc1918_are_not_a_public_exposure():
    """Real case: Docker made a 172.32.x.x bridge, which `ipaddress` reads as globally routable."""
    ip = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
          "2: eth0    inet 192.168.1.94/24 brd 192.168.1.255 scope global eth0\n"
          "9: br-ab12cd@if3    inet 172.32.98.1/24 brd 172.32.98.255 scope global br-ab12cd\n"
          "10: docker0    inet 172.17.0.1/16 scope global docker0\n")
    report = discover(ubuntu(runs={("ip", "-o", "addr", "show"): (0, ip)}))
    assert report["network"] == {"lan_addresses": ["192.168.1.94"], "public_addresses": []}
    assert "exposure" not in checks(report)


def test_a_real_public_address_on_a_real_interface_raises_the_exposure_warning():
    ip = ("2: eth0    inet 192.168.1.94/24 scope global eth0\n"
          "3: ppp0    inet 8.8.4.4/32 scope global ppp0\n")
    report = discover(ubuntu(runs={("ip", "-o", "addr", "show"): (0, ip)}))
    exposure = checks(report)["exposure"]
    assert exposure["status"] == "warn" and "8.8.4.4" in exposure["detail"] and exposure["overridable"]


def test_mount_points_with_escaped_spaces_are_unescaped():
    mounts = "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /mnt/my\\040disk ext4 rw 0 0\n"
    assert discover(ubuntu(files={"/proc/mounts": mounts}))["storage"]["mounts"][0]["mount"] == "/mnt/my disk"


def test_the_report_is_plain_json():
    assert json.loads(json.dumps(discover(mos())))["schema"] == 1


def test_setup_never_imports_config_because_it_runs_before_config_exists():
    code = ("import sys; import planet_express.setup.discover, planet_express.setup.env; "
            "print('config' in sys.modules)")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "False"
