"""Brokered tools (contract 8): the key lives in Core, the session names a tool, the output is redacted and bounded."""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import advance_to, make_store, new_mission

from planet_express.integrations.rpc import RpcError
from planet_express.missions.grants import (
    Inventory,
    build_session_env,
    scan_session_env,
)
from planet_express.missions.models import Grant
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.store import MissionStore
from planet_express.missions.tools import (
    Tool,
    ToolBroker,
    ToolRegistry,
    ToolSite,
    default_registry,
)
from planet_express.missions.worker_rpc import WORKER_METHODS, build_worker_handlers

KEY = "arr-api-key-0123456789abcdef"
DNS_KEY = "dns-secret-0123456789abcdef"
SECRETS = {"arr-api": KEY, "dns-admin": DNS_KEY, "traefik-api": "Basic dXNlcjpwdw=="}
STACKS = "/srv/stacks"


@dataclass
class Sess:
    id: str
    mission_id: str


class Http:
    def __init__(self):
        self.calls = []
        self.body = b'{"id": 7, "title": "Phantom"}'
        self.status = 200

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.status, self.body


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return 0, "", ""


def setup(tmp_path, creds=("tool:arr.queue_item",), status="investigating", **grant_over):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    prot = build_protected(site=["service:media/traefik"], discovery=Discovery(stacks_root=STACKS))
    body = {"tier": "propose", "scope": {"stacks": ["media"], "hosts": ["local"]}, "credentials": list(creds),
            "protected": {"resources": list(prot.resources)}}
    body.update(grant_over)
    m = new_mission(ms, Grant.model_validate(body))
    advance_to(ms, m.id, "queued", "investigating")
    http, runner = Http(), Runner()
    sites = {"arr.queue_item": ToolSite(urls={"sonarr": "http://sonarr.lan:8989"}),
             "traefik.read": ToolSite(urls={"default": "http://traefik.lan:8080"}),
             "app.health_get": ToolSite(urls={"media/qbit": "http://qbit.lan:8080", "media/traefik": "http://t.lan"}),
             "dns.rewrite": ToolSite(urls={"primary": "http://adguard.lan:3000"})}
    broker = ToolBroker(default_registry(), missions=ms, secrets=SECRETS.get, http=http, runner=runner, sites=sites,
                        stacks_root=str(tmp_path / "stacks"), protected=prot.resources)
    (tmp_path / "stacks" / "media").mkdir(parents=True)
    (tmp_path / "stacks" / "media" / "docker-compose.yml").write_text("services: {}\n")
    return ms, m, broker, Sess("S-1", m.id), http, runner


def events(ms, mission_id):
    return [e for e in ms.events(mission_id) if e.kind == "tool"]


def test_registry_shape():
    reg = default_registry()
    assert set(reg.names()) == {"traefik.read", "compose.dry_run", "app.health_get", "arr.queue_item", "dns.rewrite"}
    assert reg.get("dns.rewrite").risk == "write" and reg.get("arr.queue_item").risk == "read"
    assert reg.get("arr.queue_item").secret_ref == "arr-api"
    with pytest.raises(ValueError):
        reg.register(reg.get("arr.queue_item"))
    assert isinstance(reg.get("arr.queue_item"), Tool) and isinstance(reg, ToolRegistry)


def test_a_brokered_read_works_and_the_key_is_injected_but_never_returned(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    http.body = json.dumps({"id": 7, "echo": f"X-Api-Key: {KEY}", "note": f"token={KEY}"}).encode()
    out = broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 7})
    assert out["ok"] is True and "7" in out["output"]
    assert KEY not in json.dumps(out)
    method, url, headers = http.calls[0]
    assert (method, url) == ("GET", "http://sonarr.lan:8989/api/v3/queue/7")
    assert headers["X-Api-Key"] == KEY                       # Core injected it
    for e in ms.events(m.id):
        assert KEY not in e.body_json


def test_every_call_is_recorded_without_the_key(tmp_path):
    ms, m, broker, sess, *_ = setup(tmp_path)
    broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 7})
    (e,) = events(ms, m.id)
    body = json.loads(e.body_json)
    assert body["event"] == "tool.invoke" and body["tool"] == "arr.queue_item" and body["ok"] is True
    assert body["params"] == {"app": "sonarr", "id": 7} and body["bytes"] > 0


def test_a_tool_the_grant_does_not_name_is_refused_and_recorded(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, creds=())
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 7})
    assert exc.value.code == "unauthorized" and http.calls == []
    assert json.loads(events(ms, m.id)[0].body_json)["event"] == "tool.refused"


def test_unknown_tool_and_bad_params_are_refused(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    with pytest.raises(RpcError):
        broker.invoke(sess, "nope", {})
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 7, "extra": 1})
    assert exc.value.code == "bad_params"
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "evil", "id": 7})
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": "7/../../x"})
    assert http.calls == []


def test_scope_fields_are_checked_against_the_grant(tmp_path):
    ms, m, broker, sess, http, runner = setup(tmp_path, creds=("tool:compose.dry_run", "tool:app.health_get"))
    assert broker.invoke(sess, "compose.dry_run", {"stack": "media"})["ok"]
    assert runner.calls[0][:2] == ["docker", "compose"] and "--quiet" in runner.calls[0]
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "compose.dry_run", {"stack": "photos"})
    assert exc.value.code == "unauthorized"
    assert broker.invoke(sess, "app.health_get", {"service": "media/qbit"})["ok"]
    with pytest.raises(RpcError):
        broker.invoke(sess, "app.health_get", {"service": "media/traefik"})       # protected wins
    with pytest.raises(RpcError):
        broker.invoke(sess, "app.health_get", {"service": "other/db"})
    with pytest.raises(RpcError):
        broker.invoke(sess, "compose.dry_run", {"stack": "../etc"})


def test_write_tools_stay_gated(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, creds=("tool:dns.rewrite",))
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "dns.rewrite", {"server": "primary", "domain": "a.example", "answer": "10.0.0.2",
                                            "action": "add"})
    assert exc.value.code == "unauthorized" and http.calls == []
    body = json.loads(events(ms, m.id)[0].body_json)
    assert body["event"] == "tool.refused" and "gated" in body["reason"]


def test_output_is_redacted_and_bounded(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    http.body = (b"password=hunter2\n" + b"x" * 200_000)
    out = broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert "hunter2" not in out["output"] and out["truncated"] is True and len(out["output"]) <= 32 * 1024


def test_a_failing_application_is_reported_without_detail_that_could_leak(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    http.status = 500
    http.body = f"boom {KEY}".encode()
    out = broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert out["ok"] is False and out["status"] == 500 and KEY not in json.dumps(out)


def test_a_missing_secret_fails_closed(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    broker.secrets = lambda ref: None
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert http.calls == []


def test_the_tool_call_budget_is_enforced(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, budgets={"tool_calls": 2})
    broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 2})
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 3})
    assert exc.value.code == "unauthorized" and len(http.calls) == 2


def test_a_paused_or_revoked_mission_has_no_endpoints(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    broker.revoke(m.id)
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    broker.restore(m.id)
    assert broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})["ok"]
    advance_to(ms, m.id, "awaiting_decision")
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})         # not investigating or executing


def test_a_session_of_another_mission_cannot_use_this_missions_grant(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    with pytest.raises(RpcError):
        broker.invoke(Sess("S-9", "M-404"), "arr.queue_item", {"app": "sonarr", "id": 1})


def test_tool_invoke_is_a_worker_method_and_the_handler_passes_the_session():
    assert "tool.invoke" in WORKER_METHODS

    class Backend:
        def tool_invoke(self, session, name, params):
            return {"called": name, "params": params, "session": session}

    handlers = build_worker_handlers(Backend())
    assert handlers["tool.invoke"]({"name": "arr.queue_item", "params": {"id": 1}}, "S")["session"] == "S"
    with pytest.raises(RpcError):
        handlers["tool.invoke"]({"name": "Bad Name", "params": {}}, "S")
    with pytest.raises(RpcError):
        handlers["tool.invoke"]({"name": "x", "params": {"k": "v" * 40000}}, "S")
    assert not {"approval.decide", "execution.run", "checkpoint.restore", "config.set"} & set(handlers)


def test_no_secret_is_in_the_session_env_cwd_readable_files_or_broker_responses(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, creds=("tool:arr.queue_item",))
    stacks = tmp_path / "stacks"
    (stacks / "media" / "notes.txt").write_text("nothing here\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    grant = m.grant()
    prot = build_protected(site=[], discovery=Discovery(stacks_root=str(stacks)))
    base = {"PATH": "/bin", **{f"{k.upper().replace('-', '_')}_KEY": v for k, v in SECRETS.items()}}
    se = build_session_env(grant, mission_id=m.id, workspace=str(ws), worker_socket="/run/s.sock", protected=prot,
                           stacks_root=str(stacks), base_env=base, known_tools=default_registry().names(),
                           secret_values=list(SECRETS.values()))
    out = broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert scan_session_env(se, list(SECRETS.values())) == []
    assert not any(v in json.dumps(out) for v in SECRETS.values())
    assert se.tools == ("arr.queue_item",) and "arr-api" not in json.dumps(dict(se.env))
    assert Inventory(stacks=("media",)).stacks == ("media",)


# ── review round 2: SQL accounting, refusal cap ─────────────────────────────────────────────────────

def test_accounting_does_not_parse_the_event_log(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    ms.events = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the broker read the whole event log"))
    assert broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 2})["ok"]
    usage = broker._usage(m.id)
    assert usage["invoke"] == 2 and usage["bytes"] > 0


def test_refusals_are_capped_per_session_then_the_session_is_stopped_and_events_stop_growing(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, creds=())
    hits = []
    broker.on_refusal_cap = hits.append
    for _ in range(broker.refusal_cap + 15):
        with pytest.raises(RpcError):
            broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 7})
    refused = [e for e in events(ms, m.id) if json.loads(e.body_json)["event"] == "tool.refused"]
    assert len(refused) == broker.refusal_cap                    # growth is bounded, not one row per hostile call
    assert len(hits) == 1 and hits[0].id == sess.id
    assert any(json.loads(e.body_json)["event"] == "tool.refusal_cap" for e in events(ms, m.id))


def test_after_the_cap_even_a_valid_call_is_refused_for_that_session_only(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    broker.on_refusal_cap = lambda s: None
    for _ in range(broker.refusal_cap):
        with pytest.raises(RpcError):
            broker.invoke(sess, "nope", {})
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert http.calls == []
    other = Sess("S-2", m.id)
    assert broker.invoke(other, "arr.queue_item", {"app": "sonarr", "id": 1})["ok"]


def test_a_failing_cap_hook_does_not_unblock_the_session(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)

    def boom(session):
        raise RuntimeError("cannot freeze")
    broker.on_refusal_cap = boom
    for _ in range(broker.refusal_cap + 2):
        with pytest.raises(RpcError):
            broker.invoke(sess, "nope", {})
    with pytest.raises(RpcError):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    assert http.calls == []


def test_the_supervisor_freezes_a_session_that_hits_the_refusal_cap(tmp_path):
    from supervisor_fakes import Rig, update

    from planet_express.missions.sessions import SessionRow
    from planet_express.missions.tools import ToolBroker, default_registry
    rig = Rig(tmp_path, tier="propose", behaviours=[{"script": [update("running")] * 50}])
    rig.sup.tool_broker = ToolBroker(default_registry(), missions=rig.ms, secrets=lambda r: None, sites={},
                                     stacks_root=str(tmp_path))
    rig.sup.tool_broker.on_refusal_cap = rig.sup.freeze_for_tool_abuse
    rig.sup.start(rig.mid)
    row = rig.ms.live_session(rig.mid)
    session = SessionRow(id=row["id"], mission_id=rig.mid, kind=row["kind"], cgroup_path=row["cgroup_path"],
                         leader_pid=row["leader_pid"], session_uid=row["session_uid"], issued_at=row["issued_at"],
                         expires_at=row["expires_at"], fences={})
    group = rig.cont._groups[row["id"]]
    for _ in range(rig.sup.tool_broker.refusal_cap):
        with pytest.raises(RpcError):
            rig.sup.tool_invoke(session, "nope", {})
    assert group.frozen is True
    assert any("tool_abuse" in e.body_json for e in rig.ms.events(rig.mid))
