"""Release A of docs/designs/multi-host.md: the other Docker hosts read over SSH by Core, shown on Hosts and the Overview tile.

What must hold: PE can send another host only the fixed reads in `READ_COMMANDS` (no caller, config value or host answer can
become a command); the alias is an alias, never an option; Core's secrets never reach ssh's environment; container env is never
read; a failed read keeps the last good values and says why, never "0 containers"; an alias that reaches this host's own engine is
refused; one host's failure never stops another's.
"""
import json
from datetime import datetime, timezone
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_scruffy_routes import _hosts_page  # noqa: E402

from config_io import validate_config_text  # noqa: E402
from config_schema import HostEntry, MultiHostConfig  # noqa: E402
from planet_express.application import fleet_collector as fc  # noqa: E402
from planet_express.application.multi_host import Fleet, remote_summary, ssh_card  # noqa: E402
from planet_express.integrations import ssh_hosts  # noqa: E402

import dashboard_data  # noqa: E402

LINUX_METRICS = """os Linux
load 1.61 1.78 1.87
mem_kib 8000000 4000000
cpu 1000 800 1100 850
disk_kib 1000000 250000
temp_milli 53000
cpu_model Intel(R) N150
threads 4
"""
MAC_METRICS = """os Darwin
load 0.5 0.4 0.3
mem_bytes 17179869184
mem_avail_bytes 4294967296
cpu_pct 12.5
disk_kib 500000000 100000000
cpu_model Apple M2
threads 8
"""
INFO = '{"id":"REMOTE-ENGINE","name":"casalab","version":"27.1","os":"Ubuntu 26.04","arch":"x86_64","kernel":"6.8","ncpu":4,"mem":16000000000,"root":"/var/lib/docker"}'


def _container(name, **kw):
    d = {"name": "/" + name, "image": "img:" + name, "state": "running", "health": None, "restarts": 0,
         "started": "2026-11-12T10:00:00Z", "exit": 0,
         "labels": {"traefik.http.routers.x.rule": "Host(`x.casalan.com`)", "com.docker.compose.project": "media",
                    "org.opencontainers.image.source": "https://example", "SECRET_ISH": "no"},
         "ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}], "9000/tcp": None}}
    d.update(kw)
    return json.dumps(d)


def _reader(answers):
    """A fake `ssh_hosts.run`: answers[alias][name] is text, or an exception to raise."""
    def read(alias, name):
        value = answers[alias][name]
        if isinstance(value, Exception):
            raise value
        return value
    return read


# ── the alias and the command table ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("alias", ["-oProxyCommand=touch /tmp/x", "root@host", "a b", "host;id", "", ".hidden", "x" * 65,
                                   "host\n", "$(id)"])
def test_an_ssh_alias_that_is_not_a_plain_alias_is_refused_at_config(alias):
    model, errors = validate_config_text(f"stacks_root: /srv\nmulti_host:\n  hosts:\n    - name: x\n      ssh: {json.dumps(alias)}\n")
    assert model is None and errors[0]["loc"].startswith("multi_host.hosts.0")


def test_a_host_is_read_over_ssh_without_beszel_and_duplicates_or_empty_entries_are_refused():
    ok, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts:\n    - name: CASALAB\n      ssh: casalab\n")
    assert errors == [] and ok.multi_host.hosts[0].ssh == "casalab" and ok.multi_host.hosts[0].system_id is None
    dup, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts:\n    - {name: a, ssh: h}\n    - {name: b, ssh: h}\n")
    assert dup is None and "duplicate ssh alias" in errors[0]["msg"]
    empty, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts:\n    - {name: a}\n")
    assert empty is None and "nothing to show" in errors[0]["msg"]


def test_only_the_fixed_reads_can_be_sent_and_the_alias_comes_after_the_option_terminator():
    argv = ssh_hosts.ssh_argv("casalab", "info")
    assert argv[0] == "ssh" and argv[-3:] == ["--", "casalab", ssh_hosts.READ_COMMANDS["info"]]
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    with pytest.raises(KeyError):
        ssh_hosts.ssh_argv("casalab", "docker rm -f x")
    assert set(ssh_hosts.READ_COMMANDS) == {"info", "containers", "metrics"}      # an addition is a reviewed change
    for command in ssh_hosts.READ_COMMANDS.values():
        assert ".Config.Env" not in command and "docker exec" not in command
        assert not any(w in command for w in (" rm ", " stop ", " kill ", " restart ", " pull ", "compose"))


def test_ssh_runs_with_no_secret_from_cores_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"out", b"")
    assert ssh_hosts.run("casalab", "info", runner=runner) == "out"
    assert set(seen["env"]) == {"HOME", "PATH", "LANG"} and "sk-or-secret" not in json.dumps(seen["env"])
    assert seen["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("code,stderr,words,cause", [
    (255, b"Host key verification failed.", "host key does not match", "not_permitted"),
    (255, b"casaroot@192.168.1.80: Permission denied (publickey).", "key was refused", "not_permitted"),
    (255, b"ssh: Could not resolve hostname nohost", "no such SSH alias", "no_contact"),
    (127, b"bash: docker: command not found", "not on that host's PATH", "no_contact"),
    (1, b"permission denied while trying to connect to the Docker daemon socket", "cannot reach Docker", "not_permitted"),
    (255, b"ssh: connect to host 192.168.1.80 port 22: No route to host", "no SSH contact", "no_contact"),
])
def test_a_failed_read_says_why_in_words(code, stderr, words, cause):
    with pytest.raises(ssh_hosts.RemoteReadError) as err:
        ssh_hosts.run("h", "info", runner=lambda argv, **kw: subprocess.CompletedProcess(argv, code, b"", stderr))
    assert words in str(err.value) and err.value.cause == cause


def test_a_read_that_hangs_is_a_reason_not_a_stuck_collector():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    with pytest.raises(ssh_hosts.RemoteReadError, match="no answer within"):
        ssh_hosts.run("h", "metrics", runner=runner, timeout=3)


# ── parsing ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_containers_keep_routing_and_stack_labels_only_and_published_ports_and_skip_garbage():
    text = "\n".join([_container("b", health="unhealthy", restarts=7), "not json", '{"no":"name"}', _container("a")])
    cs = ssh_hosts.parse_containers(text)
    assert [c["name"] for c in cs] == ["a", "b"]
    assert set(cs[0]["labels"]) == {"traefik.http.routers.x.rule", "com.docker.compose.project"}
    assert cs[0]["ports"] == [{"container": "8080/tcp", "host_port": 8080, "host_ip": "0.0.0.0"}]
    assert cs[1]["health"] == "unhealthy" and cs[1]["restarts"] == 7
    assert "env" not in json.dumps(cs).lower()


def test_metrics_are_read_on_linux_and_macos_and_what_is_missing_is_none_not_zero():
    lin = ssh_hosts.parse_metrics(LINUX_METRICS)
    assert lin["cpu_pct"] == pytest.approx(50.0) and lin["mem_pct"] == pytest.approx(50.0) and lin["disk_pct"] == pytest.approx(25.0)
    assert lin["load"] == [1.61, 1.78, 1.87] and lin["temp_c"] == 53.0 and lin["threads"] == 4 and lin["cpu_model"] == "Intel(R) N150"
    mac = ssh_hosts.parse_metrics(MAC_METRICS)
    assert mac["cpu_pct"] == 12.5 and mac["mem_pct"] == pytest.approx(75.0) and mac["disk_pct"] == pytest.approx(20.0)
    assert "temp_c" not in mac
    assert ssh_hosts.parse_metrics("os Linux\nmem_kib 0 0\ncpu 5 5 5 5\nload x y z\n") == {"os_kind": "Linux"}


def test_engine_info_is_read_and_a_bad_answer_is_all_none():
    e = ssh_hosts.parse_info(INFO)
    assert (e.id, e.os, e.ncpu) == ("REMOTE-ENGINE", "Ubuntu 26.04", 4)
    assert ssh_hosts.parse_info("garbage").id is None and ssh_hosts.parse_info("").id is None


# ── the collector ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _inventory(*entries):
    return MultiHostConfig(hosts=[HostEntry(**e) for e in entries])


def test_the_collector_reads_every_host_and_one_failure_does_not_stop_the_others(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"}, {"name": "UNRAID", "ssh": "unraid", "link": "https://u"},
                     {"name": "LINK ONLY", "link": "http://192.168.1.154"})
    read = _reader({"casalab": {"info": INFO, "containers": _container("radarr"), "metrics": LINUX_METRICS},
                    "unraid": {"info": RuntimeError("boom")}})
    data = fc.FleetCollector(inv, tmp_path / "hosts.json", read=read, local_id=lambda: "LOCAL", clock=lambda: 1000.0).collect()
    by = {h["name"]: h for h in data["hosts"]}
    assert set(by) == {"CASALAB", "UNRAID"}                           # a link-only entry is not read
    assert by["CASALAB"]["read_at"] == 1000.0 and by["CASALAB"]["containers"][0]["name"] == "radarr"
    assert by["UNRAID"]["read_at"] is None and "unexpectedly" in by["UNRAID"]["error"]
    assert json.loads((tmp_path / "hosts.json").read_text()) == data
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".hosts-")]          # no temporary file left behind


def test_a_failed_read_keeps_the_last_good_values_with_their_own_time_and_the_reason(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    path = tmp_path / "hosts.json"
    good = _reader({"casalab": {"info": INFO, "containers": _container("radarr"), "metrics": LINUX_METRICS}})
    fc.FleetCollector(inv, path, read=good, local_id=lambda: "LOCAL", clock=lambda: 1000.0).collect()
    bad = _reader({"casalab": {"info": ssh_hosts.RemoteReadError("no SSH contact (No route to host)")}})
    rec = fc.FleetCollector(inv, path, read=bad, local_id=lambda: "LOCAL", clock=lambda: 1300.0).collect()["hosts"][0]
    assert rec["read_at"] == 1000.0 and rec["attempted_at"] == 1300.0 and rec["containers"][0]["name"] == "radarr"
    assert rec["error"].startswith("no SSH contact") and rec["cause"] == "no_contact"


def test_an_alias_that_reaches_this_hosts_own_engine_is_refused_and_shows_nothing(tmp_path):
    inv = _inventory({"name": "OOPS", "ssh": "localhost"})
    read = _reader({"localhost": {"info": INFO.replace("REMOTE-ENGINE", "LOCAL"), "containers": _container("x"), "metrics": ""}})
    rec = fc.FleetCollector(inv, tmp_path / "h.json", read=read, local_id=lambda: "LOCAL").collect()["hosts"][0]
    assert rec["cause"] == "this_host" and rec["containers"] is None and "it is this host" in rec["error"]


def test_metrics_failing_alone_still_gives_a_container_reading():
    read = _reader({"h": {"info": INFO, "containers": _container("a"), "metrics": ssh_hosts.RemoteReadError("no answer within 20s")}})
    r = fc.read_host("h", local_id="LOCAL", read=read, now=lambda: 5.0)
    assert r["read_at"] == 5.0 and r["containers"] and r["metrics"] == {"error": "no answer within 20s"}


def test_the_local_engine_id_is_none_when_docker_does_not_answer():
    assert fc.local_engine_id(runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"x")) is None
    assert fc.local_engine_id(runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, b"ABC\n", b"")) == "ABC"


# ── the page and the Overview ────────────────────────────────────────────────────────────────────────────────────────────────
def _record(read_at, error=None, cause=None, containers=None, attempted=None):
    return {"name": "CASALAB", "ssh": "casalab", "link": None, "read_at": read_at, "error": error, "cause": cause,
            "attempted_at": attempted if attempted is not None else (read_at if read_at is not None else 1000.0),
            "engine": {"os": "Ubuntu 26.04", "arch": "x86_64", "ncpu": 4},
            "metrics": ssh_hosts.parse_metrics(LINUX_METRICS),
            "containers": containers if containers is not None else ssh_hosts.parse_containers(
                "\n".join([_container("radarr", health="unhealthy", restarts=3), _container("sab", state="exited")]))}


def test_a_card_is_unknown_until_core_has_read_the_host_then_current_then_stale_with_the_reason():
    entry = HostEntry(name="CASALAB", ssh="casalab")
    assert ssh_card(entry, None, 1000.0).liveness.state == "unknown"
    card = ssh_card(entry, _record(990.0), 1000.0)
    assert card.liveness.state == "current" and card.metrics.cpu_pct == pytest.approx(50.0)
    assert [(c.name, c.status, c.health, c.restarts) for c in card.containers] == [
        ("radarr", "running", "unhealthy", 3), ("sab", "exited", None, 0)]
    stale = ssh_card(entry, _record(500.0, error="no SSH contact", attempted=990.0), 1000.0)
    assert stale.liveness.state == "stale" and stale.status == "no SSH contact" and stale.containers
    never = ssh_card(entry, {**_record(None, error="the SSH key was refused", cause="not_permitted"), "containers": None}, 1000.0)
    assert never.liveness.state == "unknown" and never.liveness.cause == "not_permitted" and never.containers is None


def test_the_overview_counts_other_hosts_beside_this_one_and_never_sums_an_unknown_host_as_zero(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"}, {"name": "UNRAID", "ssh": "unraid"})
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [_record(990.0)]}))
    summary = remote_summary(inv, path, wall=lambda: 1000.0)
    assert summary == {"hosts": 1, "unknown": 1, "containers": 2, "problems": 1}
    assert dashboard_data.remote_hosts_line(summary) == "+ 2 on 1 other host · 1 with a problem · 1 host not reading"
    assert remote_summary(_inventory({"name": "B", "link": "http://b"}), path) is None
    assert dashboard_data.remote_hosts_line(None) is None


def test_the_fleet_view_keeps_inventory_order_and_reads_ssh_hosts_from_cores_file(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"}, {"name": "SOLAR", "link": "http://192.168.1.154"})
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [_record(990.0)]}))
    view = Fleet(inv, path, wall=lambda: 1000.0).view()
    assert [(c.name, c.source, c.liveness.state) for c in view.cards] == [("CASALAB", "ssh", "current"), ("SOLAR", "link", "unknown")]


def test_the_hosts_page_lists_every_container_of_an_ssh_host_and_says_why_one_cannot_be_read():
    entry = HostEntry(name="CASALAB", ssh="casalab")
    many = ssh_hosts.parse_containers("\n".join(_container(f"svc{i:02d}") for i in range(9)))
    cards = (ssh_card(entry, _record(1800000000.0 - 5, containers=many), 1800000000.0),
             ssh_card(HostEntry(name="UNRAID", ssh="unraid"),
                      {**_record(None, error="the SSH key was refused", cause="not_permitted", attempted=1800000000.0 - 30),
                       "name": "UNRAID", "ssh": "unraid"},
                      1800000000.0))
    page = _hosts_page(cards).get_data(as_text=True)
    assert all(f"svc{i:02d}" in page for i in range(9)) and "more containers" not in page
    assert "the SSH key was refused" in page and "ssh unraid docker info" in page
    assert "read over SSH by Core" in page and "BESZEL" not in page


# ── Codex, release A ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _fake_docker(tmp_path, ps_exit, ps_out=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = ps ]; then printf "{ps_out}"; exit {ps_exit}; fi\n'
        'if [ "$1" = inspect ]; then shift 3; for id in "$@"; do echo "{\\"name\\":\\"/$id\\"}"; done; exit 0; fi\n'
        "exit 9\n")
    (bin_dir / "docker").chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}


def test_the_container_read_fails_when_docker_ps_fails_instead_of_reading_as_no_containers(tmp_path):
    cmd = ssh_hosts.READ_COMMANDS["containers"]
    failed = subprocess.run(["sh", "-c", cmd], env=_fake_docker(tmp_path, 1), capture_output=True)
    assert failed.returncode != 0
    (tmp_path / "bin" / "docker").unlink(); (tmp_path / "bin").rmdir()
    empty = subprocess.run(["sh", "-c", cmd], env=_fake_docker(tmp_path, 0), capture_output=True)
    assert empty.returncode == 0 and empty.stdout == b""                    # a host with no containers is a real answer
    (tmp_path / "bin" / "docker").unlink(); (tmp_path / "bin").rmdir()
    two = subprocess.run(["sh", "-c", cmd], env=_fake_docker(tmp_path, 0, "aaa\\nbbb\\n"), capture_output=True)
    assert two.returncode == 0 and [c["name"] for c in ssh_hosts.parse_containers(two.stdout.decode())] == ["aaa", "bbb"]


def test_the_snapshot_is_group_readable_for_the_dashboard_user(tmp_path):
    fc.write_atomic(tmp_path / "hosts.json", {"hosts": []})
    assert (tmp_path / "hosts.json").stat().st_mode & 0o777 == 0o640


def test_two_hosts_with_one_display_name_keep_their_own_readings(tmp_path):
    inv = _inventory({"name": "BOX", "ssh": "a"}, {"name": "BOX", "ssh": "b"})
    read = _reader({"a": {"info": INFO, "containers": _container("only-on-a"), "metrics": ""},
                    "b": {"info": INFO.replace("REMOTE-ENGINE", "B-ENGINE"), "containers": _container("only-on-b"), "metrics": ""}})
    data = fc.FleetCollector(inv, tmp_path / "h.json", read=read, local_id=lambda: "LOCAL", clock=lambda: 10.0).collect()
    assert [(h["ssh"], h["containers"][0]["name"]) for h in data["hosts"]] == [("a", "only-on-a"), ("b", "only-on-b")]
    view = Fleet(inv, tmp_path / "h.json", wall=lambda: 15.0).view()
    assert [c.containers[0].name for c in view.cards] == ["only-on-a", "only-on-b"]


def test_an_old_reading_becomes_unknown_and_a_collector_that_stopped_trying_says_so():
    entry = HostEntry(name="CASALAB", ssh="casalab")
    gone = ssh_card(entry, _record(0.0, error="no SSH contact", attempted=1000.0), 1000.0)
    assert gone.liveness.state == "unknown" and "no good reading for 16 min: no SSH contact" == gone.liveness.reason
    idle = ssh_card(entry, _record(990.0, attempted=100.0), 1000.0)
    assert idle.liveness.state == "unknown" and idle.problem == "not_trying" and "is Core running?" in idle.liveness.reason


def test_a_stale_host_is_not_counted_on_the_overview_as_reading(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [_record(700.0, error="no SSH contact", attempted=990.0)]}))
    assert remote_summary(inv, path, wall=lambda: 1000.0) == {"hosts": 0, "unknown": 1, "containers": 0, "problems": 0}


def test_a_container_gone_between_list_and_inspect_still_gives_a_reading(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text('#!/bin/sh\nif [ "$1" = ps ]; then printf "aaa\\ngone\\n"; exit 0; fi\n'
                                    'echo "{\\"name\\":\\"/aaa\\"}"; echo "Error: No such object: gone" >&2; exit 1\n')
    (bin_dir / "docker").chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(["sh", "-c", ssh_hosts.READ_COMMANDS["containers"]], env=env, capture_output=True)
    assert out.returncode == 0 and [c["name"] for c in ssh_hosts.parse_containers(out.stdout.decode())] == ["aaa"]
    (bin_dir / "docker").write_text('#!/bin/sh\nif [ "$1" = ps ]; then printf "aaa\\n"; exit 0; fi\nexit 1\n')
    assert subprocess.run(["sh", "-c", ssh_hosts.READ_COMMANDS["containers"]], env=env, capture_output=True).returncode != 0


def test_a_malformed_record_is_that_hosts_problem_not_the_pages(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    path = tmp_path / "hosts.json"
    bad = _record(990.0)
    bad["containers"][0]["ports"] = ["8080", {"host_port": "x"}]
    bad["metrics"]["load"] = "high"
    bad["engine"] = {"os": 5, "ncpu": True}
    path.write_text(json.dumps({"hosts": [bad]}))
    card = Fleet(inv, path, wall=lambda: 1000.0).view().cards[0]
    assert card.liveness.state == "current" and card.containers[0].ports is None and card.metrics.load is None
    assert card.details.os_name is None and card.details.threads == 4


def test_link_only_hosts_do_not_count_as_hosts_that_failed_to_read_and_this_host_says_so():
    from planet_express.application.multi_host import HostCard
    from planet_express.core.hosts import Liveness
    entry = HostEntry(name="CASALAB", ssh="casalab")
    link = HostCard("SOLAR", "SOLAR", "http://192.168.1.154", True, Liveness("unknown", "link only"), source="link")
    page = _hosts_page((ssh_card(entry, _record(1800000000.0 - 5), 1800000000.0), link)).get_data(as_text=True)
    assert "1 OTHER HOSTS READING" in page and "NOT READING" not in page and "LINK ONLY" in page
    me = ssh_card(entry, {**_record(None, error=fc.THIS_HOST, cause="this_host", attempted=1800000000.0 - 5), "containers": None},
                  1800000000.0)
    page = _hosts_page((me,)).get_data(as_text=True)
    assert "THIS HOST" in page and "Remove this entry from multi_host.hosts" in page and "NOT PERMITTED" not in page


def test_a_failed_try_shows_even_while_the_last_reading_is_still_current():
    entry = HostEntry(name="CASALAB", ssh="casalab")
    card = ssh_card(entry, _record(1800000000.0 - 30, error="the host key does not match the one pinned in known_hosts: collection stopped",
                                   cause="not_permitted", attempted=1800000000.0 - 5), 1800000000.0)
    assert card.liveness.state == "current"
    assert "Last try failed: the host key does not match" in _hosts_page((card,)).get_data(as_text=True)


def test_the_local_engine_is_asked_again_until_it_answers_and_records_say_whether_it_was(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    answers = iter([None, "LOCAL"])
    col = fc.FleetCollector(inv, tmp_path / "h.json", read=_reader({"casalab": {"info": INFO, "containers": "", "metrics": ""}}),
                            local_id=lambda: next(answers))
    assert col.collect()["hosts"][0]["locality_checked"] is False
    assert col.collect()["hosts"][0]["locality_checked"] is True
    assert fc.HOST_BUDGET_S > 3 * ssh_hosts.TIMEOUT_S


def test_an_oversized_answer_is_refused_not_cut_short_into_a_partial_list():
    big = b"x" * (ssh_hosts.MAX_OUTPUT + 1)
    with pytest.raises(ssh_hosts.RemoteReadError, match="larger than"):
        ssh_hosts.run("h", "containers", runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, big, b""))


# ── release B: the other hosts in the health picture, never in a plan ─────────────────────────────────────────────────────────
from planet_express.application import remote_health  # noqa: E402
from planet_express.core.incidents import observations_from_snapshot  # noqa: E402


def _health_file(tmp_path, containers, read_at=990.0, alias="casalab", name="CASALAB"):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [{**_record(read_at, containers=ssh_hosts.parse_containers("\n".join(containers))),
                                           "ssh": alias, "name": name}]}))
    return path


def test_the_remote_rules_are_leelas_rules_in_dockers_words():
    import casa_leela
    assert remote_health.CRASH_LOOP_RESTARTS == casa_leela.CRASH_LOOP_RESTART_THRESHOLD
    issue = lambda **kw: remote_health.container_issue({"state": "running", "health": None, "restarts": 0, **kw})  # noqa: E731
    assert issue() == (None, False)
    assert issue(state="exited", exit_code=0) == (None, False)                       # a finished one-shot
    assert issue(state="exited", exit_code=137) == ("not running (exited 137)", False)
    assert issue(state="restarting", restarts=1) == ("restarting", False)
    assert issue(state="dead") == ("dead", False)
    assert issue(health="unhealthy") == ("healthcheck failing", False)
    assert issue(restarts=3) == ("crash-looping (restarted 3x)", True)
    assert issue(health="unhealthy", restarts=4) == ("crash-looping (restarted 4x), healthcheck failing", True)
    assert issue(state="exited", exit_code=1, restarts=9) == ("not running (exited 1)", False)   # stopped is not looping


def test_assess_judges_a_fresh_reading_only_and_names_the_host(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab", "link": "https://c"}, {"name": "SOLAR", "link": "http://s"})
    path = _health_file(tmp_path, [_container("radarr", health="unhealthy"), _container("sab", state="exited", exit=0)])
    (h,) = remote_health.assess(inv, path, now=1000.0)
    assert h["reading"] and h["total"] == 2 and h["names"] == ["radarr", "sab"]
    assert [(p["name"], p["issue"]) for p in h["problems"]] == [("radarr", "healthcheck failing")]
    assert remote_health.report_lines([h]) == ["CASALAB · radarr: healthcheck failing"]
    (old,) = remote_health.assess(inv, path, now=990.0 + 20 * 60)
    assert not old["reading"] and old["problems"] == [] and "is Core running?" in old["reason"]
    assert remote_health.report_lines([old])[0].startswith("CASALAB: not reading")


def test_leela_never_fails_a_scan_because_of_another_host(monkeypatch):
    import casa_leela
    monkeypatch.setattr(remote_health, "assess", lambda *a, **k: 1 / 0)
    assert casa_leela.check_other_hosts() == []


def test_incidents_for_other_hosts_have_their_own_kinds_never_container_health(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    snap = {"other_hosts": remote_health.assess(inv, _health_file(tmp_path, [_container("radarr", health="unhealthy",
                                                                                         image="lscr.io/postgres:16"),
                                                                              _container("sab")]), now=1000.0)}
    obs = {(o.kind, o.resource): o for o in observations_from_snapshot(snap)}
    assert set(obs) == {("remote_host", "casalab"), ("remote_container", "casalab/radarr"), ("remote_container", "casalab/sab")}
    assert obs[("remote_container", "casalab/radarr")].condition == "failing"
    assert obs[("remote_container", "casalab/radarr")].severity == "HIGH"                 # a database image, as locally
    assert obs[("remote_container", "casalab/sab")].condition == "healthy"
    assert not any(o.kind == "container_health" for o in obs.values())
    gone = {"other_hosts": remote_health.assess(inv, _health_file(tmp_path, [_container("radarr")], read_at=0.0), now=1000.0)}
    (only,) = observations_from_snapshot(gone)                       # a host not reading: its containers are not judged
    assert (only.kind, only.condition, only.severity) == ("remote_host", "failing", "MEDIUM")


def test_hermes_and_so_the_planner_never_see_the_other_hosts():
    import casa_hermes
    slim = casa_hermes._slim_snapshot({"containers": [], "other_hosts": [{"host": "CASALAB", "problems": [{"name": "radarr"}]}]})
    assert "other_hosts" not in slim and "radarr" not in json.dumps(slim) and "CASALAB" not in json.dumps(slim)


def test_the_scan_report_says_other_hosts_problems_itself_and_a_quiet_rescan_does_not(monkeypatch):
    import casa_farnsworth
    sent = []
    notifier = SimpleNamespace(notify=sent.append)
    snap = {"other_hosts": [{"host": "CASALAB <x>", "ssh": "casalab", "reading": True, "problems": [
        {"name": "radarr", "issue": "healthcheck failing"}]}, {"host": "UNRAID", "reading": False, "reason": "no SSH contact"}]}
    casa_farnsworth._report_other_hosts(notifier, snap, quiet=True)
    assert sent == []
    casa_farnsworth._report_other_hosts(notifier, snap, quiet=False)
    assert "CASALAB &lt;x&gt; · radarr: healthcheck failing" in sent[0] and "UNRAID: not reading (no SSH contact)" in sent[0]
    assert "does not change them" in sent[0]
    sent.clear()
    casa_farnsworth._report_other_hosts(notifier, {"other_hosts": [{"host": "A", "reading": True, "problems": []}]}, quiet=False)
    assert sent == []


def test_an_incident_on_another_host_says_fix_it_there_and_offers_no_remediation():
    import inspect
    from planet_express.application import command_service
    src = inspect.getsource(command_service)
    assert '"remote_container", "remote_host"' in src and "fix it there" in src
    assert 'row["kind"] == "container_health"' in src                 # the only kind ever proposed for a restart


# ── release C: the Console sees the other hosts, read-only ───────────────────────────────────────────────────────────────────
def test_the_console_inventory_lists_other_hosts_read_only(tmp_path):
    from planet_express.application.multi_host import other_hosts_text
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"}, {"name": "UNRAID", "ssh": "unraid"}, {"name": "SOLAR", "link": "http://s"})
    path = _health_file(tmp_path, [_container("radarr", health="unhealthy", restarts=2)])
    text = other_hosts_text(inv, path, wall=lambda: 1000.0)
    assert "READ-ONLY" in text and "a plan must not name them" in text
    assert "- CASALAB (1 containers, read 10s ago):" in text and "radarr: running, unhealthy, 2 restarts, ports 8080→8080/tcp" in text
    assert "- UNRAID: not reading" in text and "SOLAR" not in text
    assert other_hosts_text(_inventory({"name": "SOLAR", "link": "http://s"}), path) == ""


def test_a_failing_other_hosts_source_never_stops_a_console_turn():
    from planet_express.console.runtime import ConsoleRuntime
    rt = ConsoleRuntime.__new__(ConsoleRuntime)
    rt.other_hosts = lambda: 1 / 0
    assert rt._other_hosts_text() == "\nOther hosts: not available this turn.\n"
    rt.other_hosts = None
    assert rt._other_hosts_text() == ""


def test_beszel_is_gone_and_its_config_keys_still_load():
    import importlib.util
    assert importlib.util.find_spec("planet_express.integrations.beszel") is None
    ok, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts:\n    - {name: SOLAR, system_id: 7y4fosy0ebhtk9x, "
                                      "link: 'https://solar.casalan.com'}\n")
    assert errors == [] and ok.multi_host.hosts[0].ssh is None


def test_a_remote_healthcheck_still_starting_is_unknown_not_healthy(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    snap = {"other_hosts": remote_health.assess(inv, _health_file(tmp_path, [_container("radarr", health="starting")]), now=1000.0)}
    obs = {o.resource: o for o in observations_from_snapshot(snap)}
    assert (obs["casalab/radarr"].condition, obs["casalab/radarr"].severity) == ("unknown", None)


def test_the_console_gets_the_other_hosts_even_without_this_hosts_inventory():
    from planet_express.console.runtime import ConsoleRuntime
    rt = ConsoleRuntime.__new__(ConsoleRuntime)
    rt.inventory, rt.other_hosts = None, (lambda: "\nOther hosts (READ-ONLY): CASALAB\n")
    text = rt._inventory_text()
    assert "do not write plan.json" in text and "Other hosts (READ-ONLY): CASALAB" in text



# ── /code-review, releases B and C ───────────────────────────────────────────────────────────────────────────────────────────
def test_one_missed_read_raises_no_host_alert_and_old_values_are_still_judged(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    path = tmp_path / "hosts.json"
    rec = _record(700.0, error="no SSH contact", attempted=990.0,
                  containers=ssh_hosts.parse_containers(_container("radarr", health="unhealthy")))
    path.write_text(json.dumps({"hosts": [rec]}))
    (h,) = remote_health.assess(inv, path, now=1000.0)                    # 300 s old: stale, not unknown
    assert h["reading"] and [p["name"] for p in h["problems"]] == ["radarr"]
    assert not any(o.kind == "remote_host" and o.condition == "failing" for o in observations_from_snapshot({"other_hosts": [h]}))


def test_a_stuck_start_and_a_never_started_container_are_problems_like_locally():
    now = 1_800_000_000.0
    started = lambda secs: datetime.fromtimestamp(now - secs, tz=timezone.utc).isoformat()   # noqa: E731
    issue = lambda **kw: remote_health.container_issue({"state": "running", "restarts": 0, **kw}, now)[0]  # noqa: E731
    assert issue(health="starting", started=started(60)) is None
    assert issue(health="starting", started=started(900)) == "healthcheck still starting after 900s"
    assert issue(health="starting", started="0001-01-01T00:00:00Z") is None             # never started: no uptime to judge
    assert issue(state="created") == "created, never started"


def test_the_overview_counts_problems_by_the_alerts_rule(tmp_path):
    inv = _inventory({"name": "CASALAB", "ssh": "casalab"})
    path = _health_file(tmp_path, [_container("a", state="exited", exit=1), _container("b", restarts=4), _container("c")])
    assert remote_summary(inv, path, wall=lambda: 1000.0)["problems"] == 2


def test_investigating_an_incident_on_another_host_says_so_in_the_goal():
    import inspect
    from planet_express.application import mission_service
    src = inspect.getsource(mission_service.MissionService.investigate_incident)
    assert '("remote_container", "remote_host")' in src and "ANOTHER host" in src and "Propose nothing for this host" in src


def test_leftover_beszel_keys_never_make_a_config_invalid():
    ok, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts:\n    - {name: a, ssh: a, system_id: ptf3tn2gzpg913i}\n"
                                      "    - {name: b, link: 'http://b', system_id: ptf3tn2gzpg913i}\n")
    assert errors == []
    ok, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  hosts: []\n  local_system_id: n7n7ppta55karj9\n")
    assert errors == []


# ── rc14.10.1: a metrics-only host (`docker: false`), live SolarAssistant ────────────────────────────────────────────────────────
def test_a_metrics_only_host_is_read_for_metrics_only_and_its_failure_is_the_hosts(tmp_path):
    inv = _inventory({"name": "SOLAR", "ssh": "solar", "docker": False, "link": "https://solar"})
    calls = []

    def read(alias, name):
        calls.append(name)
        return LINUX_METRICS
    data = fc.FleetCollector(inv, tmp_path / "h.json", read=read, local_id=lambda: "LOCAL", clock=lambda: 1000.0).collect()
    rec = data["hosts"][0]
    assert calls == ["metrics"] and rec["read_at"] == 1000.0 and rec["containers"] is None and rec["docker"] is False
    assert rec["metrics"]["cpu_pct"] == pytest.approx(50.0)
    bad = fc.read_host("solar", local_id=None, read=_reader({"solar": {"metrics": ssh_hosts.RemoteReadError("no SSH contact")}}),
                       docker=False)
    assert bad == {"error": "no SSH contact", "cause": "no_contact"}


def test_a_docker_host_whose_docker_is_missing_still_fails_and_is_never_shown_as_metrics_only():
    read = _reader({"h": {"info": ssh_hosts.RemoteReadError("docker is not on that host's PATH"), "metrics": LINUX_METRICS}})
    assert fc.read_host("h", local_id=None, read=read)["error"] == "docker is not on that host's PATH"


def test_a_metrics_only_host_shows_metrics_counts_as_reading_and_raises_no_container_problems(tmp_path):
    inv = _inventory({"name": "SOLAR", "ssh": "solar", "docker": False}, {"name": "CASALAB", "ssh": "casalab"})
    path = tmp_path / "hosts.json"
    solar = {"name": "SOLAR", "ssh": "solar", "docker": False, "read_at": 990.0, "attempted_at": 990.0, "error": None,
             "engine": None, "containers": None, "metrics": ssh_hosts.parse_metrics(LINUX_METRICS)}
    path.write_text(json.dumps({"hosts": [solar, _record(990.0)]}))
    card = Fleet(inv, path, wall=lambda: 1000.0).view().cards[0]
    assert (card.liveness.state, card.docker, card.containers, card.locality_checked) == ("current", False, None, True)
    assert card.metrics.cpu_pct == pytest.approx(50.0)
    health = {h["host"]: h for h in remote_health.assess(inv, path, now=1000.0)}
    assert health["SOLAR"]["reading"] and health["SOLAR"]["problems"] == [] and health["SOLAR"]["names"] == []
    assert remote_summary(inv, path, wall=lambda: 1000.0)["unknown"] == 0
    from planet_express.application.multi_host import other_hosts_text
    assert "- SOLAR: no Docker, metrics only (CPU 50%, memory 50%, disk 25%)" in other_hosts_text(inv, path, wall=lambda: 1000.0)
    page = _hosts_page((ssh_card(HostEntry(name="SOLAR", ssh="solar", docker=False),
                                 {**solar, "read_at": 1800000000.0 - 5, "attempted_at": 1800000000.0 - 5}, 1800000000.0),)
                       ).get_data(as_text=True)
    assert "No Docker on this host" in page and "<span class=\"cell-dim\">no Docker</span>" in page and "unreadable" not in page
    silent = {**solar, "read_at": 0.0, "attempted_at": 990.0, "error": "no SSH contact"}
    path.write_text(json.dumps({"hosts": [silent]}))
    (h,) = remote_health.assess(_inventory({"name": "SOLAR", "ssh": "solar", "docker": False}), path, now=1000.0)
    assert not h["reading"]                                              # a silent appliance still alerts as not reading


def test_a_metrics_only_answer_with_no_usable_metrics_is_a_failed_read():
    for text in ("", "os FreeBSD\n", "garbage"):
        r = fc.read_host("solar", local_id=None, read=_reader({"solar": {"metrics": text}}), docker=False)
        assert "no usable values" in r["error"] and "read_at" not in r
