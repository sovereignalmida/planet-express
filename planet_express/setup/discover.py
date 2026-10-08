"""discover: read-only facts about this host, plus the checks the setup wizard shows.

Nothing here changes the host. Every probe goes through `env`, so the whole report can be built
from a fake host in tests. The shape is the contract in docs/designs/installer-brief.md section 6:
`checks[]{id,label,status,detail,fix,overridable}` with status `ok`, `warn` or `blocked`.

Setup runs before any Planet Express config exists: this module must not import `config`.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from planet_express.setup.env import SystemEnv

SCHEMA = 1

# Mount types that survive a reboot. `rootfs`, `tmpfs` and `ramfs` do not: MOS keeps / in RAM.
VOLATILE_FSTYPES = {"rootfs", "tmpfs", "ramfs"}
PERSISTENT_FSTYPES = {"ext4", "ext3", "xfs", "btrfs", "zfs", "f2fs", "ntfs", "exfat",
                      "fuse.mergerfs", "nfs", "nfs4", "cifs"}
# A pool needs room for the checkout, the venv and a state directory.
MIN_INSTALL_GIB = 2.0
PORTS_OF_INTEREST = (80, 443, 8420, 8421)
DASHBOARD_PORT = 8420
STACK_ROOT_PATTERNS = ("/home/*/stacks", "/root/stacks", "/opt/stacks", "/srv/stacks",
                       "/data/stacks", "/mnt/*/stacks")
# The stack that carries ingress/DNS for the rest. Stack control tears it down last and never
# manages it automatically, so the wizard suggests watching it only.
INGRESS_STACK_NAMES = {"network"}
# Addresses on these interfaces are Docker/VM plumbing, not the host's reachable addresses. Docker
# will happily create a bridge outside the RFC 1918 ranges (172.32.x.x), which `is_global` reads as public.
VIRTUAL_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet")


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _mounts(env) -> list[tuple[str, str]]:
    """(mount point, fstype) from /proc/mounts. Later entries win, as on a real mount stack."""
    found: dict[str, str] = {}
    for line in (env.read("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            found[_unescape_mount(parts[1])] = parts[2]
    return list(found.items())


def _host(env) -> dict:
    release: dict[str, str] = {}
    for line in (env.read("/etc/os-release") or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            release[key] = value.strip().strip('"')
    if env.exists("/usr/local/bin/mos-start") and env.exists("/etc/init.d/start-mos"):
        init = "mos"
    elif env.is_dir("/run/systemd/system"):
        init = "systemd"
    else:
        init = "unknown"
    return {
        "os": release.get("PRETTY_NAME") or release.get("NAME") or "unknown",
        "os_id": release.get("ID", ""),
        "version": release.get("VERSION_ID", ""),
        "init_system": init,
        "arch": env.machine(),
        "hostname": env.hostname(),
    }


def _docker(env) -> dict:
    version = env.run(["docker", "--version"])
    installed = version.rc == 0
    match = re.search(r"Docker version ([\w.\-+]+)", version.out)
    plugin = installed and env.run(["docker", "compose", "version"]).rc == 0
    standalone = (not plugin) and env.run(["docker-compose", "version"]).rc == 0
    info = env.run(["docker", "info", "--format", "{{.DockerRootDir}}"]) if installed else None
    daemon = bool(info and info.rc == 0)
    return {
        "installed": installed,
        "version": match.group(1) if match else None,
        "daemon_running": daemon,
        "compose_flavour": "plugin" if plugin else "standalone" if standalone else None,
        "root_dir": info.out.strip() if daemon and info.out.strip() else None,
    }


def _storage(env, host: dict, repo_root: str) -> dict:
    mounts = _mounts(env)
    root_fstype = dict(mounts).get("/", "unknown")
    persistent_root = root_fstype not in VOLATILE_FSTYPES and root_fstype != "unknown"
    persistent_mounts = [
        {"name": Path(mount).name, "mount": mount, "fstype": fstype, "free_gib": env.free_gib(mount)}
        for mount, fstype in mounts
        if fstype in PERSISTENT_FSTYPES and mount.startswith("/mnt/")
    ]
    # A "pool" is a MOS concept: the only place anything survives a reboot. Elsewhere these are just mounts.
    pools = persistent_mounts if host["init_system"] == "mos" else []
    if host["init_system"] == "mos":
        # Nothing outside a pool survives a reboot, so the only places PE can live are on one.
        candidates = [f"{p['mount']}/pe" for p in pools
                      if p["free_gib"] is not None and p["free_gib"] >= MIN_INSTALL_GIB]
    else:
        candidates = [repo_root]
    return {"root_fstype": root_fstype, "persistent": persistent_root, "mounts": persistent_mounts,
            "pools": pools, "candidate_install_paths": candidates}


def _stack_roots(env, hint: str | None) -> list[dict]:
    if hint:
        patterns = [hint]
    else:
        patterns = list(STACK_ROOT_PATTERNS)
    roots = []
    seen = set()
    for pattern in patterns:
        for root in env.glob(pattern):
            if root in seen or not env.is_dir(root):
                continue
            seen.add(root)
            count = len(env.glob(f"{root}/*/docker-compose.yml"))
            roots.append({"path": root, "count": count})
    return sorted(roots, key=lambda r: (-r["count"], r["path"]))


def _stacks(env, roots: list[dict]) -> list[dict]:
    if not roots or roots[0]["count"] == 0:
        return []
    root = roots[0]["path"]
    out = []
    for compose in env.glob(f"{root}/*/docker-compose.yml"):
        path = str(Path(compose).parent)
        name = Path(path).name
        out.append({"name": name, "path": path, "root": root,
                    "ingress_suggested": name in INGRESS_STACK_NAMES})
    return out


def _containers(env, docker: dict) -> list[dict]:
    if not docker["daemon_running"]:
        return []
    ps = env.run(["docker", "ps", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'])
    if ps.rc != 0:
        return []
    out = []
    for line in ps.out.splitlines():
        name, _, project = line.partition("\t")
        if name:
            out.append({"name": name, "project": project or None})
    return out


def _listening_ports(env) -> dict[str, bool]:
    listening: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in (env.read(table) or "").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A":  # TCP_LISTEN
                try:
                    listening.add(int(fields[1].rpartition(":")[2], 16))
                except ValueError:
                    continue
    return {str(port): port in listening for port in PORTS_OF_INTEREST}


def _network(env) -> dict:
    """Addresses on this host. A public address is the signal the wizard's exposure banner needs:
    setup is meant for the LAN only, and a globally routable address means it could be reachable."""
    addresses = []
    for line in env.run(["ip", "-o", "addr", "show"]).out.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1].split("@")[0].startswith(VIRTUAL_IFACE_PREFIXES):
            continue
        match = re.search(r"\binet6?\s+([0-9a-fA-F:.]+)/\d+", line)
        if not match:
            continue
        try:
            addresses.append(ipaddress.ip_address(match.group(1)))
        except ValueError:
            continue
    usable = [a for a in addresses if not (a.is_loopback or a.is_link_local)]
    return {
        "lan_addresses": [str(a) for a in usable if a.is_private],
        "public_addresses": [str(a) for a in usable if a.is_global],
    }


def _privileges(env) -> dict:
    root = env.euid() == 0
    return {"root": root, "can_sudo": root or env.run(["sudo", "-n", "true"]).rc == 0}


def _assigned(text: str | None, key: str) -> str | None:
    """VALUE from a `KEY=VALUE` assignment, with an optional `Environment=` prefix and quotes."""
    match = re.search(rf"^\s*(?:Environment=)?\"?{key}=\"?([^\"\s]+)", text or "", re.MULTILINE)
    return match.group(1) if match else None


def _existing_pe(env) -> dict:
    """Is Planet Express already here, and where? A systemd unit or /etc/default says where the
    install and its config actually are, which is not always the default path."""
    unit = env.read("/etc/systemd/system/casa-planetexpress.service")
    defaults = env.read("/etc/default/casa-planetexpress")
    install_dir = _assigned(unit, "WorkingDirectory") or _assigned(defaults, "DAEMON_DIR")
    config_path = _assigned(unit, "CASA_CONFIG") or _assigned(defaults, "CASA_CONFIG")
    markers = {
        "config": env.exists(config_path or "/etc/planetexpress/config.yaml"),
        "env_file": env.exists("/etc/planetexpress.env"),
        "systemd_unit": unit is not None,
        "init_script": env.exists("/etc/init.d/casa-planetexpress"),
    }
    return {"installed": any(markers.values()), **markers,
            "install_dir": install_dir, "config_path": config_path}


def _check(id_: str, label: str, status: str, detail: str, fix: str = "", overridable: bool = False) -> dict:
    return {"id": id_, "label": label, "status": status, "detail": detail, "fix": fix,
            "overridable": overridable}


def _checks(facts: dict) -> list[dict]:
    host, docker, storage = facts["host"], facts["docker"], facts["storage"]
    mos = host["init_system"] == "mos"
    checks = []

    if host["init_system"] == "unknown":
        checks.append(_check("init_system", "Init system", "blocked",
                             "Neither systemd nor MOS was detected.",
                             "Planet Express supports systemd hosts and MOS."))
    else:
        checks.append(_check("init_system", "Init system", "ok",
                             f"{host['os']} ({'MOS, sysvinit' if mos else 'systemd'})"))

    priv = facts["privileges"]
    if priv["root"]:
        checks.append(_check("privileges", "Privileges", "ok", "Running as root."))
    elif priv["can_sudo"]:
        checks.append(_check("privileges", "Privileges", "warn", "Not root, but sudo works without a password.",
                             "Setup writes system files; re-run it with sudo.", True))
    else:
        checks.append(_check("privileges", "Privileges", "blocked",
                             "Setup writes system files and needs root.",
                             "Re-run as root, or with a user that has passwordless sudo."))

    if not docker["installed"]:
        fix = ("Enable Docker in the MOS web UI (Settings, Docker), then re-check." if mos
               else "Install Docker Engine for your distribution, then re-check. "
                    "Planet Express guides this; it does not install Docker for you.")
        checks.append(_check("docker", "Docker", "blocked", "Docker is not installed.", fix))
    elif not docker["daemon_running"]:
        fix = ("Enable the Docker service in the MOS web UI (Settings, Docker)." if mos
               else "Start the Docker service, then re-check.")
        checks.append(_check("docker", "Docker", "blocked",
                             "Docker is installed but its daemon is not running.", fix))
    else:
        checks.append(_check("docker", "Docker", "ok", f"Docker {docker['version']} is running."))

    if docker["installed"]:
        flavour = docker["compose_flavour"]
        if flavour == "plugin":
            checks.append(_check("compose", "Docker Compose", "ok", "The `docker compose` plugin is available."))
        elif flavour == "standalone":
            checks.append(_check("compose", "Docker Compose", "ok",
                                 "Standalone `docker-compose` found; Planet Express uses it on this host."))
        else:
            checks.append(_check("compose", "Docker Compose", "blocked", "Neither `docker compose` nor `docker-compose` was found.",
                                 "Install the Docker Compose plugin, then re-check."))

    if mos:
        if storage["candidate_install_paths"]:
            checks.append(_check("storage", "Persistent storage", "ok",
                                 f"Pool(s) available: {', '.join(p['mount'] for p in storage['pools'])}. "
                                 "MOS keeps / in RAM, so Planet Express lives on a pool."))
        else:
            checks.append(_check("storage", "Persistent storage", "blocked",
                                 "MOS keeps / in RAM and no pool with enough free space is mounted.",
                                 f"Create and mount a pool of at least {MIN_INSTALL_GIB:g} GiB in the MOS web UI "
                                 "(Pools), then re-check. Planet Express guides this; it does not create pools."))
    elif storage["persistent"]:
        checks.append(_check("storage", "Persistent storage", "ok", f"Root filesystem is {storage['root_fstype']}."))
    else:
        checks.append(_check("storage", "Persistent storage", "warn",
                             f"Root filesystem type is {storage['root_fstype']}; it may not survive a reboot.",
                             "Install on a persistent disk.", True))

    in_use = facts["ports_in_use"].get(str(DASHBOARD_PORT), False)
    if in_use and facts["existing_pe"]["installed"]:
        checks.append(_check("dashboard_port", f"Port {DASHBOARD_PORT}", "ok",
                             "In use, which is expected: Planet Express is already installed here."))
    elif in_use:
        checks.append(_check("dashboard_port", f"Port {DASHBOARD_PORT}", "warn",
                             f"Something else is listening on {DASHBOARD_PORT}.",
                             "Choose another dashboard port in the next step.", True))
    else:
        checks.append(_check("dashboard_port", f"Port {DASHBOARD_PORT}", "ok", "Free."))

    existing = facts["existing_pe"]
    if existing["installed"]:
        found = [k for k in ("config", "env_file", "systemd_unit", "init_script") if existing[k]]
        checks.append(_check("existing_pe", "Existing install", "warn",
                             f"Planet Express is already installed here ({', '.join(found)}).",
                             "Continue as a repair or upgrade, or uninstall first.", True))
    else:
        checks.append(_check("existing_pe", "Existing install", "ok", "No Planet Express install found."))

    count = len(facts["stacks"])
    checks.append(_check("stacks", "Compose stacks", "ok",
                         f"{count} stack(s) found under {facts['stacks'][0]['root']}." if count
                         else "No stacks found yet. That is normal on a new host."))

    if facts["network"]["public_addresses"]:
        checks.append(_check("exposure", "Network exposure", "warn",
                             "This host has a publicly routable address: "
                             f"{', '.join(facts['network']['public_addresses'])}.",
                             "Setup must only be reachable from your LAN. Do not forward its port.", True))
    return checks


def discover(env=None, *, repo_root: str | None = None, stacks_root: str | None = None) -> dict:
    """The full report. `env` defaults to the real host; `repo_root` is where this checkout lives."""
    env = env or SystemEnv()
    repo_root = repo_root or str(Path(__file__).resolve().parents[2])
    host = _host(env)
    docker = _docker(env)
    roots = _stack_roots(env, stacks_root)
    facts = {
        "schema": SCHEMA,
        "host": host,
        "docker": docker,
        "storage": _storage(env, host, repo_root),
        "stacks_roots": roots,
        "stacks": _stacks(env, roots),
        "containers_running": _containers(env, docker),
        "ports_in_use": _listening_ports(env),
        "network": _network(env),
        "privileges": _privileges(env),
        "existing_pe": _existing_pe(env),
    }
    facts["checks"] = _checks(facts)
    counts = {s: sum(1 for c in facts["checks"] if c["status"] == s) for s in ("ok", "warn", "blocked")}
    facts["summary"] = {**counts, "can_continue": not any(
        c["status"] == "blocked" and not c["overridable"] for c in facts["checks"])}
    return facts
