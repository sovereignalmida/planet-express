"""What the Docker proxy refuses and accepts (contract 6.1), at the policy level: every refusal class has a test."""
import json
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
from dockerproxy_fakes import (
    ID_A,
    HOST,
    ID_B,
    FakeInspector,
    create_body,
    labels,
    make_scope,
)

from planet_express.missions.dockerproxy.httpwire import (
    Head,
    Refusal,
    normalise_target,
)
from planet_express.missions.dockerproxy.policy import (
    ROUTES,
    Policy,
    redact_inspect,
)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "stacks" / "media" / "config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "elsewhere").mkdir()
    return tmp_path


@pytest.fixture
def policy(root):
    return Policy(make_scope(str(root)), FakeInspector())


def route(name):
    return next(r for r in ROUTES if r.name == name)


def plan_for(policy, name, ident=None, body=None, query=None, method=None, raw_length=0):
    r = route(name)
    return policy.plan(r, method or r.methods[0], {"id": ident} if ident else {}, policy.clean_query(r, query or {}), body, raw_length)


def create(policy, body=None, **query):
    return plan_for(policy, "container.create", body=body if body is not None else create_body(), query=query)


def host(**kw):
    return create_body(HostConfig={"RestartPolicy": {"Name": "no"}, **kw})


def head(method, target, **headers):
    path, segments, query, version = normalise_target(target)
    return Head(method, path, segments, query, {k.lower(): v for k, v in headers.items()}, version, b"")


# ── the happy path ───────────────────────────────────────────────────────────────────────────────────

def test_a_plain_in_scope_create_is_accepted_and_rebuilt_from_the_validated_fields(policy):
    plan = create(policy, create_body(Env=["TOKEN=s3cret-value"]), name="qbittorrent2")
    out = json.loads(plan.body)
    assert out["Image"] and out["Labels"]["com.docker.compose.service"] == "qbittorrent"
    assert plan.targets == ["host:local/service:media/qbittorrent"]
    assert "s3cret" not in json.dumps(plan.summary) and plan.summary["env_names"] == ["TOKEN"]      # names only, never values
    assert plan.summary["cmd_argc"] == 1 and "run" not in json.dumps(plan.summary)


def test_allowed_binds_volumes_and_caps_pass(policy, root):
    bind = f"{root}/stacks/media/config:/config:rw"
    data = f"{root}/data:/data:ro"
    plan = create(policy, host(Binds=[bind, data, "media_cfg:/cfg"], CapAdd=["NET_BIND_SERVICE", "cap_chown"],
                               NetworkMode="media_default", Mounts=[{"Type": "volume", "Source": "media_cfg", "Target": "/c2"},
                                                                    {"Type": "bind", "Source": f"{root}/data", "Target": "/d2",
                                                                     "ReadOnly": True}],
                               SecurityOpt=["no-new-privileges"], GroupAdd=["1000"], PortBindings={"8080/tcp": [{"HostPort": "8080"}]}))
    host_cfg = json.loads(plan.body)["HostConfig"]
    assert host_cfg["CapAdd"] == ["NET_BIND_SERVICE", "CHOWN"] and len(plan.summary["mounts"]) == 5


# ── container create refusal list ──────────────────────────────────────────────────────────────────────

def refused(policy, body, match):
    with pytest.raises(Refusal, match=match):
        create(policy, body)


def test_privileged_is_refused(policy):
    refused(policy, host(Privileged=True), "privileged")


@pytest.mark.parametrize("field", ["PidMode", "IpcMode", "NetworkMode", "UsernsMode", "CgroupnsMode", "UTSMode"])
def test_every_host_namespace_mode_is_refused(policy, field):
    with pytest.raises(Refusal):
        create(policy, host(**{field: "host"}))


@pytest.mark.parametrize("mode", ["host", "other_default", "nonesuch"])
def test_network_modes_other_than_builtin_or_scoped_are_refused(policy, mode):
    with pytest.raises(Refusal):
        create(policy, host(NetworkMode=mode))


# ── NetworkMode container:<ref> (G4-F4): served only for a target the daemon says is in scope ─────────────────
def _with_extra_containers(extra):
    from planet_express.missions.dockerproxy.policy import ContainerInfo

    class Inspector(FakeInspector):
        def container(self, ref):
            for name, (cid, lab) in extra.items():
                if ref in (name, cid, cid[:12]):
                    return ContainerInfo(cid, name, lab)
            return super().container(ref)
    return Inspector()


@pytest.mark.parametrize("ref", ["qbittorrent", ID_A, ID_A[:12]])
def test_container_network_mode_of_an_in_scope_target_is_served_and_passed_through(policy, ref):
    plan = create(policy, host(NetworkMode=f"container:{ref}"))
    assert json.loads(plan.body)["HostConfig"]["NetworkMode"] == f"container:{ref}"


@pytest.mark.parametrize("ref, why", [
    ("traefik", "outside this mission's scope"),          # exists, other stack
    ("stray", "outside this mission's scope"),            # no compose labels at all
    ("vault", "protected"),                               # protected wins over in-scope stack
    ("nonesuch", "does not exist"),
    ("bad ref", "not a valid"),
    ("../x", "not a valid"),
    ("", "not a valid"),
])
def test_container_network_mode_of_anything_else_is_refused_with_a_specific_reason(policy, ref, why):
    with pytest.raises(Refusal, match=why):
        create(policy, host(NetworkMode=f"container:{ref}"))


def test_host_network_mode_stays_refused_and_so_does_container_in_an_endpoint_config(policy):
    refused(policy, host(NetworkMode="host"), "host network mode")
    body = host(NetworkMode="container:qbittorrent")
    body["NetworkingConfig"] = {"EndpointsConfig": {"container:qbittorrent": {}}}
    refused(policy, body, "not served")


def test_compose_endpoint_defaults_are_ignored_but_endpoint_configuration_is_refused(policy):
    body = create_body(NetworkingConfig={"EndpointsConfig": {"media_default": {
        "Aliases": ["qbittorrent"], "IPAMConfig": None, "Links": None, "DriverOpts": None, "GwPriority": 0,
        "MacAddress": "", "DNSNames": ["qbittorrent", "abc123"], "NetworkID": "a" * 64,
        "EndpointID": "b" * 64, "Gateway": "172.20.0.1", "IPAddress": "172.20.0.2", "IPPrefixLen": 16,
        "IPv6Gateway": "", "GlobalIPv6Address": "", "GlobalIPv6PrefixLen": 0,
    }}})
    sent = json.loads(create(policy, body).body)["NetworkingConfig"]
    assert sent == {"EndpointsConfig": {"media_default": {"Aliases": ["qbittorrent"]}}}
    for key, value in (("IPAMConfig", {"IPv4Address": "10.0.0.2"}), ("Links", ["x:y"]),
                       ("DriverOpts", {"x": "y"}), ("GwPriority", 1), ("MacAddress", "02:42:ac:11:00:02")):
        bad = create_body(NetworkingConfig={"EndpointsConfig": {"media_default": {key: value}}})
        with pytest.raises(Refusal, match=rf"endpoint {key} is not served"):
            create(policy, bad)
    inspect_only = create_body(NetworkingConfig={"EndpointsConfig": {"media_default": {"EndpointID": "x"}}})
    assert json.loads(create(policy, inspect_only).body)["NetworkingConfig"] == {"EndpointsConfig": {"media_default": {}}}
    unknown = create_body(NetworkingConfig={"EndpointsConfig": {"media_default": {"Spoofed": "x"}}})
    with pytest.raises(Refusal, match="endpoint field not served: Spoofed"):
        create(policy, unknown)


def test_a_service_scoped_recreate_may_rejoin_only_its_existing_compose_project_network(root):
    from planet_express.missions.dockerproxy.policy import NetworkInfo

    class ProjectNetworks(FakeInspector):
        def network(self, ref):
            if ref == "media_backend":
                return NetworkInfo("b" * 64, ref, labels("media"))
            return super().network(ref)

    paths = (f"{HOST}/path:{root}/stacks/media",)
    scoped = Policy(make_scope(str(root), resources=(f"{HOST}/service:media/qbittorrent", *paths), paths=paths),
                    ProjectNetworks())
    body = create_body(NetworkingConfig={"EndpointsConfig": {"media_default": {}}},
                       HostConfig={"NetworkMode": "media_default", "RestartPolicy": {"Name": "no"}})
    assert create(scoped, body)
    for network in ("media_backend", "other_default", "host"):
        bad = create_body(NetworkingConfig={"EndpointsConfig": {network: {}}})
        with pytest.raises(Refusal):
            create(scoped, bad)


def test_an_excluded_container_is_not_a_network_target(root):
    p = Policy(make_scope(str(root), excluded=(f"{HOST}/service:media/plex",)), FakeInspector())
    with pytest.raises(Refusal, match="outside this mission's scope|protected"):
        create(p, host(NetworkMode="container:plex"))
    assert create(p, host(NetworkMode="container:qbittorrent"))


def test_a_target_on_the_host_network_or_chained_to_one_is_refused(root):
    from planet_express.missions.dockerproxy.policy import ContainerInfo

    def policy_with(modes):
        class Inspector(FakeInspector):
            def container(self, ref):
                info = super().container(ref)
                if info is not None and info.name in modes:
                    return ContainerInfo(info.id, info.name, info.labels, {"NetworkMode": modes[info.name]})
                return info
        return Policy(make_scope(str(root)), Inspector())
    with pytest.raises(Refusal, match="host network"):
        create(policy_with({"qbittorrent": "host"}), host(NetworkMode="container:qbittorrent"))
    with pytest.raises(Refusal, match="host network"):                       # a chain: plex -> qbittorrent -> host
        create(policy_with({"plex": "container:qbittorrent", "qbittorrent": "host"}), host(NetworkMode="container:plex"))
    with pytest.raises(Refusal, match="outside this mission's scope"):       # a chain that leaves scope
        create(policy_with({"plex": "container:traefik"}), host(NetworkMode="container:plex"))
    with pytest.raises(Refusal, match="cannot be checked"):
        create(policy_with({"plex": ["host"]}), host(NetworkMode="container:plex"))
    with pytest.raises(Refusal, match="too deeply"):
        create(policy_with({"plex": "container:qbittorrent", "qbittorrent": "container:plex"}), host(NetworkMode="container:plex"))
    assert create(policy_with({"plex": "container:qbittorrent", "qbittorrent": "bridge"}), host(NetworkMode="container:plex"))


def test_a_container_of_another_mission_or_stack_is_not_a_network_target(root):
    inspector = _with_extra_containers({"othermission": ("f" * 64, labels("other", "gluetun"))})
    p = Policy(make_scope(str(root)), inspector)
    for ref in ("othermission", "f" * 64, "f" * 12):
        with pytest.raises(Refusal, match="outside this mission's scope"):
            create(p, host(NetworkMode=f"container:{ref}"))


def test_labels_in_the_body_cannot_put_the_target_in_scope_only_the_daemon_can(policy):
    body = host(NetworkMode="container:traefik")
    body["Labels"] = {**labels("media", "traefik")}           # the body claims; the daemon says traefik is stack ingress
    with pytest.raises(Refusal, match="outside this mission's scope"):
        create(policy, body)


def test_an_ambiguous_reference_is_refused(root):
    from planet_express.missions.dockerproxy.policy import ContainerInfo

    class Ambiguous(FakeInspector):
        def container(self, ref):
            if ref == "aaa":                                  # a daemon that answered with some container the ref does not name
                return ContainerInfo(ID_B, "plex", labels("media", "plex"))
            return super().container(ref)
    with pytest.raises(Refusal, match="ambiguous"):
        create(Policy(make_scope(str(root)), Ambiguous()), host(NetworkMode="container:aaa"))


def test_a_read_only_mission_still_cannot_create_at_all(root):
    p = Policy(None, FakeInspector(), read_only=True)
    with pytest.raises(Refusal, match="read-only"):
        p.match(head("POST", "/v1.43/containers/create"))


def _started(root, mode, target_labels=None):
    """A container the proxy created (CREATED_LABEL) whose HostConfig.NetworkMode is `mode`, started later."""
    from planet_express.missions.dockerproxy.policy import CREATED_LABEL, ContainerInfo

    class Inspector(FakeInspector):
        def container(self, ref):
            if ref == "fresh":
                return ContainerInfo("f" * 64, "fresh", {**labels("media", "dependent"), CREATED_LABEL: "1"},
                                     {"NetworkMode": mode}, ())
            if ref == "swapped" and target_labels is not None:
                return ContainerInfo("9" * 64, "swapped", target_labels)
            return super().container(ref)
    return Policy(make_scope(str(root)), Inspector())


def test_start_re_checks_the_container_network_target(root):
    assert plan_for(_started(root, "container:qbittorrent"), "container.start", "fresh")
    assert plan_for(_started(root, "bridge"), "container.start", "fresh")
    for mode, why in (("container:traefik", "outside this mission's scope"), ("container:vault", "protected"),
                      ("container:gone", "does not exist"), ("host", "host network mode")):
        with pytest.raises(Refusal, match=why):
            plan_for(_started(root, mode), "container.start", "fresh")
    with pytest.raises(Refusal, match="outside this mission's scope"):      # the target was swapped for a different stack's
        plan_for(_started(root, "container:swapped", labels("other", "gluetun")), "container.start", "fresh")
    with pytest.raises(Refusal, match="cannot be checked"):
        plan_for(_started(root, ["container:qbittorrent"]), "container.start", "fresh")


def test_no_other_endpoint_accepts_a_network_mode(policy):
    with pytest.raises(Refusal):                                 # container.update takes limits and restart policy only
        plan_for(policy, "container.update", "qbittorrent", body={"NetworkMode": "container:qbittorrent"})
    with pytest.raises(Refusal):                                 # network connect names a network, never a mode
        plan_for(policy, "network.connect", "container:qbittorrent", body={"Container": "plex"})


@pytest.mark.parametrize("key, value", [("Devices", [{"PathOnHost": "/dev/sda"}]), ("Sysctls", {"net.ipv4.ip_forward": "1"}),
                                        ("Tmpfs", {"/x": ""}), ("DeviceCgroupRules", ["a *:* rwm"]),
                                        ("DeviceRequests", [{"Driver": "nvidia"}])])
def test_devices_sysctls_tmpfs_are_refused(policy, key, value):
    refused(policy, host(**{key: value}), key)


@pytest.mark.parametrize("cap", ["SYS_ADMIN", "ALL", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "NET_RAW", "cap_sys_admin"])
def test_dangerous_capabilities_are_refused(policy, cap):
    refused(policy, host(CapAdd=[cap]), "capability")


@pytest.mark.parametrize("opt", ["seccomp=unconfined", "apparmor=unconfined", "label=disable", "systempaths=unconfined"])
def test_security_opt_beyond_the_allowlist_is_refused(policy, opt):
    refused(policy, host(SecurityOpt=[opt]), "SecurityOpt")


@pytest.mark.parametrize("group", [["0"], ["998"], [998], ["docker"], ["root"], ["-1"], [True]])
def test_group_add_to_a_protected_gid_is_refused(policy, group):
    refused(policy, host(GroupAdd=group), "GroupAdd")


@pytest.mark.parametrize("ref", ["traefik", "vault", "stray", "nonesuch"])
def test_volumes_from_an_out_of_scope_container_is_refused(policy, ref):
    with pytest.raises(Refusal):
        create(policy, host(VolumesFrom=[ref]))
    assert create(policy, host(VolumesFrom=["plex:ro"])).body


def test_an_unknown_top_level_or_host_config_field_is_refused(policy):
    refused(policy, create_body(Volumes={"/x": {}}), "Volumes is not served")
    refused(policy, create_body(OnBuild=["x"]), "OnBuild is not served")
    refused(policy, host(CgroupParent="/x"), "HostConfig CgroupParent is not served")
    refused(policy, host(Runtime="nvidia"), "HostConfig Runtime is not served")
    refused(policy, host(Links=["a:b"]), "HostConfig Links is not served")


def test_an_empty_inspect_shaped_onbuild_placeholder_is_ignored_but_an_instruction_is_refused(policy):
    sent = json.loads(create(policy, create_body(OnBuild=None)).body)
    assert "OnBuild" not in sent
    for key in ("OnBuild", "Volumes", "Shell"):
        for value in (None, [], {}, "", False, 0):
            assert key not in json.loads(create(policy, create_body(**{key: value})).body)
        with pytest.raises(Refusal, match=rf"{key} is not served"):
            create(policy, create_body(**{key: ["not inert"]}))


def test_zero_value_inspect_host_config_fields_are_ignored_but_meaningful_values_are_refused(policy):
    inert = {"BlkioDeviceReadBps": [], "Runtime": "", "CgroupParent": None, "CpuQuota": 0, "OomKillDisable": False,
             "ConsoleSize": [0, 0]}
    sent = json.loads(create(policy, host(**inert)).body)["HostConfig"]
    assert not (set(sent) & set(inert))
    for key, value in (("BlkioDeviceReadBps", [{"Path": "/dev/sda", "Rate": 1}]), ("Runtime", "nvidia"),
                       ("CgroupParent", "/x"), ("CpuQuota", 1), ("OomKillDisable", True)):
        with pytest.raises(Refusal, match=rf"HostConfig {key} is not served"):
            create(policy, host(**{key: value}))
    with pytest.raises(Refusal, match="HostConfig ConsoleSize is not served"):
        create(policy, host(ConsoleSize=[80, 25]))


def test_an_empty_inspect_log_driver_marker_is_ignored_but_an_unapproved_driver_is_refused(policy):
    for config in (None, {}, []):
        sent = json.loads(create(policy, host(LogConfig={"Type": "", "Config": config})).body)["HostConfig"]
        assert "LogConfig" not in sent
    with pytest.raises(Refusal, match="LogConfig not served"):
        create(policy, host(LogConfig={"Type": "syslog", "Config": {}}))


def test_the_new_container_must_carry_labels_that_put_it_in_scope(policy):
    refused(policy, create_body(Labels={}), "labels")
    refused(policy, create_body(Labels=labels("ingress", "traefik")), "outside")
    refused(policy, create_body(Labels=labels("media", "vault")), "protected")
    refused(policy, create_body(Labels={"com.docker.compose.project": "media"}), "labels")
    refused(policy, create_body(Labels=None), "labels")


def test_an_image_that_is_not_present_is_refused_a_worker_never_pulls(policy):
    refused(policy, create_body(Image="evil/image:latest"), "never pulls")
    refused(policy, create_body(Image="../x"), "bad string")


# ── mounts ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_bind_of_a_host_path_outside_the_grant_is_refused(policy, root):
    refused(policy, host(Binds=["/etc:/etc"]), "outside the grant")
    refused(policy, host(Binds=[f"{root}/elsewhere:/e"]), "outside the grant")
    refused(policy, host(Binds=["/:/host"]), "outside|protected")
    refused(policy, host(Mounts=[{"Type": "bind", "Source": "/etc/shadow", "Target": "/s"}]), "outside|protected")


def test_the_docker_socket_is_never_mountable(policy, root):
    refused(policy, host(Binds=["/var/run/docker.sock:/var/run/docker.sock"]), "socket|outside|protected")
    refused(policy, host(Mounts=[{"Type": "bind", "Source": "/run/docker.sock", "Target": "/d.sock"}]), "socket|protected|outside")
    inside = root / "stacks" / "media" / "docker.sock"
    inside.write_text("")
    refused(policy, host(Binds=[f"{inside}:/d.sock"]), "socket")                 # even by name, inside a granted path
    real = root / "stacks" / "media" / "some.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(real))
    try:
        refused(policy, host(Binds=[f"{real}:/x"]), "socket")                      # even under another name
    finally:
        s.close()


def test_path_tricks_in_a_bind_source_are_refused(policy, root):
    for source in (f"{root}/stacks/media/../../elsewhere", f"{root}/stacks/media/./config", f"{root}//stacks/media/config",
                   f"{root}/stacks/media/config/", "relative/../x:/y", "\x00"):
        with pytest.raises(Refusal):
            create(policy, host(Binds=[f"{source}:/x"]))


def test_a_symlink_out_of_the_granted_path_is_refused(policy, root):
    link = root / "stacks" / "media" / "escape"
    link.symlink_to(root / "elsewhere")
    refused(policy, host(Binds=[f"{link}:/x"]), "symlink")
    refused(policy, host(Mounts=[{"Type": "bind", "Source": str(link), "Target": "/x"}]), "symlink")
    inner = root / "stacks" / "media" / "config" / "link"
    inner.symlink_to("/etc")
    refused(policy, host(Binds=[f"{inner}/passwd:/x"]), "symlink")                  # the symlink is mid-path


def test_a_bind_that_touches_a_protected_resource_is_refused(root):
    scope = make_scope(str(root), protected=(f"host:local/path:{root}/stacks/media/config",))
    p = Policy(scope, FakeInspector())
    with pytest.raises(Refusal, match="protected"):
        create(p, host(Binds=[f"{root}/stacks/media/config:/x"]))


@pytest.mark.parametrize("bind", ["media_cfg", "media_cfg:/c:shared", "/a:/b:rshared", "/a:/b:slave", "a:b:c:d", ":/x", "media_cfg:",
                                  "/x"])
def test_malformed_binds_and_propagation_options_are_refused(policy, root, bind):
    with pytest.raises(Refusal):
        create(policy, host(Binds=[bind.replace("/a", f"{root}/data")]))


@pytest.mark.parametrize("volume", ["other_vol", "nonesuch", "bindvol", "fancy"])
def test_only_an_existing_scoped_plain_local_named_volume_may_be_mounted(policy, volume):
    with pytest.raises(Refusal):
        create(policy, host(Binds=[f"{volume}:/x"]))
    with pytest.raises(Refusal):
        create(policy, host(Mounts=[{"Type": "volume", "Source": volume, "Target": "/x"}]))


def test_a_volume_can_not_be_created_by_a_worker_in_any_way(policy):
    refused(policy, host(Mounts=[{"Type": "volume", "Source": "brand-new", "Target": "/x"}]), "never created")
    refused(policy, host(Mounts=[{"Type": "volume", "Source": "media_cfg", "Target": "/x",
                                  "VolumeOptions": {"DriverConfig": {"Name": "local", "Options": {"o": "bind"}}}}]), "volume options")
    refused(policy, host(Binds=["/x"]), "anonymous|source and a target")
    refused(policy, host(Mounts=[{"Type": "tmpfs", "Target": "/x"}]), "only bind and named-volume")
    for name in ("volumes/create", "volumes/prune"):
        with pytest.raises(Refusal):
            policy.match(head("POST", f"/v1.43/{name}"))


def test_mount_option_blocks_are_refused(policy, root):
    refused(policy, host(Mounts=[{"Type": "bind", "Source": f"{root}/data", "Target": "/x",
                                  "BindOptions": {"Propagation": "rshared"}}]), "bind option")
    refused(policy, host(Mounts=[{"Type": "bind", "Source": f"{root}/data", "Target": "/x", "BindOptions": {"CreateMountpoint": True}}]),
            "bind option")
    refused(policy, host(Mounts=[{"Type": "bind", "Source": f"{root}/data", "Target": "../x"}]), "target")


# ── routes: what is never served ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, target", [
    ("POST", "/v1.43/build"), ("POST", "/v1.43/images/load"), ("POST", "/v1.43/images/create"), ("POST", "/v1.43/images/prune"),
    ("POST", "/v1.43/containers/prune"), ("POST", "/v1.43/networks/prune"), ("POST", "/v1.43/volumes/prune"),
    ("POST", "/v1.43/system/prune"), ("GET", "/v1.43/info"), ("GET", "/v1.43/swarm"),
    ("GET", "/v1.43/secrets"), ("GET", "/v1.43/configs"), ("GET", "/v1.43/plugins"), ("POST", "/v1.43/plugins/pull"),
    ("POST", "/v1.43/swarm/init"), ("POST", "/v1.43/session"), ("POST", "/v1.43/auth"), ("GET", "/v1.43/system/df"),
    ("DELETE", "/v1.43/images/alpine:3"), ("POST", "/v1.43/images/alpine/tag"), ("POST", "/v1.43/images/alpine/push"),
    ("POST", "/v1.43/networks/create"), ("DELETE", "/v1.43/networks/media_default"), ("POST", "/v1.43/containers/plex/pause"),
    ("POST", "/v1.43/containers/plex/commit"), ("POST", "/v1.43/commit"), ("GET", "/v1.43/containers/plex/export"),
    ("GET", "/v1.43/containers/plex/attach/ws"), ("POST", "/v1.43/services/create"), ("POST", "/v1.43/containers/plex/wait"),
    ("PUT", "/v1.43/containers/create"), ("GET", "/v1.43/containers/create"), ("DELETE", "/v1.43/volumes/media_cfg"),
])
def test_an_unlisted_endpoint_or_method_is_refused(policy, method, target):
    with pytest.raises(Refusal):
        policy.match(head(method, target))


def test_an_unknown_api_version_is_refused_and_so_is_an_unversioned_endpoint_other_than_ping(policy):
    for target in ("/v1.99/containers/json", "/v1.40/containers/json", "/v2.0/containers/json", "/v1.4/containers/json"):
        with pytest.raises(Refusal):
            head("GET", target)
            policy.match(head("GET", target))
    policy.match(head("GET", "/v1.43/containers/json"))
    policy.match(head("GET", "/_ping"))
    policy.match(head("GET", "/version"))                     # compose and the docker CLI negotiate with it


def test_version_is_a_read_served_to_every_tier_in_both_forms_and_nothing_else(root):
    for p in (Policy(make_scope(str(root)), FakeInspector()), Policy(None, FakeInspector(), read_only=True)):
        for target in ("/version", "/v1.43/version", "/_ping", "/v1.43/_ping"):
            r, _ = p.match(head("GET", target))
            assert not r.write
        assert p.match(head("HEAD", "/_ping"))[0].name == "ping"
        for method in ("POST", "PUT", "DELETE", "HEAD"):
            with pytest.raises(Refusal):
                p.match(head(method, "/v1.43/version"))
        r = p.match(head("GET", "/v1.43/version"))[0]
        assert p.plan(r, "GET", {}, {}, None).upstream_path == "/version"
        with pytest.raises(Refusal):
            p.clean_query(r, {"remote": "x"})                  # no query parameter is accepted
        with pytest.raises(Refusal):
            p.match(head("GET", "/v1.99/version"))


@pytest.mark.parametrize("query", [{"remote": "http://evil/x"}, {"fromSrc": "http://evil/x"}, {"fromImage": "alpine"}, {"link": "1"},
                                   {"t": "1; rm"}, {"unknown": "1"}, {"name": "../x"}, {"name": "a b"}, {"platform": "x y"}])
def test_remote_and_unknown_query_parameters_are_refused(policy, query):
    with pytest.raises(Refusal):
        policy.clean_query(route("container.create"), query)


def test_removing_anonymous_volumes_with_a_container_is_refused(policy):
    with pytest.raises(Refusal):
        policy.clean_query(route("container.remove"), {"v": "1"})
    assert policy.clean_query(route("container.remove"), {"force": "1", "v": "0"}) == {"force": "1", "v": "0"}


# ── objects outside the scope ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["container.start", "container.stop", "container.restart", "container.kill", "container.rename",
                                  "container.remove", "container.logs", "container.stats", "container.archive.get",
                                  "container.attach", "container.update", "exec.create"])
@pytest.mark.parametrize("ref", ["traefik", "vault", "stray", ID_A[:12] + "zz"])
def test_every_container_operation_refuses_an_out_of_scope_or_protected_container(policy, name, ref):
    r = route(name)
    body = {"Cmd": ["id"]} if name == "exec.create" else {} if name == "container.update" else None
    query = {"path": "/x"} if "archive" in name else {"name": "x"} if name == "container.rename" else {}
    with pytest.raises(Refusal):
        policy.plan(r, r.methods[0], {"id": ref}, policy.clean_query(r, query), body)


def test_an_in_scope_container_is_resolved_to_its_full_id_so_a_rename_cannot_redirect_the_request(policy):
    plan = plan_for(policy, "container.restart", "qbittorrent", query={"t": "5"})
    assert plan.upstream_path == f"/containers/{ID_A}/restart" and plan.query == {"t": "5"}


def test_inspect_is_allowed_for_non_protected_containers_but_never_for_protected_ones(policy):
    assert plan_for(policy, "container.inspect", "traefik").upstream_path.endswith("/json")
    assert plan_for(policy, "container.inspect", "stray")
    with pytest.raises(Refusal, match="protected"):
        plan_for(policy, "container.inspect", "vault")


def test_network_connect_needs_an_in_scope_network_and_container(policy):
    ok = plan_for(policy, "network.connect", "media_default", body={"Container": "qbittorrent"})
    assert json.loads(ok.body)["Container"] == ID_A
    for net, ctr in (("other_default", "qbittorrent"), ("host", "qbittorrent"), ("bridge", "qbittorrent"),
                     ("media_default", "traefik"), ("nonesuch", "qbittorrent")):
        with pytest.raises(Refusal):
            plan_for(policy, "network.connect", net, body={"Container": ctr})
    with pytest.raises(Refusal):
        plan_for(policy, "network.connect", "media_default", body={"Container": "plex", "EndpointConfig": {"IPAMConfig": {}}})


# ── exec ───────────────────────────────────────────────────────────────────────────────────────────

def test_exec_create_remembers_its_container_and_refuses_privileged_and_unknown_fields(policy):
    plan = plan_for(policy, "exec.create", "plex", body={"Cmd": ["ls", "-l"], "AttachStdout": True})
    assert plan.exec_container == ID_B and plan.summary["cmd_argc"] == 2
    for body in ({"Cmd": ["id"], "Privileged": True}, {"Cmd": ["id"], "Extra": 1}, {"AttachStdout": True}, {"Cmd": "id"},
                 {"Cmd": ["id"], "Env": ["bad env"]}):
        with pytest.raises(Refusal):
            plan_for(policy, "exec.create", "plex", body=body)


def test_exec_start_only_for_an_exec_this_session_made_on_a_container_still_in_scope(root):
    execs = {"1" * 64: ID_B, "2" * 64: "c" * 64}
    p = Policy(make_scope(str(root)), FakeInspector(), exec_owner=execs.get)
    assert plan_for(p, "exec.start", "1" * 64, body={"Detach": False}).upstream_path == f"/exec/{'1' * 64}/start"
    with pytest.raises(Refusal):
        plan_for(p, "exec.start", "2" * 64, body={})                      # its container is out of scope
    with pytest.raises(Refusal):
        plan_for(p, "exec.start", "3" * 64, body={})                      # an exec this session never made
    with pytest.raises(Refusal):
        plan_for(p, "exec.start", "1" * 64, body={"Detach": False, "x": 1})


def test_an_upgrade_is_only_served_on_exec_start_and_attach(policy):
    up = {"Connection": "Upgrade", "Upgrade": "tcp"}
    assert policy.check_upgrade(route("exec.start"), head("POST", "/v1.43/exec/x/start", **up)) is True
    assert policy.check_upgrade(route("container.attach"), head("POST", "/v1.43/containers/x/attach", **up)) is True
    assert policy.check_upgrade(route("exec.start"), head("POST", "/v1.43/exec/x/start")) is False
    for r, t in (("container.restart", "/v1.43/containers/x/restart"), ("container.logs", "/v1.43/containers/x/logs")):
        with pytest.raises(Refusal):
            policy.check_upgrade(route(r), head("GET", t, **up))
    for bad in ({"Connection": "Upgrade", "Upgrade": "websocket"}, {"Connection": "Upgrade, keep-alive", "Upgrade": "tcp"},
                {"Upgrade": "tcp"}, {"Connection": "Upgrade"}, {"Connection": "te"}):
        with pytest.raises(Refusal):
            policy.check_upgrade(route("exec.start"), head("POST", "/v1.43/exec/x/start", **bad))


# ── update ─────────────────────────────────────────────────────────────────────────────────────────

def test_update_takes_resource_limits_and_restart_policy_only(policy):
    plan = plan_for(policy, "container.update", "plex", body={"Memory": 1024, "RestartPolicy": {"Name": "always"}})
    assert json.loads(plan.body) == {"Memory": 1024, "RestartPolicy": {"Name": "always"}}
    for body in ({"Privileged": True}, {"Binds": ["/:/x"]}, {"Memory": "1g"}, {"Memory": True}, {"RestartPolicy": {"Name": "evil"}}):
        with pytest.raises(Refusal):
            plan_for(policy, "container.update", "plex", body=body)


# ── responses ────────────────────────────────────────────────────────────────────────────────────────

def test_inspect_output_has_env_values_and_secret_labels_redacted():
    doc = {"Config": {"Env": ["API_KEY=hunter2hunter2", "PUID=1000", "NOEQUALS"], "Cmd": ["run", "--password=hunter2hunter2"],
                      "Labels": {"com.docker.compose.project": "media", "traefik.api.token": "abc123def456", "x": "ok"}}}
    out = json.dumps(redact_inspect(doc))
    assert "hunter2" not in out and "1000" not in out and "abc123" not in out
    assert "API_KEY=" in out and "PUID=" in out and "com.docker.compose.project" in out and '"x": "ok"' in out


def test_read_only_policy_serves_reads_and_nothing_else(root):
    p = Policy(None, FakeInspector(), read_only=True)
    for target in ("/v1.43/containers/json", "/v1.43/containers/plex/json", "/v1.43/images/json", "/v1.43/networks", "/v1.43/volumes",
                   "/v1.43/events", "/_ping"):
        p.match(head("GET", target))
    for method, target in (("POST", "/v1.43/containers/create"), ("POST", "/v1.43/containers/plex/start"),
                           ("GET", "/v1.43/containers/plex/logs"), ("GET", "/v1.43/containers/plex/archive"),
                           ("GET", "/v1.43/containers/plex/stats"), ("DELETE", "/v1.43/containers/plex"),
                           ("POST", "/v1.43/containers/plex/exec"), ("GET", "/v1.43/exec/x/json")):
        with pytest.raises(Refusal):
            p.match(head(method, target))
    with pytest.raises(ValueError):
        Policy(None, FakeInspector())


def test_a_bind_source_that_is_a_file_name_trick_is_refused_by_normalisation_not_by_existence(policy, root):
    assert os.path.exists(root / "stacks" / "media" / "config")
    refused(policy, host(Binds=[f"{root}/stacks/media/config/../config:/x"]), "normalised")


# ── exec and attach are root on the host when the container is (review finding) ─────────────────────────────────

class Hazard(FakeInspector):
    """A FakeInspector whose `plex` carries the given inspect HostConfig and Mounts."""

    def __init__(self, host_config=None, mounts=()):
        super().__init__()
        self.host_config, self.mounts = host_config or {}, tuple(mounts)

    def container(self, ref):
        info = super().container(ref)
        if info is None or info.name != "plex":
            return info
        from planet_express.missions.dockerproxy.policy import ContainerInfo
        return ContainerInfo(info.id, info.name, info.labels, self.host_config, self.mounts)


@pytest.mark.parametrize("host_config, mounts, why", [
    ({"Privileged": True}, (), "privileged"),
    ({"PidMode": "host"}, (), "PidMode"),
    ({"UsernsMode": "host"}, (), "UsernsMode"),
    ({"CapAdd": ["SYS_ADMIN"]}, (), "capability"),
    ({"CapAdd": ["cap_sys_ptrace"]}, (), "capability"),
    ({"CapAdd": ["ALL"]}, (), "capability"),
    ({"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]}, (), "Docker socket"),
    ({"Binds": ["/run/docker.sock:/d.sock:ro"]}, (), "Docker socket"),
    ({}, ({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/d.sock"},), "Docker socket"),
    ({}, ({"Type": "bind", "Source": "/var/run", "Destination": "/r"},), "Docker socket"),
    ({}, ({"Type": "bind", "Source": "/run", "Destination": "/r"},), "Docker socket"),
    ({}, ({"Type": "bind", "Source": "/", "Destination": "/host"},), "Docker socket"),
    ({}, ({"Source": "/srv/anything/docker.sock", "Destination": "/d"},), "Docker socket"),
])
def test_exec_and_attach_are_refused_on_a_root_equivalent_container(root, host_config, mounts, why):
    p = Policy(make_scope(str(root)), Hazard(host_config, mounts), exec_owner={"1" * 64: ID_B}.get)
    for name, body in (("exec.create", {"Cmd": ["id"]}), ("container.attach", None)):
        with pytest.raises(Refusal, match=why):
            plan_for(p, name, "plex", body=body)
    with pytest.raises(Refusal, match=why):
        plan_for(p, "exec.start", "1" * 64, body={})
    assert plan_for(p, "container.inspect", "plex") and plan_for(p, "container.logs", "plex")      # still readable
    for name, query in (("container.start", {}), ("container.restart", {}), ("container.archive.put", {"path": "/x"})):
        with pytest.raises(Refusal, match=why):                                                     # but never acted on
            plan_for(p, name, "plex", query=query, raw_length=5 if "archive" in name else 0)


def test_an_ordinary_mount_of_data_does_not_stop_an_exec(root):
    mounts = ({"Type": "bind", "Source": str(root / "data"), "Destination": "/data"}, {"Type": "volume", "Name": "x"})
    p = Policy(make_scope(str(root)), Hazard({"CapAdd": ["NET_ADMIN"], "Binds": [f"{root}/data:/data"]}, mounts))
    assert plan_for(p, "exec.create", "plex", body={"Cmd": ["id"]})


def test_volume_inspect_needs_the_volume_to_be_in_scope_but_the_read_only_proxy_reads_any(policy):
    assert plan_for(policy, "volume.inspect", "media_cfg").upstream_path == "/volumes/media_cfg"
    assert plan_for(policy, "volume.inspect", "fancy")                                  # in scope, even if its driver is not plain
    for name in ("other_vol", "nonesuch"):
        with pytest.raises(Refusal):
            plan_for(policy, "volume.inspect", name)
    assert plan_for(Policy(None, FakeInspector(), read_only=True), "volume.inspect", "other_vol")


def test_exec_is_refused_on_a_container_whose_named_volume_is_bind_backed_by_the_docker_socket_or_its_directory(root):
    for device in ("/var/run/docker.sock", "/var/run", "/run", "/"):
        class Volumes(Hazard):
            def volume(self, name):
                return {"Driver": "local", "Options": {"type": "none", "o": "bind", "device": device}, "Labels": labels("media")}
        p = Policy(make_scope(str(root)), Volumes({}, ({"Type": "volume", "Name": "dockervol", "Source": "/var/lib/docker/v/_data"},)))
        with pytest.raises(Refusal, match="Docker socket"):
            plan_for(p, "exec.create", "plex", body={"Cmd": ["id"]})
    harmless = Hazard({}, ({"Type": "volume", "Name": "media_cfg", "Source": "/var/lib/docker/volumes/media_cfg/_data"},))
    assert plan_for(Policy(make_scope(str(root)), harmless), "exec.create", "plex", body={"Cmd": ["id"]})


# ── VolumesFrom inherits the source's mounts, so the source is held to the same rules ────────────────────────

@pytest.mark.parametrize("host_config, mounts, why", [
    ({"Privileged": True}, (), "privileged"),
    ({}, ({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/d"},), "Docker socket"),
    ({"Binds": ["/run:/r"]}, (), "Docker socket"),
])
def test_volumes_from_a_container_with_the_docker_socket_or_that_is_root_equivalent_is_refused(root, host_config, mounts, why):
    p = Policy(make_scope(str(root)), Hazard(host_config, mounts))
    with pytest.raises(Refusal, match=why):
        create(p, host(VolumesFrom=["plex"]))


def test_volumes_from_a_container_that_mounts_a_protected_path_is_refused_but_exec_on_it_is_not(root):
    protected = (f"host:local/mount:{root}/data",)
    mounts = ({"Type": "bind", "Source": str(root / "data"), "Destination": "/data"},)
    p = Policy(make_scope(str(root), protected=protected), Hazard({}, mounts))
    with pytest.raises(Refusal, match="protected path"):
        create(p, host(VolumesFrom=["plex:ro"]))
    assert plan_for(p, "exec.create", "plex", body={"Cmd": ["id"]})            # the accepted residual: data volumes via exec


def test_inherited_mounts_are_re_checked_when_a_created_container_starts(root):
    from planet_express.missions.dockerproxy.policy import CREATED_LABEL, ContainerInfo
    mounts = ({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/d"},)

    class Inspector(Hazard):
        def container(self, ref):
            if ref == "fresh":
                return ContainerInfo("f" * 64, "fresh", {**labels("media", "qbittorrent"), CREATED_LABEL: "1"},
                                     {"VolumesFrom": ["plex"]}, ())
            return super().container(ref)
    p = Policy(make_scope(str(root)), Inspector({}, mounts))
    with pytest.raises(Refusal, match="Docker socket"):
        plan_for(p, "container.start", "fresh")
    clean = Policy(make_scope(str(root)), Inspector({}, ()))
    assert plan_for(clean, "container.start", "fresh")


# ── API versions follow the daemon ──────────────────────────────────────────────────────────────────

def test_the_served_versions_are_the_floor_up_to_the_lower_of_the_daemon_and_the_proxy():
    from planet_express.missions.dockerproxy.policy import API_FLOOR, API_MAX, served_versions
    assert served_versions(52) == served_versions(None) == frozenset(f"v1.{n}" for n in range(API_FLOOR, API_MAX + 1))
    assert max(served_versions(45)) == "v1.45" and "v1.46" not in served_versions(45) and "v1.41" in served_versions(45)
    assert served_versions(30) == frozenset()                                    # an ancient daemon: nothing is served
    assert Policy(make_scope("/x"), FakeInspector(), versions=served_versions(45)).max_minor == 45


# ── labels drive other components: a worker may not invent them (audit M1) ───────────────────────────────────

class WithLabels(FakeInspector):
    """An inspector whose `qbittorrent` service already carries extra labels (as a compose service with traefik labels does)."""

    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def service_labels(self, project, service):
        return [{**labels(project, service), **self.existing}] if (project, service) == ("media", "qbittorrent") else []


def with_labels(extra, **kw):
    return create_body(Labels={**labels("media", "qbittorrent"), **extra}), kw


@pytest.mark.parametrize("key", ["traefik.enable", "traefik.http.routers.x.rule", "traefik.http.routers.x.priority",
                                 "com.centurylinklabs.watchtower.enable", "com.centurylinklabs.watchtower.monitor-only",
                                 "autoheal", "autoheal.stop.timeout", "homepage.name", "homepage.href", "dockflow.deploy",
                                 "caddy", "caddy.reverse_proxy", "io.portainer.accesscontrol.users", "random.thing", "x"])
def test_a_label_that_is_not_compose_carried_over_or_allowlisted_is_refused(policy, key):
    body, _ = with_labels({key: "true"})
    with pytest.raises(Refusal, match="not allowed"):
        create(policy, body)


def test_the_compose_labels_and_the_proxys_own_are_always_fine(policy):
    body, _ = with_labels({"com.docker.compose.config-hash": "abc", "com.docker.compose.container-number": "1",
                           "com.docker.compose.project.working_dir": "/x"})
    assert create(policy, body)


def test_a_recreate_keeps_the_labels_the_service_already_has_with_the_same_values(root):
    existing = {"traefik.enable": "true", "traefik.http.routers.q.rule": "Host(`q.example`)", "homepage.name": "qBit"}
    p = Policy(make_scope(str(root)), WithLabels(existing))
    body, _ = with_labels(existing)
    sent = json.loads(create(p, body).body)["Labels"]
    assert sent["traefik.enable"] == "true" and sent["homepage.name"] == "qBit"
    for changed in ({"traefik.http.routers.q.rule": "Host(`evil.example`)"}, {"traefik.http.routers.q.priority": "9999"},
                    {"traefik.enable": "false"}):
        bad, _ = with_labels({**existing, **changed})
        with pytest.raises(Refusal, match="not allowed"):
            create(p, bad)
    other = create_body(Labels={**labels("media", "plex"), "traefik.enable": "true"})      # plex has no such labels: not carried over
    with pytest.raises(Refusal, match="not allowed"):
        create(Policy(make_scope(str(root)), WithLabels(existing)), other)


def test_the_operators_allowlist_admits_exact_keys_and_dotted_prefixes_only(root):
    p = Policy(make_scope(str(root)), FakeInspector(), label_allow=("org.example.owner", "app.meta."))
    body, _ = with_labels({"org.example.owner": "me", "app.meta.team": "ops", "app.meta.x.y": "1"})
    assert create(p, body)
    for key in ("org.example.owner2", "app.metadata", "org.example", "traefik.enable"):
        bad, _ = with_labels({key: "1"})
        with pytest.raises(Refusal, match="not allowed"):
            create(p, bad)


def test_an_inspector_that_cannot_say_what_the_service_had_carries_nothing_over(root):
    class Broken(FakeInspector):
        def service_labels(self, project, service):
            raise OSError("daemon down")
    bad, _ = with_labels({"traefik.enable": "true"})
    with pytest.raises(Refusal, match="not allowed"):
        create(Policy(make_scope(str(root)), Broken()), bad)


# ── more root-equivalent containers (audit M2) ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["/etc", "/etc/traefik", "/root", "/root/.ssh", "/home/chris", "/var/lib/docker", "/var/lib/docker/volumes",
                                    "/proc", "/sys", "/boot", "/", "/var", "/usr/local", "/dev"])
def test_exec_restart_and_archive_put_are_refused_on_a_container_with_a_system_path_mounted_read_write(root, source):
    mounts = ({"Type": "bind", "Source": source, "Destination": "/h", "RW": True},)
    p = Policy(make_scope(str(root)), Hazard({}, mounts))
    for name, body, query in (("exec.create", {"Cmd": ["id"]}, {}), ("container.attach", None, {}), ("container.restart", None, {}),
                              ("container.start", None, {}), ("container.archive.put", None, {"path": "/x"})):
        with pytest.raises(Refusal, match="host write access|Docker socket"):          # "/" and "/var" hold the socket too
            plan_for(p, name, "plex", body=body, query=query, raw_length=3 if "archive" in name else 0)


def test_a_read_only_system_mount_and_a_mount_inside_the_grant_are_fine(root):
    ro = ({"Type": "bind", "Source": "/etc", "Destination": "/h", "RW": False},)
    assert plan_for(Policy(make_scope(str(root)), Hazard({}, ro)), "exec.create", "plex", body={"Cmd": ["id"]})
    assert plan_for(Policy(make_scope(str(root)), Hazard({"Binds": ["/etc/hostname:/h:ro"]}, ())), "exec.create", "plex",
                    body={"Cmd": ["id"]})
    inside = ({"Type": "bind", "Source": str(root / "stacks" / "media" / "config"), "Destination": "/c", "RW": True},
              {"Type": "bind", "Source": str(root / "data"), "Destination": "/d", "RW": True})
    assert plan_for(Policy(make_scope(str(root)), Hazard({}, inside)), "exec.create", "plex", body={"Cmd": ["id"]})
    rw_etc = Hazard({"Binds": ["/etc/hostname:/h"]}, ())
    with pytest.raises(Refusal, match="host write access"):
        plan_for(Policy(make_scope(str(root)), rw_etc), "exec.create", "plex", body={"Cmd": ["id"]})


def test_a_protected_path_mounted_read_write_outside_the_grant_is_refused_but_a_data_mount_is_not(root):
    protected = (f"host:local/path:{root}/elsewhere/secrets",)
    scope = make_scope(str(root), protected=protected)
    bad = ({"Type": "bind", "Source": f"{root}/elsewhere/secrets", "Destination": "/s", "RW": True},)
    with pytest.raises(Refusal, match="host write access"):
        plan_for(Policy(scope, Hazard({}, bad)), "exec.create", "plex", body={"Cmd": ["id"]})
    data = ({"Type": "bind", "Source": f"{root}/elsewhere/media-data", "Destination": "/m", "RW": True},)
    assert plan_for(Policy(scope, Hazard({}, data)), "exec.create", "plex", body={"Cmd": ["id"]})


@pytest.mark.parametrize("host_config, why", [
    ({"Devices": [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}]}, "host devices"),
    ({"Devices": [{"PathOnHost": "/dev/dri"}]}, "host devices"),
    ({"SecurityOpt": ["seccomp=unconfined"]}, "unconfined"),
    ({"SecurityOpt": ["apparmor=unconfined"]}, "unconfined"),
    ({"SecurityOpt": ["no-new-privileges", "label=type:unconfined_t"]}, "unconfined"),
    ({"SecurityOpt": ["seccomp:unconfined"]}, "unconfined"),
])
def test_devices_and_unconfined_profiles_make_a_container_unactionable(root, host_config, why):
    p = Policy(make_scope(str(root)), Hazard(host_config, ()))
    with pytest.raises(Refusal, match=why):
        plan_for(p, "exec.create", "plex", body={"Cmd": ["id"]})
    assert Policy(make_scope(str(root)), Hazard(host_config, ())).hazards(Hazard(host_config, ()).container("plex"))
    assert Policy(make_scope(str(root)), FakeInspector()).hazards(FakeInspector().container("plex")) == []


def test_a_bind_of_a_parent_of_an_excluded_path_is_refused_but_a_sibling_is_not(root):
    (root / "stacks" / "media" / "other").mkdir()
    scope = make_scope(str(root), excluded=(f"host:local/path:{root}/stacks/media/config",))
    p = Policy(scope, FakeInspector())
    with pytest.raises(Refusal, match="excluded"):
        create(p, host(Binds=[f"{root}/stacks/media:/x"]))                # the parent would hand over the excluded child
    with pytest.raises(Refusal, match="excluded"):
        create(p, host(Binds=[f"{root}/stacks/media/config:/x"]))
    assert create(p, host(Binds=[f"{root}/stacks/media/other:/x"]))        # a sibling is untouched


def test_joining_a_target_attached_to_an_out_of_scope_network_is_refused(root):
    from planet_express.missions.dockerproxy.policy import ContainerInfo

    def policy_with(networks):
        class Inspector(FakeInspector):
            def container(self, ref):
                info = super().container(ref)
                if info is not None and info.name == "qbittorrent":
                    return ContainerInfo(info.id, info.name, info.labels, {}, (), networks)
                return info
        return Policy(make_scope(str(root)), Inspector())
    assert create(policy_with(("media_default", "bridge")), host(NetworkMode="container:qbittorrent"))
    with pytest.raises(Refusal, match="outside this mission's scope"):
        create(policy_with(("media_default", "other_default")), host(NetworkMode="container:qbittorrent"))
    with pytest.raises(Refusal, match="cannot be checked"):
        create(policy_with(None), host(NetworkMode="container:qbittorrent"))
