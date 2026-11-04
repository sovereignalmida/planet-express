"""Rollback is the best path back, not a literal undo (rc14.1, from live M-19 on the operator's host).

M-19: the session appended a comment line to the media stack's `.env` and recreated prowlarr. The rc14 restore wrote `.env`
back and then force-recreated all 24 services of the stack (every service of a written stack counted as affected). That took
about 3 minutes, hit the 180 s timeout with no reason recorded, started `lidarr` (behind a profile, no container at the
checkpoint), and left the mission needing the operator. The following cancel was then held, because lidarr's container now
existed. These tests pin the replacement: act only on services whose container facts, run state or effective Compose config
differ, never `--force-recreate`, never name a service that had no container, and record why an action failed.

The inspect documents have the shape `docker inspect` printed for CASA_PROWLARR on that host (Compose 2.39.1). Environment
values are placeholders.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkpoint_fakes import make_env

from planet_express.missions import recovery
from planet_express.missions.checkpoint.provider import (
    UP_MAX_TIMEOUT_S,
    ContainerFacts,
    action_timeout,
    comparable_facts,
)

MEDIA = """services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    env_file: .env
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
  archivist-es:
    image: elasticsearch:8
  lidarr:
    image: lscr.io/linuxserver/lidarr:latest
    profiles:
      - legacy-lidarr
"""
DOTENV = "PUID=1000\nPGID=1000\nTZ=Europe/Lisbon"            # no trailing newline, as on the host


def recorded(service="prowlarr", *, cid="8ea58764531a" + "0" * 52, created="2026-11-04T11:56:41.100000000Z",
             started="2026-11-04T11:56:41.843000000Z", endpoint="1e498da529ff0619", config_hash="9f537499aacc2626",
             compose_version="2.39.1", config_files="/home/casaroot/stacks/media/docker-compose.yml", env=None,
             status="running", health=None, labels=None):
    """A container as `docker inspect` shows it (trimmed to the keys PE reads plus the identity ones that change on a recreate)."""
    state = {"Status": status, "Running": status == "running", "Paused": False, "Restarting": False, "OOMKilled": False,
             "Dead": False, "Pid": 2686986, "ExitCode": 0, "Error": "", "StartedAt": started, "FinishedAt": "0001-01-01T00:00:00Z"}
    if health:
        state["Health"] = {"Status": health, "FailingStreak": 0, "Log": []}
    return {
        "Id": cid, "Created": created, "Name": f"/CASA_{service.upper()}", "RestartCount": 0,
        "Image": "sha256:1c5a898b0bdb5e92e5e260953302a5767ca6bee7724e089415dba82c6e8669ac",
        "State": state,
        "Config": {"Hostname": cid[:12], "Env": list(env or ["PUID=1000", "PGID=1000", "TZ=Europe/Lisbon", "PATH=/usr/bin"]),
                   "Cmd": None, "Entrypoint": ["/init"], "Healthcheck": None,
                   "Labels": {**{
                       "build_version": "Linuxserver.io version:- 2.7.0.5723-ls166",
                       "com.docker.compose.config-hash": config_hash,
                       "com.docker.compose.container-number": "1",
                       "com.docker.compose.depends_on": "",
                       "com.docker.compose.image": "sha256:1c5a898b0bdb5e92e5e260953302a5767ca6bee7724e089415dba82c6e8669ac",
                       "com.docker.compose.oneoff": "False",
                       "com.docker.compose.project": "media",
                       "com.docker.compose.project.config_files": config_files,
                       "com.docker.compose.project.working_dir": "/home/casaroot/stacks/media",
                       "com.docker.compose.replace": f"CASA_{service.upper()}",
                       "com.docker.compose.service": service,
                       "com.docker.compose.version": compose_version,
                       "traefik.enable": "true",
                       f"traefik.http.routers.{service}.rule": f"Host(`{service}.casalan.com`)"}, **(labels or {})}},
        "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
        "Mounts": [{"Type": "bind", "Source": f"/home/casaroot/apps/mediaserver/appdata/{service}/config", "Destination": "/config",
                    "Mode": "rw", "RW": True, "Propagation": "rprivate"}],
        "NetworkSettings": {"Networks": {
            "casalan": {"Aliases": [f"CASA_{service.upper()}", service], "NetworkID": "71297fc802f4", "EndpointID": endpoint,
                        "IPAddress": "172.22.0.15", "DNSNames": [f"CASA_{service.upper()}", service, cid[:12]]},
            "casaproxy": {"Aliases": [f"CASA_{service.upper()}", service], "NetworkID": "046d37ea2211", "EndpointID": endpoint[::-1],
                          "IPAddress": "172.20.0.25", "DNSNames": [f"CASA_{service.upper()}", service, cid[:12]]}}},
    }


def media_env(tmp_path):
    env = make_env(tmp_path)
    env.compose.write_text(MEDIA)
    env.dotenv.write_text(DOTENV)
    env.docker.containers = {("media", name): recorded(name, cid=f"{name}-1".ljust(64, "0"))
                             for name in ("prowlarr", "sonarr", "archivist-es")}
    env.docker.containers[("media", "archivist-es")]["State"]["Health"] = {"Status": "healthy"}
    env.docker.baseline = {k: json.loads(json.dumps(v)) for k, v in env.docker.containers.items()}
    return env


def recreate_same_config(env, name):
    """What `docker compose up -d --force-recreate <name>` leaves: a new container with the same configuration."""
    env.docker.containers[("media", name)] = recorded(name, cid=f"{name}-2".ljust(64, "0"), created="2026-11-04T11:59:34.145291505Z",
                                                      started="2026-11-04T11:59:45.843384820Z", endpoint="0964a0085118098d",
                                                      config_hash="d00dfeed00000000")
    if name == "archivist-es":
        env.docker.containers[("media", name)]["State"]["Health"] = {"Status": "healthy"}


# ── item 1: act only where something differs ────────────────────────────────────────────────────────
def test_m19_a_comment_in_dotenv_and_a_same_config_recreate_needs_no_service_action(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV + "\n# rc14 trust-mode test\n")              # what the session did
    recreate_same_config(env, "prowlarr")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok, report.unrestored
    assert report.written == [str(env.dotenv.resolve())]
    assert env.dotenv.read_text() == DOTENV
    assert env.docker.up_calls == [] and env.docker.restarted == [] and report.actions == {}
    # lidarr (profile, no container) was never named and never started
    assert ("media", "lidarr") not in env.docker.containers
    # and the cancel that follows sees the host as the checkpoint recorded it
    manifest = env.svc.manifest(cid)
    assert recovery.matches_checkpoint(recovery.collect_host_state(env.svc, manifest), manifest)


def test_a_service_whose_container_differs_is_brought_up_alone(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text(DOTENV.replace("TZ=Europe/Lisbon", "TZ=UTC"))
    env.docker.containers[("media", "prowlarr")]["Config"]["Env"] = ["PUID=1000", "PGID=1000", "TZ=UTC", "PATH=/usr/bin"]
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    # The fake resolves `.env` into every service, so the write changes every active service's config: each of THOSE is named
    # (Compose recreates only the ones whose hash changed), but never with --force-recreate, and never lidarr.
    assert report.ok, report.unrestored
    assert report.actions == {"up": ["media/archivist-es", "media/prowlarr", "media/sonarr"]}
    assert all("--force-recreate" not in call and "lidarr" not in call for call in env.docker.up_calls)


def test_only_a_service_whose_effective_config_the_restore_changes_is_named(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text(MEDIA.replace("image: lscr.io/linuxserver/sonarr:latest", "image: lscr.io/linuxserver/sonarr:develop"))
    env.svc.capture_post(cid)                                                  # edited, never brought up: containers unchanged
    report = env.svc.restore(cid)
    assert report.ok and report.actions == {"up": ["media/sonarr"]}


def test_a_service_stopped_since_the_checkpoint_is_started_and_others_are_untouched(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.docker.containers[("media", "sonarr")]["State"] = {"Status": "exited", "Running": False}
    env.svc.capture_post(cid)                                                  # no file changed
    report = env.svc.restore(cid)
    assert report.written == [] and report.actions == {"up": ["media/sonarr"]}
    assert report.ok, report.unrestored


def test_a_service_left_not_running_is_reported_with_its_state(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.docker.containers[("media", "sonarr")]["State"] = {"Status": "created", "Running": False}
    env.docker.fix_on_recreate = False                                         # `up` returns but the container never starts
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert {"service": "media/sonarr", "fields": ["state"], "reason": "created; it was running at the checkpoint"} in report.unrestored
    assert not report.ok


def test_an_unhealthy_service_with_the_right_config_is_restarted_not_recreated(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.docker.containers[("media", "archivist-es")]["State"]["Health"] = {"Status": "unhealthy"}
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.actions == {"restart": ["media/archivist-es"]} and env.docker.up_calls == []
    assert report.ok, report.unrestored


def test_a_missing_container_that_existed_is_brought_back(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    del env.docker.containers[("media", "sonarr")]
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.actions == {"up": ["media/sonarr"]} and report.ok


def test_a_compose_timeout_is_recorded_with_its_reason_and_reaches_the_handover(tmp_path):
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    del env.docker.containers[("media", "sonarr")]
    env.svc.capture_post(cid)
    real = env.docker.__call__

    def slow(argv, timeout):
        if "up" in argv:
            assert timeout == action_timeout(1)
            return 124, "", f"docker timed out after {timeout}s"           # what casa_bender.run_argv returns on a timeout
        return real(argv, timeout)
    env.provider.run = slow
    report = env.svc.restore(cid)
    reason = f"media: compose up timed out after {action_timeout(1)}s"
    assert {"service": "media/sonarr", "fields": ["up"], "reason": reason} in report.unrestored
    assert report.why() == f"media/sonarr: {reason}"
    done = json.loads([e for e in env.ms.events(env.mission.id) if '"checkpoint.restore_done"' in e.body_json][-1].body_json)
    assert any(u.get("reason") == reason for u in done["unrestored"])


def test_the_compose_timeout_grows_with_the_services_and_is_capped():
    assert action_timeout(1) == 150 and action_timeout(24) == 840
    assert action_timeout(200) == UP_MAX_TIMEOUT_S


def test_a_failure_reason_is_the_tail_of_what_compose_said_redacted(tmp_path):
    env = media_env(tmp_path)
    secret = "sk-ant-api03-" + "x" * 40

    def failing(argv, timeout):
        if "up" in argv:
            return 1, "", ("Container CASA_SONARR Recreate\n" * 50) + f"Error response from daemon: refused\nTOKEN={secret}"
        return env.docker(argv, timeout)
    env.provider.run = failing
    result = env.provider.up(["media/sonarr"])
    assert not result.ok and result.error.startswith("media: compose up exited 1: ")
    assert "Error response from daemon: refused" in result.error and secret not in result.error and len(result.error) < 400


# ── item 2: "recreated with the same config" matches ────────────────────────────────────────────────
def test_a_recreate_with_the_same_config_has_the_same_comparable_facts():
    before = ContainerFacts.from_inspect(recorded())
    after = ContainerFacts.from_inspect(recorded(cid="f" * 64, created="2026-11-04T11:59:34Z", started="2026-11-04T11:59:45Z",
                                                 endpoint="0964a0085118098d", config_hash="another",
                                                 compose_version="2.40.0", config_files="/srv/stacks/media/docker-compose.yml"))
    assert comparable_facts(before) == comparable_facts(after)


def test_real_configuration_still_differs():
    base = comparable_facts(ContainerFacts.from_inspect(recorded()))
    for changed in (recorded(env=["PUID=0"]), recorded(labels={"traefik.enable": "false"}),
                    {**recorded(), "Image": "sha256:" + "b" * 64},
                    {**recorded(), "HostConfig": {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}}}):
        assert comparable_facts(ContainerFacts.from_inspect(changed)) != base


def test_run_state_is_recorded_but_not_part_of_the_comparison():
    running, stopped = ContainerFacts.from_inspect(recorded()), ContainerFacts.from_inspect(recorded(status="exited"))
    assert running.state == {"status": "running", "health": None} and stopped.state["status"] == "exited"
    assert comparable_facts(running) == comparable_facts(stopped)


def test_cancel_matches_an_rc14_manifest_whose_labels_carry_compose_bookkeeping(tmp_path):
    # An rc14 manifest kept `com.docker.compose.version` and `project.config_files`; old v1 ones kept `config-hash` too.
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.svc.capture_post(cid)
    path = env.root / cid / "manifest.json"
    manifest = json.loads(path.read_text())
    for facts in manifest["facts"]["services"].values():
        if facts:
            facts["labels"].update({"com.docker.compose.version": "2.39.1", "com.docker.compose.config-hash": "old",
                                    "com.docker.compose.project.config_files": "/home/casaroot/stacks/media/docker-compose.yml"})
            facts.pop("state", None)
    path.write_text(json.dumps(manifest))
    recreate_same_config(env, "prowlarr")
    manifest = env.svc.manifest(cid)
    assert recovery.matches_checkpoint(recovery.collect_host_state(env.svc, manifest), manifest)


def test_cancel_still_refuses_a_container_the_checkpoint_did_not_have(tmp_path):
    # What actually held M-19's cancel: the rc14 rollback started lidarr, which had no container at the checkpoint.
    env = media_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    manifest = env.svc.manifest(cid)
    assert manifest["facts"]["services"]["media/lidarr"] is None
    env.docker.containers[("media", "lidarr")] = recorded("lidarr")
    assert not recovery.matches_checkpoint(recovery.collect_host_state(env.svc, manifest), manifest)


def test_a_service_stopped_at_the_checkpoint_is_put_back_stopped_after_up(tmp_path):
    """Review, rc14.1: `up` starts what it creates. A service deliberately stopped at the checkpoint ends stopped again."""
    env = media_env(tmp_path)
    env.docker.containers[("media", "sonarr")]["State"] = {"Status": "exited", "Running": False}
    env.docker.baseline = {k: json.loads(json.dumps(v)) for k, v in env.docker.containers.items()}
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text(MEDIA.replace("image: lscr.io/linuxserver/sonarr:latest", "image: lscr.io/linuxserver/sonarr:develop"))
    env.svc.capture_post(cid)
    env.docker.fix_on_recreate = False                       # `up` really starts it
    env.docker.containers[("media", "sonarr")]["State"] = {"Status": "running", "Running": True}
    report = env.svc.restore(cid)
    assert report.actions.get("up") == ["media/sonarr"] and report.actions.get("stop") == ["media/sonarr"]
    assert env.docker.stopped == ["media/sonarr"]
    assert env.docker.containers[("media", "sonarr")]["State"]["Status"] == "exited"


def test_a_service_stopped_at_the_checkpoint_but_running_after_is_reported():
    from planet_express.missions.checkpoint import service as svc_mod
    import inspect
    assert "it was {was.get('status')} at the checkpoint" in inspect.getsource(svc_mod)
