"""Release proof item 4:

    A session without protected-domain credentials cannot alter those domains.

An ungranted session is built the way the supervisor builds every session (grant -> environment builder, brokered tools,
launcher spec). The operator's own environment is full of protected-domain secrets (DNS, ingress, router, backup); none
may reach the session by environment, file, readable path or tool, and the one tool that could write DNS is refused."""
import json

import pytest
from supervisor_fakes import Rig, update
from test_mission_tools import setup as tool_setup

from planet_express.integrations.rpc import RpcError
from planet_express.missions.grants import build_session_env, scan_session_env
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.tools import default_registry
from release_proof.support import ROOT  # noqa: F401  (sys.path)

STACKS = "/srv/stacks"
SECRETS = {"DNS_API_KEY": "dns-admin-0123456789abcdef", "TRAEFIK_DASHBOARD_TOKEN": "traefik-tok-0123456789",
           "ROUTER_PASSWORD": "router-pw-0123456789", "RESTIC_PASSWORD": "restic-pw-0123456789"}


def operator_env():
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **SECRETS, "DISGUISED": "prefix-" + SECRETS["DNS_API_KEY"]}


def build(tmp_path, grant_credentials=()):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    stacks = tmp_path / "stacks"
    (stacks / "media").mkdir(parents=True, exist_ok=True)
    (stacks / "media" / "compose.yml").write_text("services: {}\n")
    prot = build_protected(site=["service:media/traefik"], discovery=Discovery(stacks_root=str(stacks)))
    from missions_helpers import repair_grant

    from planet_express.missions.grants import with_site_protected
    grant = with_site_protected(repair_grant(credentials=list(grant_credentials), backup={"covered": True}), prot)
    se = build_session_env(grant, mission_id="M-1", workspace=str(ws), worker_socket="/run/s/w.sock", protected=prot,
                           stacks_root=str(stacks), base_env=operator_env(), known_tools=default_registry().names(),
                           secret_values=list(SECRETS.values()),
                           secret_paths=[str(stacks / "media" / "core-secrets.yaml"), "/etc/pe/config.yaml"])
    return se, stacks


def test_no_protected_secret_reaches_an_ungranted_sessions_environment(tmp_path):
    se, _ = build(tmp_path)
    assert se.tools == () and se.env["PE_TOOLS"] == ""
    blob = json.dumps(se.env)
    assert not any(v in blob for v in SECRETS.values())
    assert not set(SECRETS) & set(se.env) and "DISGUISED" not in se.env       # by name and by value
    assert set(SECRETS) | {"DISGUISED"} <= set(se.dropped)


def test_protected_and_secret_paths_are_not_readable_and_no_secret_sits_in_a_readable_file(tmp_path):
    se, stacks = build(tmp_path)
    assert "/etc/pe/config.yaml" in se.denied and str(stacks / "media" / "core-secrets.yaml") in se.denied
    assert all(not r.startswith("/etc") for r in se.readable)
    assert scan_session_env(se, list(SECRETS.values())) == []
    (stacks / "media" / "leak.txt").write_text("DNS=" + SECRETS["DNS_API_KEY"])          # the control: a leak IS found
    hits = scan_session_env(se, list(SECRETS.values()))
    assert hits and SECRETS["DNS_API_KEY"] not in " ".join(hits)


def test_the_launched_process_gets_exactly_the_built_environment(tmp_path, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)                                         # Core's own process env holds them too
    se, _ = build(tmp_path)
    rig = Rig(tmp_path / "sup", behaviours=[{"script": [update()] * 3}])
    rig.sup.worker_env = dict(se.env)
    rig.sup.start(rig.mid)
    spawned = [s for g in rig.cont._groups.values() for s in g.spawns]
    assert spawned
    for spawn in spawned:
        assert not any(v in json.dumps(spawn) for v in SECRETS.values()), "a secret reached the launch spec"
        assert not set(SECRETS) & set(spawn["env"])
    assert not any(v in rig.dump() for v in SECRETS.values())


def test_the_dns_write_tool_is_refused_without_the_credential_and_even_with_it_until_it_is_ungated(tmp_path):
    params = {"server": "primary", "domain": "a.example", "answer": "10.0.0.2", "action": "add"}
    _ms, _m, broker, sess, http, _ = tool_setup(tmp_path, creds=())
    with pytest.raises(RpcError) as exc:
        broker.invoke(sess, "dns.rewrite", params)
    assert exc.value.code == "unauthorized" and http.calls == []
    _ms, _m, broker, sess, http, _ = tool_setup(tmp_path / "granted", creds=("tool:dns.rewrite",))
    with pytest.raises(RpcError):
        broker.invoke(sess, "dns.rewrite", params)
    assert http.calls == []                                                    # nothing ever left Core


def test_a_read_tool_the_grant_does_not_name_is_refused_so_no_protected_key_is_ever_injected(tmp_path):
    _ms, _m, broker, sess, http, _ = tool_setup(tmp_path, creds=())
    for tool, params in (("traefik.read", {}), ("arr.queue_item", {"app": "sonarr", "id": 1})):
        with pytest.raises(RpcError) as exc:
            broker.invoke(sess, tool, params)
        assert exc.value.code in ("unauthorized", "bad_params")
    assert http.calls == []


def test_a_grant_cannot_name_a_credential_that_is_not_a_registered_brokered_tool(tmp_path):
    from planet_express.missions.grants import EnvLeak
    with pytest.raises(EnvLeak):
        build(tmp_path, grant_credentials=["tool:router.admin"])
