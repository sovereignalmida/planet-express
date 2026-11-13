"""Release D of docs/designs/multi-host.md: the other hosts' traefik labels become routes in ONE generated file.

What must hold (it changes the ingress): nothing that already routes can lose traffic to a remote label (no Host already served, no
rule without a Host, nothing this Traefik does not have); names cannot collide with local routers; no label value becomes YAML;
the write is under the host-mutation lock, atomic, and checked against Traefik's API with the previous file put back on failure;
nothing is written without Traefik's API, or when nothing changed; a host that did not answer keeps its routes.
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_io import validate_config_text  # noqa: E402
from planet_express.application import remote_ingress as ri  # noqa: E402

API = "http://127.0.0.1:8079"
# Unraid's `homepage` container, as read live on 2026-11-13.
HOMEPAGE = {"traefik.enable": "true", "traefik.http.routers.app.entrypoints": "websecure",
            "traefik.http.routers.app.rule": "Host(`app.casaalmida.com`)", "traefik.http.routers.app.tls": "true"}


def _c(name, labels, ports=((3000, 3000, "0.0.0.0"),)):
    return {"name": name, "labels": labels,
            "ports": [{"container": f"{c}/tcp", "host_port": h, "host_ip": ip} for c, h, ip in ports]}


def _host(alias, *containers, error=None):
    return {"ssh": alias, "name": alias.upper(), "error": error, "containers": list(containers)}


def _traefik(foreign=("radarr4k.casalan.com",), entrypoints=("web", "websecure"), middlewares=("lan-only@file",),
             resolvers=("cloudflare",), blocked=None, names=("radarr4k",)):
    return ri.TraefikState(frozenset(foreign), blocked or {}, frozenset(names), frozenset(entrypoints), frozenset(middlewares),
                           frozenset(resolvers))


def _plan(*containers, traefik=None, alias="unraid", address="192.168.1.171"):
    return ri.plan_routes([_host(alias, *containers)], traefik or _traefik(), {alias: address})


# ── translation ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_live_homepage_labels_become_one_prefixed_route_to_the_hosts_published_port():
    plan = _plan(_c("homepage", HOMEPAGE))
    assert plan.skipped == []
    assert plan.routers == {"pe-unraid--homepage--app": {"rule": "Host(`app.casaalmida.com`)", "entryPoints": ["websecure"], "tls": {},
                                              "service": "pe-unraid--homepage--3000"}}
    assert plan.services == {"pe-unraid--homepage--3000": {"loadBalancer": {"servers": [{"url": "http://192.168.1.171:3000"}]}}}
    assert plan.routes == [{"host": "unraid", "container": "homepage", "router": "pe-unraid--homepage--app",
                            "service": "pe-unraid--homepage--3000", "hosts": ["app.casaalmida.com"], "url": "http://192.168.1.171:3000"}]


def _reason(plan):
    (s,) = plan.skipped
    return s["reason"]


@pytest.mark.parametrize("labels,reason", [
    ({**HOMEPAGE, "traefik.http.routers.app.rule": "Host(`radarr4k.casalan.com`)"}, "already served on this host: radarr4k"),
    ({**HOMEPAGE, "traefik.http.routers.app.rule": "PathPrefix(`/`)"}, "names no Host"),
    ({**HOMEPAGE, "traefik.http.routers.app.rule": "Host(`a.b`)\n  evil: 1"}, "no usable rule"),
    ({**HOMEPAGE, "traefik.http.routers.app.entrypoints": "admin"}, "entry points that do not exist here: admin"),
    ({k: v for k, v in HOMEPAGE.items() if "entrypoints" not in k}, "no entrypoints label"),
    ({**HOMEPAGE, "traefik.http.routers.app.middlewares": "auth"}, "middleware 'auth' does not exist here (as auth@file)"),
    ({**HOMEPAGE, "traefik.http.routers.app.tls.certresolver": "letsencrypt"}, "certificate resolver 'letsencrypt' is not in use"),
    ({**HOMEPAGE, "traefik.http.routers.app.tls.options": "x"}, "router keys PE does not forward: tls.options"),
    ({**HOMEPAGE, "traefik.http.services.s.loadbalancer.server.port": "8080"}, "port 8080 is not published on the host"),
    ({**HOMEPAGE, "traefik.http.services.s.loadbalancer.server.scheme": "h2c"}, "scheme 'h2c' is not forwarded"),
    ({**HOMEPAGE, "traefik.http.routers.app.service": "nope"}, "service 'nope' is not declared"),
    ({**HOMEPAGE, "traefik.http.routers.app.priority": "high"}, "priority is not a number"),
])
def test_a_route_that_could_take_traffic_or_names_something_missing_is_not_written_and_says_why(labels, reason):
    plan = _plan(_c("homepage", labels))
    assert plan.routers == {} and reason in _reason(plan)


def test_ports_must_be_published_reachable_and_unambiguous():
    assert "published on 127.0.0.1 only, not on 192.168.1.171" in _reason(_plan(_c("x", HOMEPAGE, ports=((3000, 3000, "127.0.0.1"),))))
    assert "not exactly one published TCP port" in _reason(_plan(_c("x", HOMEPAGE, ports=((80, 8080, "0.0.0.0"), (443, 8443, "")))))
    assert "not exactly one published TCP port" in _reason(_plan(_c("x", HOMEPAGE, ports=())))
    two = _plan(_c("x", {**HOMEPAGE, "traefik.http.services.s.loadbalancer.server.port": "443"},
                   ports=((80, 8080, "0.0.0.0"), (443, 8443, "0.0.0.0"))))
    assert two.services["pe-unraid--x--8443"]["loadBalancer"]["servers"] == [{"url": "http://192.168.1.171:8443"}]


def test_labels_pe_does_not_translate_are_reported_and_a_container_without_routers_is_not_given_a_default():
    plan = _plan(_c("x", {**HOMEPAGE, "traefik.http.middlewares.auth.basicauth.users": "u:hash",
                          "traefik.tcp.routers.t.rule": "HostSNI(`*`)"}))
    assert "pe-unraid--x--app" in plan.routers
    assert any("traefik.http.middlewares.auth.basicauth.users" in s["reason"] and "traefik.tcp.routers.t.rule" in s["reason"]
               for s in plan.skipped)
    bare = _plan(_c("y", {"traefik.enable": "true"}))
    assert bare.routers == {} and "PE does not invent a default rule" in _reason(bare)
    assert _plan(_c("z", {**HOMEPAGE, "traefik.enable": "false"})).skipped == []          # not enabled: not ours to mention


def test_two_remote_containers_claiming_one_host_are_both_refused():
    plan = ri.plan_routes([_host("unraid", _c("a", HOMEPAGE)), _host("casalab", _c("b", HOMEPAGE))], _traefik(),
                          {"unraid": "192.168.1.171", "casalab": "192.168.1.80"})
    assert plan.routers == {} and len(plan.skipped) == 2
    assert all("another remote container claims the same host: app.casaalmida.com" == s["reason"] for s in plan.skipped)


def test_the_same_router_name_on_two_hosts_stays_two_routes():
    other = {**HOMEPAGE, "traefik.http.routers.app.rule": "Host(`app2.casaalmida.com`)"}
    plan = ri.plan_routes([_host("unraid", _c("a", HOMEPAGE)), _host("casalab", _c("b", other))], _traefik(),
                          {"unraid": "192.168.1.171", "casalab": "192.168.1.80"})
    assert set(plan.routers) == {"pe-unraid--a--app", "pe-casalab--b--app"}


def test_no_label_value_becomes_yaml_syntax():
    tricky = {**HOMEPAGE, "traefik.http.routers.app.rule": "Host(`app.casaalmida.com`) && Path(`/x: {y} [z] # '\" &a *b |c`)"}
    plan = _plan(_c("homepage", tricky))
    assert plan.skipped == []
    text = ri.render(plan.document())
    assert yaml.safe_load(text) == plan.document()
    assert ri.render(ri.Plan().document()) is None


def test_the_hosts_address_comes_from_ssh_config_and_nothing_is_sent():
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"user casaroot\nhostname 192.168.1.80\nport 22\n", b"")
    assert ri.host_address("casalab", runner=runner) == "192.168.1.80"
    assert seen == [["ssh", "-G", "--", "casalab"]]
    bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"hostname evil;rm\n", b"")  # noqa: E731
    assert ri.host_address("x", runner=bad) is None


def test_traefik_state_counts_only_routers_in_pes_own_file_as_not_taken():
    answers = {
        "/api/http/routers": [{"name": "radarr4k@file", "rule": "Host(`radarr4k.casalan.com`)", "tls": {"certResolver": "cf"}},
                              {"name": "pe-unraid--homepage--app@file", "rule": "Host(`app.casaalmida.com`)"},
                              {"name": "pe-handwritten@file", "rule": "Host(`mine.casalan.com`)"},
                              {"name": "app@docker", "rule": "Host(`App.Casalan.com`) || Host(`b.casalan.com`)"}],
        "/api/entrypoints": [{"name": "websecure"}], "/api/http/middlewares": [{"name": "lan-only@file"}],
        "/api/http/services": [{"name": "homeunraid@file"}, {"name": "pe-unraid--homepage--3000@file"}]}
    state = ri.read_traefik(API, get=lambda url: answers[url.removeprefix(API)], owned=frozenset({"pe-unraid--homepage--app"}))
    # Only routers in PE's own file are PE's: a hand-written `pe-` router is protected like any other (Codex).
    assert state.foreign_hosts == {"radarr4k.casalan.com", "app.casalan.com", "b.casalan.com", "mine.casalan.com"}
    assert state.resolvers == {"cf"} and state.entrypoints == {"websecure"} and state.middlewares == {"lan-only@file"}
    assert state.names == {"radarr4k", "pe-handwritten", "homeunraid", "pe-unraid--homepage--3000"}


# ── the write ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class Lock:
    def __init__(self, free=True):
        self.free, self.events = free, []

    def try_begin_mutation(self, owner):
        self.events.append(("begin", owner))
        return self.free

    def end_mutation(self, owner):
        self.events.append(("end", owner))


class FakeTraefik:
    """The API before the write, and after it: what the file says, as Traefik would show it once loaded. `refuse` routers show an
    error; `stale` makes it keep showing the PREVIOUS file's routers (a reload that has not happened)."""

    def __init__(self, directory, refuse=(), down=False, stale=False):
        self.directory, self.refuse, self.down, self.stale = directory, set(refuse), down, stale
        self.loaded = None

    def _doc(self):
        f = self.directory / ri.FILE_NAME
        doc = (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}
        if self.stale:
            if self.loaded is None:
                self.loaded = doc
            return self.loaded
        return doc

    def __call__(self, url):
        if self.down:
            raise OSError("connection refused")
        path = url.removeprefix(API)
        if path == "/api/entrypoints":
            return [{"name": "websecure"}, {"name": "web"}]
        if path == "/api/http/middlewares":
            return []
        http = self._doc().get("http", {})
        if path == "/api/http/services":
            return [{"name": f"{n}@file", "loadBalancer": v["loadBalancer"]} for n, v in (http.get("services") or {}).items()]
        mine = [{**r, "name": f"{n}@file", "status": "disabled" if n in self.refuse else "enabled",
                 **({"error": ["bad"]} if n in self.refuse else {})} for n, r in (http.get("routers") or {}).items()]
        return [{"name": "radarr4k@file", "rule": "Host(`radarr4k.casalan.com`)", "status": "enabled", "entryPoints": ["websecure"]},
                {"name": "web-to-websecure@internal", "rule": "HostRegexp(`^.+$`)", "entryPoints": ["web"], "status": "enabled"},
                *mine]


def _ingress(tmp_path, *, refuse=(), down=False, free=True, stale=False):
    directory = tmp_path / "dynamic"
    directory.mkdir()
    (directory / "arr4k-lan.yml").write_text("http: {}\n")                 # the operator's file: never touched
    sent, lock = [], Lock(free)
    cfg = SimpleNamespace(directory=str(directory), traefik_api=API, enabled=True)
    clock = iter(range(0, 10_000))
    ing = ri.RemoteIngress(cfg, lock=lock, notify=sent.append, state_dir=tmp_path / "state",
                           address=lambda alias: {"unraid": "192.168.1.171"}.get(alias),
                           get=FakeTraefik(directory, refuse, down, stale), clock=lambda: next(clock), sleep=lambda s: None)
    (tmp_path / "state").mkdir()
    return ing, directory, sent, lock


INV = SimpleNamespace(hosts=[SimpleNamespace(ssh="unraid", docker=True), SimpleNamespace(ssh="solar", docker=False)])


def test_a_new_route_is_written_under_the_lock_checked_and_announced(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "written"
    doc = yaml.safe_load((directory / ri.FILE_NAME).read_text())
    assert set(doc["http"]["routers"]) == {"pe-unraid--homepage--app"}
    assert (directory / ri.FILE_NAME).read_text().startswith("# Generated by Planet Express")
    assert lock.events == [("begin", ri.OWNER), ("end", ri.OWNER)]
    assert "app.casaalmida.com → http://192.168.1.171:3000 (homepage on unraid)" in sent[0]
    assert (directory / "arr4k-lan.yml").read_text() == "http: {}\n"
    status = json.loads((tmp_path / "state" / "remote_routes.json").read_text())
    assert status["routes"][0]["router"] == "pe-unraid--homepage--app" and status["skipped"] == []
    # Again with nothing new: no lock, no write, no message.
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "unchanged"
    assert len(lock.events) == 2 and len(sent) == 1


def test_a_route_traefik_refuses_puts_the_previous_file_back(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path, refuse={"pe-unraid--homepage--app"})
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "rolled-back"
    assert not (directory / ri.FILE_NAME).exists()                         # there was none before
    assert "Traefik did not take pe-unraid--homepage--app" in sent[0] and "nothing changed" in sent[0]
    assert lock.events[-1] == ("end", ri.OWNER)


def test_nothing_is_written_without_traefiks_api_or_while_the_host_is_busy(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path, down=True)
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "no-traefik"
    assert not (directory / ri.FILE_NAME).exists() and lock.events == [] and sent == []
    (tmp_path / "b").mkdir()
    ing, directory, sent, lock = _ingress(tmp_path / "b", free=False)
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "busy"
    assert not (directory / ri.FILE_NAME).exists() and sent == []


def test_a_host_that_did_not_answer_keeps_its_routes_and_a_removed_label_removes_its_route(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    assert ing.sync({"hosts": [_host("unraid", error="no SSH contact")]}, INV) == "unchanged"
    assert "pe-unraid--homepage--app" in yaml.safe_load((directory / ri.FILE_NAME).read_text())["http"]["routers"]
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", {}))]}, INV) == "written"
    assert not (directory / ri.FILE_NAME).exists() and "− pe-unraid--homepage--app" in sent[-1]


def test_a_metrics_only_host_is_never_read_for_labels(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    assert ing.sync({"hosts": [_host("solar", _c("x", HOMEPAGE))]}, INV) == "unchanged"
    assert not (directory / ri.FILE_NAME).exists()


# ── config and page ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_ingress_is_off_by_default_needs_a_directory_and_talks_to_loopback_only():
    ok, errors = validate_config_text("stacks_root: /srv\n")
    assert errors == [] and ok.multi_host.ingress.enabled is False
    _, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  ingress: {enabled: true}\n")
    assert "needs multi_host.ingress.directory" in errors[0]["msg"]
    for api in ("http://192.168.1.94:8079", "https://127.0.0.1:8079", "http://127.0.0.1"):
        _, errors = validate_config_text(f"stacks_root: /srv\nmulti_host:\n  ingress: {{traefik_api: '{api}'}}\n")
        assert errors, api
    _, errors = validate_config_text("stacks_root: /srv\nmulti_host:\n  ingress: {enabled: true, directory: relative/dir}\n")
    assert "absolute path" in errors[0]["msg"]


def test_the_hosts_page_shows_each_hosts_routes_and_what_was_not_routed(tmp_path):
    from config_schema import HostEntry, MultiHostConfig
    from planet_express.application.multi_host import Fleet
    from test_fleet_ssh import _hosts_page, _record
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": [{**_record(990.0), "ssh": "unraid", "name": "UNRAID"}]}))
    (tmp_path / "remote_routes.json").write_text(json.dumps({
        "routes": [{"host": "unraid", "container": "homepage", "router": "pe-unraid--homepage--app", "hosts": ["app.casaalmida.com"],
                    "url": "http://192.168.1.171:3000"}],
        "skipped": [{"host": "unraid", "container": "airconnect", "router": None, "reason": "no traefik.http.routers labels"}]}))
    inv = MultiHostConfig(hosts=[HostEntry(name="UNRAID", ssh="unraid")], ingress={"enabled": True, "directory": "/x"})
    card = Fleet(inv, tmp_path / "hosts.json", wall=lambda: 1000.0).view().cards[0]
    assert card.routes[0]["url"] == "http://192.168.1.171:3000" and card.unrouted[0]["container"] == "airconnect"
    page = _hosts_page((card,)).get_data(as_text=True)
    assert "app.casaalmida.com → http://192.168.1.171:3000" in page and "NOT ROUTED" in page and "airconnect" in page
    off = MultiHostConfig(hosts=[HostEntry(name="UNRAID", ssh="unraid")])
    assert Fleet(off, tmp_path / "hosts.json", wall=lambda: 1000.0).view().cards[0].routes == ()



# ── Codex, release D ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rule", ["Host(`new.example`) || PathPrefix(`/`)", "Host(`a.b`) || HostRegexp(`.+`)",
                                  "!Host(`radarr4k.casalan.com`) && Host(`a.b`)", "Host(`a.b`) || Header(`X`, `y`)"])
def test_a_rule_that_can_match_hosts_it_does_not_name_is_refused(rule):
    assert "PE forwards only rules that match the hosts they name" in _reason(
        _plan(_c("homepage", {**HOMEPAGE, "traefik.http.routers.app.rule": rule})))


@pytest.mark.parametrize("rule", ["Host(`a.b`)", "Host(`a.b`) || Host(`c.d`)", "(Host(`a.b`) || Host(`c.d`)) && PathPrefix(`/api`)",
                                  "Host(`a.b`) && Path(`/x`)"])
def test_rules_confined_to_their_named_hosts_are_forwarded(rule):
    assert _plan(_c("homepage", {**HOMEPAGE, "traefik.http.routers.app.rule": rule})).skipped == []


def test_a_route_traefik_still_shows_in_its_old_form_is_not_taken_as_applied(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path, stale=True)
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV) == "rolled-back"
    assert not (directory / ri.FILE_NAME).exists()


def test_a_kept_route_reserves_its_host_and_two_routers_with_one_generated_name_are_refused(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    inv = SimpleNamespace(hosts=[SimpleNamespace(ssh="unraid", docker=True), SimpleNamespace(ssh="casalab", docker=True)])
    ing._address = lambda a: {"unraid": "192.168.1.171", "casalab": "192.168.1.80"}[a]
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, inv) == "written"
    out = ing.sync({"hosts": [_host("unraid", error="no SSH contact"), _host("casalab", _c("other", HOMEPAGE))]}, inv)
    assert out == "unchanged"
    status = json.loads((tmp_path / "state" / "remote_routes.json").read_text())
    assert status["routes"][0]["kept"] is True
    assert "served by a route PE is keeping (its host or container is not up): app.casaalmida.com" in status["skipped"][0]["reason"]
    dup = ri.plan_routes([_host("unraid", _c("a-b", {**HOMEPAGE, "traefik.http.routers.c.rule": "Host(`x.y`)",
                                                       "traefik.http.routers.c.entrypoints": "websecure"}))], _traefik(),
                         {"unraid": "1.2.3.4"})
    assert len(dup.routers) == 2                                     # app and c: two names, both written


def test_the_hosts_page_status_changes_only_when_a_write_succeeds(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path, refuse={"pe-unraid--homepage--app"})
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    assert not (tmp_path / "state" / "remote_routes.json").exists()


def test_a_changed_route_is_announced(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE, ports=((3000, 3001, "0.0.0.0"),)))]}, INV) == "written"
    assert "(changed)" in sent[-1] or "+ app.casaalmida.com → http://192.168.1.171:3001" in sent[-1]



# ── second review, release D (Codex 5, /code-review 10) ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rule,ok", [
    ("Host(`a`)", True), ("(Host(`a`) || Host(`b`)) && !(PathPrefix(`/x`) || PathPrefix(`/y`))", True),
    ("Host(`a`) && PathPrefix(`/api`)", True), ("Host(`a`) && PathPrefix(`/x`) || PathPrefix(`/`)", False),
    ("HostRegexp(`^.+$`)", False), ("PathPrefix(`/`)", False), ("Host(`a`, `b`)", False), ("Host(\"a\")", False),
    ("Host(`a`) || PathPrefix(`/`)", False)])
def test_an_existing_rule_counts_as_readable_only_when_confined_to_its_hosts(rule, ok):
    assert ri.confined(rule) is ok


def test_an_entry_point_with_an_unreadable_existing_rule_takes_no_remote_route():
    plan = _plan(_c("homepage", {**HOMEPAGE, "traefik.http.routers.app.entrypoints": "web"}),
                 traefik=_traefik(blocked={"web": "web-to-websecure@internal"}))
    assert "an existing route on web (web-to-websecure@internal) matches hosts PE cannot read" in _reason(plan)
    assert _plan(_c("homepage", HOMEPAGE), traefik=_traefik(blocked={"web": "x@internal"})).skipped == []      # websecure is fine


def test_a_generated_name_already_used_in_the_file_provider_is_refused():
    plan = _plan(_c("homepage", HOMEPAGE), traefik=_traefik(names=("pe-unraid--homepage--app",)))
    assert "is already in use" in _reason(plan) and plan.routers == {}


def test_only_tcp_ports_and_binds_reachable_at_the_hosts_address_are_used():
    udp = {"name": "x", "labels": HOMEPAGE, "ports": [{"container": "3000/udp", "host_port": 3000, "host_ip": "0.0.0.0"}]}
    assert "not exactly one published TCP port" in _reason(_plan(udp))
    other_nic = _plan(_c("x", HOMEPAGE, ports=((3000, 3000, "10.0.0.5"),)))
    assert "published on 10.0.0.5 only, not on 192.168.1.171" in _reason(other_nic)
    same_nic = _plan(_c("x", HOMEPAGE, ports=((3000, 3000, "192.168.1.171"),)))
    assert same_nic.skipped == []


def test_a_file_traefik_did_not_take_is_not_retried_every_minute(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path, refuse={"pe-unraid--homepage--app"})
    data = {"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}
    assert ing.sync(data, INV) == "rolled-back"
    assert ing.sync(data, INV) == "refused-before" and len(sent) == 1 and len(lock.events) == 2
    changed = {"hosts": [_host("unraid", _c("homepage", HOMEPAGE, ports=((3000, 3001, "0.0.0.0"),)))]}
    assert ing.sync(changed, INV) == "rolled-back"                     # a new file is tried


def test_a_change_traefik_has_not_loaded_is_caught_even_when_only_a_middleware_changes(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    ing._get = FakeTraefik(directory, stale=True)                       # Traefik keeps showing what it loaded before
    ing._get("/api/http/routers")
    with_mw = {**HOMEPAGE, "traefik.http.routers.app.middlewares": "lan-only@file"}
    ing._get.loaded["http"]["routers"]["pe-unraid--homepage--app"].pop("middlewares", None)
    real = ing._get

    def get(url):                                                      # lan-only exists here, so the plan accepts it
        return [{"name": "lan-only@file"}] if url.endswith("/api/http/middlewares") else real(url)
    ing._get = get
    assert ing.sync({"hosts": [_host("unraid", _c("homepage", with_mw))]}, INV) == "rolled-back"
    assert "middlewares" not in yaml.safe_load((directory / ri.FILE_NAME).read_text())["http"]["routers"]["pe-unraid--homepage--app"]


def test_a_stopped_container_keeps_its_route_instead_of_flapping(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    stopped = {**_c("homepage", HOMEPAGE, ports=()), "state": "restarting"}
    assert ing.sync({"hosts": [_host("unraid", stopped)]}, INV) == "unchanged"
    assert len(sent) == 1
    gone = {"hosts": [_host("unraid")]}                                # removed from the host: its route goes
    assert ing.sync(gone, INV) == "written" and "− pe-unraid--homepage--app" in sent[-1]


def test_which_host_owns_a_route_is_pes_record_not_a_name_prefix(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    inv = SimpleNamespace(hosts=[SimpleNamespace(ssh="nas", docker=True), SimpleNamespace(ssh="nas--b", docker=True)])
    ing._address = lambda a: {"nas": "10.0.0.1", "nas--b": "10.0.0.2"}[a]
    ing.sync({"hosts": [_host("nas--b", _c("homepage", HOMEPAGE)), _host("nas")]}, inv)
    moved = {"hosts": [_host("nas--b", _c("homepage", HOMEPAGE, ports=((3000, 3005, "0.0.0.0"),))),
                       _host("nas", error="no SSH contact")]}
    assert ing.sync(moved, inv) == "written"                          # nas being down does not freeze nas--b's route
    assert "10.0.0.2:3005" in (directory / ri.FILE_NAME).read_text()


def test_turning_ingress_off_removes_pes_file_under_the_lock_and_says_so(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    assert ing.retire() == "removed"
    assert not (directory / ri.FILE_NAME).exists() and not (tmp_path / "state" / ri.STATUS_NAME).exists()
    assert (directory / "arr4k-lan.yml").exists() and "removed pe-unraid--homepage--app" in sent[-1]
    assert lock.events[-2:] == [("begin", ri.OWNER), ("end", ri.OWNER)]
    assert ing.retire() == "absent"


def test_every_page_of_traefiks_api_is_read(monkeypatch):
    pages = {"1": ([{"name": "a"}], "2"), "2": ([{"name": "b"}], "3"), "3": ([{"name": "c"}], "")}

    class Resp:
        def __init__(self, page):
            self.body, self.nxt = pages[page]
            self.headers = {"X-Next-Page": self.nxt}

        def read(self, n):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = []

    def urlopen(url, timeout):
        seen.append(url)
        return Resp(url.rsplit("page=", 1)[1])
    monkeypatch.setattr(ri.urllib.request, "urlopen", urlopen)
    assert [r["name"] for r in ri._get_json(API + "/api/http/routers")] == ["a", "b", "c"]
    assert all("per_page=100" in u for u in seen)


def test_the_overview_line_does_not_say_of_these_when_this_hosts_count_is_not_shown():
    import dashboard_data
    remote = {"hosts": 3, "unknown": 0, "containers": 15, "problems": 0, "cells": []}
    assert dashboard_data.remote_hosts_line(remote, summed=False) == "15 on 3 other hosts"



# ── Codex, third pass ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_removing_the_file_is_retried_while_the_host_is_busy(tmp_path):
    ing, directory, sent, lock = _ingress(tmp_path)
    ing.sync({"hosts": [_host("unraid", _c("homepage", HOMEPAGE))]}, INV)
    answers = iter([False, False, True])
    lock.try_begin_mutation = lambda owner: next(answers)
    assert ing.retire_until_done(retry_s=0) == "removed" and not (directory / ri.FILE_NAME).exists()


def test_the_retire_runs_when_ingress_is_off_or_no_docker_host_is_left():
    import inspect

    import casa_farnsworth
    src = inspect.getsource(casa_farnsworth)
    assert "ingress_runs = config.MULTI_HOST.ingress.enabled and any(e.ssh and e.docker" in src
    assert "if config.MULTI_HOST.ingress.directory and not ingress_runs:" in src and "retire_until_done" in src


def test_an_ipv6_host_address_is_bracketed_in_the_backend_url():
    plan = _plan(_c("homepage", HOMEPAGE), address="2001:db8::1")
    assert plan.services["pe-unraid--homepage--3000"]["loadBalancer"]["servers"] == [{"url": "http://[2001:db8::1]:3000"}]



def test_the_last_page_pointing_back_to_page_one_ends_the_read(monkeypatch):
    pages = {"1": ([{"name": "a"}], "2"), "2": ([{"name": "b"}], "1")}

    class Resp:
        def __init__(self, page):
            self.body, nxt = pages[page]
            self.headers = {"X-Next-Page": nxt}

        def read(self, n):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ri.urllib.request, "urlopen", lambda url, timeout: Resp(url.rsplit("page=", 1)[1]))
    assert [r["name"] for r in ri._get_json(API + "/api/http/routers")] == ["a", "b"]
