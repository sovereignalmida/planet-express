"""S9b: the repair rehearsal's agent reaches Docker only through the real per-session proxy. Unit tests for the new wiring
(authenticator, socket provider, cleanup, proxy summary); nothing here touches Docker or needs root."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("rehearse_repair_s9b", ROOT / "scripts/rehearse_repair.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearse_repair_s9b"] = module
    spec.loader.exec_module(module)
    return module


def test_the_record_says_the_proxy_is_exercised_and_what_still_is_not(mod):
    assert not any("per-session Docker proxy socket" in item for item in mod.NOT_EXERCISED)
    assert any("cgroup launcher" in item for item in mod.NOT_EXERCISED)


def test_the_agent_accepts_only_a_proxy_socket_as_its_docker_host(mod):
    ok = "unix:///tmp/pe-rehearsal-x/sock/S-1/proxy.sock"
    assert mod.proxy_docker_host({"DOCKER_HOST": ok}) == ok
    for bad in ({}, {"DOCKER_HOST": ""}, {"DOCKER_HOST": "unix:///var/run/docker.sock"},
                {"DOCKER_HOST": "tcp://127.0.0.1:2375"}, {"DOCKER_HOST": "unix:///tmp/proxy.sock/../docker.sock"},
                {"DOCKER_HOST": "unix://relative/proxy.sock"}):
        assert mod.proxy_docker_host(bad) is None


def test_the_agent_refuses_to_run_against_the_host_socket(mod, tmp_path, monkeypatch):
    (tmp_path / mod.MARKER).write_text("x")
    ns = mod.parse_args(["agent", "--agent-scenario", "namespace", "--stack-dir", str(tmp_path)])
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert mod.agent_main(ns) == 2
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert mod.agent_main(ns) == 2


def test_the_runtime_env_never_hands_the_agent_a_docker_host_of_its_own(mod):
    assert "DOCKER_HOST" not in mod.AGENT_ENV_ALLOW and "PATH" in mod.AGENT_ENV_ALLOW


def test_only_renamed_leftovers_of_the_rehearsal_project_are_picked_for_removal(mod):
    rows = ["aaa111 c2fab46c7829_pe-rehearsal-gateway-1", "bbb222 pe-rehearsal-gateway-1", "ccc333 0123456789ab_media-web-1",
            "ddd444 0123456789ab_pe-rehearsal-web-1", "eee555 pe-rehearsal-web-1_old"]
    assert mod.leftover_ids(rows) == ["aaa111", "ddd444"]


def test_cleanup_removes_leftovers_by_id_and_nothing_else(mod):
    calls = []

    def run(argv, timeout=120):
        calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return 0, "aaa111 c2fab46c7829_pe-rehearsal-gateway-1\nbbb222 pe-rehearsal-web-1\n"
        return 0, ""
    assert mod.remove_leftovers(run) == ["aaa111"]
    assert ["docker", "rm", "-f", "aaa111"] in calls
    assert all(c[:2] != ["docker", "rm"] or c[-1] == "aaa111" for c in calls)


def test_teardown_checks_networks_as_well_as_containers(mod):
    assert mod.networks_left(lambda argv, timeout=120: (0, "net1\n")) == ["net1"]
    assert mod.networks_left(lambda argv, timeout=120: (0, "")) == []


def event(seq, body):
    return type("E", (), {"seq": seq, "body_json": json.dumps(body)})()


def test_the_proxy_summary_counts_writes_creates_and_names_every_refusal(mod):
    events = [event(1, {"event": "docker.write", "route": "container.create"}),
              event(2, {"event": "docker.created", "created_id": "a" * 64}),
              event(3, {"event": "docker.refused", "reason": "not in scope", "route": "network.create", "method": "POST",
                        "path": "/v1.47/networks/create"}),
              event(4, {"event": "docker.refused", "further_refusals_not_recorded": True}),
              event(5, {"event": "note"})]
    summary = mod.proxy_summary(events)
    assert summary["writes"] == 1 and summary["created"] == 1
    assert summary["refused"] == [{"reason": "not in scope", "route": "network.create", "method": "POST",
                                   "path": "/v1.47/networks/create"}]
    assert summary["further_refusals_not_recorded"] is True


def test_a_scenario_that_never_touched_the_proxy_is_not_a_pass(mod):
    assert mod.went_through_proxy({"writes": 3, "created": 2}) is True
    assert mod.went_through_proxy({"writes": 0, "created": 0}) is False


class Contained:
    """A containment that only knows which pids belong to which session."""

    def __init__(self):
        self.members = {}

    def contains(self, session_id, pid, getpgid=None):
        return pid in self.members.get(session_id, ())


@pytest.fixture
def session(tmp_path):
    from missions_helpers import (
        advance_to,
        make_store,
        new_mission,
        repair_grant,
        token,
    )

    from planet_express.integrations.rpc import PeerCred
    from planet_express.missions.approvals import record_approval
    from planet_express.missions.store import MissionStore
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, repair_grant())
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    sid = ms.start_session(m.id, kind="local_cgroup", token_hash=token(), session_ttl_s=600, lease_ttl_s=60,
                           cgroup_path="/rehearsal/S", uid_range=(61000, 61010))
    return store, ms, sid, PeerCred


def test_the_authenticator_knows_only_the_agents_own_processes(mod, session):
    store, _ms, sid, PeerCred = session
    contained = Contained()
    contained.members[sid] = {4242}
    auth = mod.RehearsalAuthenticator(store, contained)
    row = auth.by_id(PeerCred(4242, 0, 0), sid)
    assert row is not None and row.id == sid and row.fences
    assert auth.by_id(PeerCred(9999, 0, 0), sid) is None                       # a process outside the session
    assert auth.by_id(PeerCred(4242, 0, 0), "S-other") is None                  # another session id
    assert auth(PeerCred(4242, 0, 0), {"session_token": "x" * 64}) is None      # tokens are not accepted here


def test_a_revoked_session_is_not_recognised(mod, session):
    store, ms, sid, PeerCred = session
    contained = Contained()
    contained.members[sid] = {4242}
    ms.revoke_session(sid)
    assert mod.RehearsalAuthenticator(store, contained).by_id(PeerCred(4242, 0, 0), sid) is None


class FakeProxy:
    def __init__(self, bound=True):
        self.bound, self.stopped, self.started = bound, 0, []

    def start(self, cs):
        self.started.append(cs.session_id)
        return (lambda: setattr(self, "stopped", self.stopped + 1)) if self.bound else None

    def env(self, cs):
        return {"DOCKER_HOST": f"unix://x/{cs.session_id}/proxy.sock"}


def test_the_socket_provider_makes_the_directory_starts_the_proxy_and_cleans_up(mod, tmp_path):
    proxy = FakeProxy()
    sockets = mod.ProxySockets(str(tmp_path), proxy)
    cs = type("CS", (), {"session_id": "S-1"})()
    stop = sockets.start(cs)
    assert (tmp_path / "S-1").is_dir() and oct((tmp_path / "S-1").stat().st_mode & 0o777) == "0o700"
    assert proxy.started == ["S-1"] and sockets.env(cs)["DOCKER_HOST"].endswith("/S-1/proxy.sock")
    stop()
    assert proxy.stopped == 1 and not (tmp_path / "S-1").exists()


def test_a_session_with_no_proxy_socket_fails_loudly_rather_than_falling_back_to_the_host(mod, tmp_path):
    sockets = mod.ProxySockets(str(tmp_path), FakeProxy(bound=False))
    with pytest.raises(RuntimeError, match="proxy"):
        sockets.start(type("CS", (), {"session_id": "S-2"})())
    assert not (tmp_path / "S-2").exists()


def test_a_socket_path_too_long_for_a_unix_socket_is_refused_up_front(mod, tmp_path):
    long_root = str(tmp_path / ("d" * 120))
    with pytest.raises(RuntimeError, match="too long"):
        mod.ProxySockets(long_root, FakeProxy()).start(type("CS", (), {"session_id": "S-3"})())


def test_the_process_containment_recognises_members_by_process_group(mod):
    c = mod.ProcessContainment()
    c._procs["S"] = type("P", (), {"pid": 100})()
    assert c.contains("S", 555, getpgid=lambda pid: 100) is True
    assert c.contains("S", 555, getpgid=lambda pid: 7) is False
    assert c.contains("nope", 555, getpgid=lambda pid: 100) is False

    def gone(pid):
        raise ProcessLookupError
    assert c.contains("S", 555, getpgid=gone) is False
