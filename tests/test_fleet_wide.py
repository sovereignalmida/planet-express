"""rc14.12 (operator, live): every tab shows the whole fleet, not only this host.

Overview's stacks include the other hosts' (grouped by Compose project), Farnsworth's lines count all hosts, the Hosts tab has
this host's own card (same metrics, same read, run locally), a metrics-only host reports its OS, and the host cards stop
saying "not reportedC" or the architecture twice.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_ssh import INFO, LINUX_METRICS, _container, _hosts_page, _inventory, _record  # noqa: E402

import dashboard_data  # noqa: E402
from config_schema import HostEntry  # noqa: E402
from planet_express.application import fleet_collector as fc  # noqa: E402
from planet_express.application.multi_host import Fleet, local_card, remote_stacks, ssh_card  # noqa: E402
from planet_express.integrations import ssh_hosts  # noqa: E402


def test_a_host_without_docker_says_what_it_is_from_the_metrics_read():
    m = ssh_hosts.parse_metrics(LINUX_METRICS + "os_name Raspbian GNU/Linux 12 (bookworm)\nuname_m aarch64\nuname_r 6.6.51+rpt\n")
    assert (m["os_name"], m["arch"], m["kernel"]) == ("Raspbian GNU/Linux 12 (bookworm)", "aarch64", "6.6.51+rpt")
    solar = {**_record(990.0), "ssh": "solar", "name": "SOLAR", "engine": None, "containers": None, "metrics": m}
    card = ssh_card(HostEntry(name="SOLAR", ssh="solar", docker=False), solar, 1000.0)
    assert (card.details.os_name, card.details.arch, card.details.kernel) == ("Raspbian GNU/Linux 12 (bookworm)", "aarch64", "6.6.51+rpt")
    assert all(key in ssh_hosts.READ_COMMANDS["metrics"] for key in ("os_name", "uname_m", "uname_r"))


def test_the_architecture_is_not_said_twice_and_unknown_cores_are_not_printed():
    rec = {**_record(990.0), "engine": {"os": "Unraid OS 7.3 x86_64", "arch": "x86_64", "ncpu": 2}}
    card = ssh_card(HostEntry(name="UNRAID", ssh="casalab"), rec, 1000.0)
    assert card.details.os_name == "Unraid OS 7.3" and card.details.arch == "x86_64"
    page = _hosts_page((ssh_card(HostEntry(name="CASALAB", ssh="casalab"), _record(1800000000.0 - 5), 1800000000.0),)
                       ).get_data(as_text=True)
    assert "not reportedC" not in page and "4T" in page


def test_the_collector_reads_this_hosts_metrics_locally_and_keeps_the_last_good_on_failure(tmp_path, monkeypatch):
    inv = _inventory()
    col = fc.FleetCollector(inv, tmp_path / "hosts.json", read=lambda *a: "", local_id=lambda: "LOCAL", clock=lambda: 50.0)
    seen = []

    def run_local(command):
        seen.append(command)
        return LINUX_METRICS if command == ssh_hosts.READ_COMMANDS["metrics"] else INFO
    monkeypatch.setattr(col, "_run_local", run_local)
    local = col.collect()["local"]
    assert seen == [ssh_hosts.READ_COMMANDS["metrics"], ssh_hosts.READ_COMMANDS["info"]]   # the same fixed reads, no SSH
    assert local["read_at"] == 50.0 and local["metrics"]["cpu_pct"] == pytest.approx(50.0) and local["engine"]["name"] == "casalab"

    def broken(command):
        raise subprocess.TimeoutExpired(command, 20)
    monkeypatch.setattr(col, "_run_local", broken)
    col._clock = lambda: 99.0
    kept = col.collect()["local"]
    assert kept["read_at"] == 50.0 and kept["attempted_at"] == 99.0 and "failed" in kept["error"]


def test_the_local_read_runs_with_no_secret_from_cores_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"os Linux\n", b"")
    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    col = fc.FleetCollector(_inventory(), Path("/nonexistent/h.json"))
    col._run_local("echo")
    assert set(seen["env"]) == {"PATH", "HOME", "LANG"}


def test_this_host_is_first_on_hosts_named_by_its_engine_with_its_container_count(tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [_record(990.0)], "local": {
        "read_at": 995.0, "attempted_at": 995.0, "error": None, "metrics": ssh_hosts.parse_metrics(LINUX_METRICS),
        "engine": {"name": "casamediaserver", "os": "Ubuntu 24.04.5 LTS", "arch": "x86_64"}}}))
    view = Fleet(_inventory({"name": "CASALAB", "ssh": "casalab"}), path, wall=lambda: 1000.0, local_containers=lambda: 87).view()
    me = view.cards[0]
    assert (me.name, me.source, me.local_containers, me.liveness.state) == ("casamediaserver", "local", 87, "current")
    assert [c.name for c in view.cards] == ["casamediaserver", "CASALAB"]
    assert local_card(None, 1000.0, name="x", containers=None).liveness.state == "unknown"


def test_the_this_host_card_renders_wide_with_a_link_to_overview():
    rec = {"read_at": 1800000000.0 - 5, "attempted_at": 1800000000.0 - 5, "error": None,
           "metrics": ssh_hosts.parse_metrics(LINUX_METRICS), "engine": {"name": "casamediaserver"}}
    page = _hosts_page((local_card(rec, 1800000000.0, name="casamediaserver", containers=87),)).get_data(as_text=True)
    assert "THIS HOST" in page and "grid-column:1/-1" in page and "on Overview" in page and ">87 " in page
    assert "Planet Express runs here" in page


def test_the_other_hosts_stacks_are_grouped_by_compose_project_worst_first(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    proj = {"traefik.enable": "true", "com.docker.compose.project": "arr4k"}
    path = tmp_path / "hosts.json"
    containers = ssh_hosts.parse_containers("\n".join([
        _container("radarr", labels=proj), _container("sonarr", labels=proj, health="unhealthy"),
        _container("lonely", labels={}), _container("oneshot", state="exited", exit=0, labels={})]))
    path.write_text(json.dumps({"hosts": [_record(990.0, containers=containers)]}))
    stacks = remote_stacks(inv, path, wall=lambda: 1000.0)
    by = {s["name"]: s for s in stacks}
    assert stacks[0]["name"] == "arr4k" and by["arr4k"]["level"] == "warn" and (by["arr4k"]["up"], by["arr4k"]["total"]) == (1, 2)
    assert [m["level"] for m in by["arr4k"]["members"]] == ["warn", "ok"]
    assert by["oneshot"]["members"][0]["level"] == "idle" and by["oneshot"]["level"] == "ok"
    assert all(s["host"] == "CASALAB" for s in stacks)
    assert remote_stacks(inv, path, wall=lambda: 990.0 + 300) == []                        # not current: not shown


def _lines(remote, routers=()):
    containers = {"available": True, "total": 87, "healthy": 87, "down": 0, "degraded": 0}
    ctx = {"containers": containers, "findings": {"counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}},
           "system_and_backups": {"available": False}, "traefik": {"available": True, "routers": list(routers)},
           "adguard": {}, "health": {"last_scan_mode": "full"}, "pipeline_status": {"state": "idle"}, "remote_hosts": remote}
    return dashboard_data.build_professor_lines(ctx)


def test_farnsworth_counts_the_whole_fleet_and_says_which_hosts_are_not_counted():
    lines = _lines({"hosts": 3, "unknown": 0, "containers": 15, "problems": 0, "cells": ["online"] * 15})
    assert "All 102 of 102 containers across 4 hosts are alive and well" in lines["overview"]
    assert lines["hosts"].startswith("3 other hosts reading, 15 containers between them.")
    bad = _lines({"hosts": 2, "unknown": 1, "containers": 10, "problems": 1, "cells": ["online"] * 9 + ["down"]})
    assert "1 of 97 containers across 3 hosts are down" in bad["overview"] and "1 host not reading, not counted" in bad["overview"]
    assert "1 not answering" in bad["hosts"]
    assert "All 87 of 87 containers are alive" in _lines(None)["overview"] and "Just this ship" in _lines(None)["hosts"]


def test_the_network_line_says_how_many_routes_pe_wrote_for_other_hosts():
    routers = [{"name": "radarr@docker", "status": "enabled"}, {"name": "pe-casalab--CASA4K_RADARR--radarr4k@file", "status": "enabled"},
               {"name": "pe-handwritten", "status": "enabled"}]
    assert "1 of them I wrote myself, for containers on the other hosts." in _lines(None, routers)["network"]


def test_the_other_hosts_stacks_do_not_depend_on_this_hosts_stack_data():
    """Codex, rc14.12: inside the local-stacks conditional, a scan without this host's stacks hid failing remote containers."""
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
    local_end = tpl.index("No compose stacks found.")
    remote = tpl.rindex("{% if ctx.remote_stacks %}")                      # the block, not the header count
    section_end = tpl.index("</section>", remote)
    between = tpl[local_end:remote]
    # the services conditional closes (its last endif) before the remote block opens, and the remote block is in the panel
    assert between.rstrip().endswith("{% endif %}") and "{% else %}" in between and tpl.index("ON OTHER HOSTS") < section_end


def test_the_console_agent_is_told_the_other_hosts_are_in_inventory_md():
    """Live (rc14.11.1): asked about the 4K apps on CASALAB, the agent searched this host with `sudo find /`; INVENTORY.md listed
    them all along but nothing told it to look there."""
    from planet_express.console.runtime import SYSTEM
    assert "INVENTORY.md" in SYSTEM and "Other hosts" in SYSTEM and "before searching this host" in SYSTEM
