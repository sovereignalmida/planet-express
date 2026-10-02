"""Boot recovery uses fake Docker argv calls; no test talks to a daemon."""

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_boot
import config

ID_A = "a" * 64
ID_B = "b" * 64
DEAD_ID = "c" * 64


def _row(
    container_id=ID_A,
    name="CASA_TEST",
    network_mode="default",
    config_files="",
    service="",
    one_off="",
    state="running",
    exit_code="0",
):
    return "\t".join((
        container_id, f"/{name}", network_mode, config_files, service, one_off, state, exit_code,
    ))


class FakeDocker:
    def __init__(self, compose_returns, ids="", inspect_outputs=(), declared_services=None):
        self.compose_returns = {key: list(value) for key, value in compose_returns.items()}
        self.ids = ids
        self.inspect_outputs = list(inspect_outputs)
        # inspect_outputs is CONSUMED by pop(0), so keep an unconsumed copy: the services
        # query runs after the container read has already drained it.
        self._all_inspect = list(inspect_outputs)
        # None means "every service these containers name is still declared", which is the
        # normal case and keeps a test from having to restate the compose file it already
        # described. A set says exactly which services survive -- that is the retired-service
        # case, where a container still carries an ACTIVE file's label.
        self.declared_services = declared_services
        self.calls = []

    def _declared(self):
        if self.declared_services is not None:
            return self.declared_services
        seen = set()
        for block in self._all_inspect:
            for line in block.splitlines():
                fields = line.split("\t")
                if len(fields) == 8 and fields[4]:
                    seen.add(fields[4])
        return seen

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:2] == ["docker", "compose"] and argv[-2:] == ["config", "--services"]:
            return SimpleNamespace(
                returncode=0, stdout="".join(f"{name}\n" for name in sorted(self._declared())),
                stderr="",
            )
        if argv[:2] == ["docker", "compose"]:
            compose_file = argv[argv.index("-f") + 1]
            return SimpleNamespace(returncode=self.compose_returns[compose_file].pop(0))
        if argv[:3] == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=0, stdout=self.ids, stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0, stdout=self.inspect_outputs.pop(0), stderr="",
            )
        raise AssertionError(f"unexpected argv: {argv}")


def _stack(tmp_path, monkeypatch, name="media"):
    stack = tmp_path / name
    stack.mkdir()
    compose_file = stack / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(config, "active_stack_dirs", lambda: [stack])
    return stack, str(compose_file)


def _install(monkeypatch, fake):
    monkeypatch.setattr(casa_boot.subprocess, "run", fake)
    return fake


def _force_recreates(fake):
    return [argv for argv, _ in fake.calls if "--force-recreate" in argv]


def test_dead_namespace_reference_is_force_recreated(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    row = _row(
        name="CASA_QBIT",
        network_mode=f"container:{DEAD_ID}",
        config_files=compose_file,
        service="qbittorrent",
    )
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0, 0]}, f"{ID_A}\n", [f"{row}\n", f"{row}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 0
    assert _force_recreates(fake) == [[
        "docker", "compose", "-f", compose_file, "up", "-d", "--force-recreate", "qbittorrent",
    ]]


def test_live_namespace_reference_is_left_alone(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    dependent = _row(
        name="CASA_QBIT", network_mode=f"container:{ID_B[:12]}", config_files=compose_file,
        service="qbittorrent",
    )
    target = _row(container_id=ID_B, name="CASA_GLUETUN")
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n{ID_B}\n", [f"{dependent}\n{target}\n"] * 2,
    ))

    assert casa_boot.bring_up_all_stacks() == 0
    assert not _force_recreates(fake)


def test_failed_stack_then_successful_retry_is_success(tmp_path, monkeypatch, capsys):
    _, compose_file = _stack(tmp_path, monkeypatch)
    _install(monkeypatch, FakeDocker({compose_file: [1, 0]}, "", ["", ""]))

    assert casa_boot.bring_up_all_stacks() == 0
    assert "Retrying failed stacks once: media" in capsys.readouterr().out


def test_failed_retry_exits_nonzero_and_names_down_container(tmp_path, monkeypatch, capsys):
    _, compose_file = _stack(tmp_path, monkeypatch)
    down = _row(
        name="CASA_AIRWAVE_DB", config_files=compose_file, service="database",
        state="exited", exit_code="1",
    )
    _install(monkeypatch, FakeDocker(
        {compose_file: [1, 1]}, f"{ID_A}\n", [f"{down}\n", f"{down}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 1
    output = capsys.readouterr().out
    assert "CASA_AIRWAVE_DB (state exited, exit 1)" in output
    assert "retry failed" in output


def test_created_container_is_reported(tmp_path, monkeypatch, capsys):
    _, compose_file = _stack(tmp_path, monkeypatch)
    created = _row(
        name="CASA_IMMICH_SERVER", config_files=compose_file, service="server", state="created",
    )
    _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n", [f"{created}\n", f"{created}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 1
    assert "CASA_IMMICH_SERVER (state created, exit 0)" in capsys.readouterr().out


def test_dead_namespace_without_compose_labels_does_not_crash(monkeypatch, capsys):
    row = _row(name="CASA_UNMANAGED", network_mode=f"container:{DEAD_ID}")
    fake = _install(monkeypatch, FakeDocker({}, f"{ID_A}\n", [f"{row}\n"]))

    assert casa_boot.repair_dead_namespace_references() == 0
    assert not _force_recreates(fake)
    assert "Skipping CASA_UNMANAGED" in capsys.readouterr().out


def test_all_healthy_path_performs_zero_recreates(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    healthy = _row(config_files=compose_file, service="test")
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n", [f"{healthy}\n", f"{healthy}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 0
    assert not _force_recreates(fake)


def test_slow_dry_run_does_not_shrink_the_real_boot_timeout(tmp_path, monkeypatch):
    """A codex-review regression test: the deadline used for real docker compose calls must be
    computed *after* the dependency-graph dry run, so however long that diagnostic takes, it
    cannot eat into the budget the real retry/verify path gets."""
    _, compose_file = _stack(tmp_path, monkeypatch)
    healthy = _row(config_files=compose_file, service="test")
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n", [f"{healthy}\n", f"{healthy}\n"],
    ))

    clock = {"t": 1_000.0}
    monkeypatch.setattr(casa_boot.time, "monotonic", lambda: clock["t"])

    def slow_summarize(stacks):
        clock["t"] += 200.0  # simulate a dry run that took 200s
        return ["[dry-run graph] simulated slow pass"]

    monkeypatch.setattr(casa_boot.dependency_dryrun, "summarize", slow_summarize)

    assert casa_boot.bring_up_all_stacks() == 0
    up_timeouts = [
        kwargs["timeout"] for argv, kwargs in fake.calls
        if argv[:2] == ["docker", "compose"] and "up" in argv and "timeout" in kwargs
    ]
    assert up_timeouts and min(up_timeouts) > casa_boot.BOOT_TIMEOUT_SECONDS - 5


def test_dead_reference_from_an_inactive_stack_is_not_recreated(tmp_path, monkeypatch):
    active, _ = _stack(tmp_path, monkeypatch)
    inactive_file = str(tmp_path / "retired" / "docker-compose.yml")
    row = _row(
        network_mode=f"container:{DEAD_ID}", config_files=inactive_file, service="retired",
    )
    fake = _install(monkeypatch, FakeDocker({}, f"{ID_A}\n", [f"{row}\n"]))

    assert casa_boot.repair_dead_namespace_references(
        active_compose_files={str((active / "docker-compose.yml").resolve())},
    ) == 0
    assert not _force_recreates(fake)


def test_inactive_down_container_does_not_fail_active_boot(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    inactive = _row(
        name="CASA_RETIRED", config_files=str(tmp_path / "retired" / "docker-compose.yml"),
        service="retired", state="exited", exit_code="1",
    )
    healthy = _row(config_files=compose_file, service="test")
    _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n{ID_B}\n", [f"{inactive}\n{healthy}\n"] * 2,
    ))

    assert casa_boot.bring_up_all_stacks() == 0


def test_paused_container_is_not_recreated_or_reported_down(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    paused = _row(
        name="CASA_OFF", network_mode=f"container:{DEAD_ID}", config_files=compose_file,
        service="off", state="exited", exit_code="1",
    )
    monkeypatch.setattr(config, "PAUSED_CONTAINERS", ["CASA_OFF"])
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n", [f"{paused}\n", f"{paused}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 0
    assert not _force_recreates(fake)


def test_compose_one_off_is_not_recreated_or_reported_down(tmp_path, monkeypatch):
    _, compose_file = _stack(tmp_path, monkeypatch)
    one_off = _row(
        name="CASA_MIGRATION_RUN", network_mode=f"container:{DEAD_ID}",
        config_files=compose_file, service="migration", one_off="True", state="exited", exit_code="1",
    )
    fake = _install(monkeypatch, FakeDocker(
        {compose_file: [0]}, f"{ID_A}\n", [f"{one_off}\n", f"{one_off}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 0
    assert not _force_recreates(fake)


def test_failed_namespace_recreate_makes_boot_fail(tmp_path, monkeypatch, capsys):
    _, compose_file = _stack(tmp_path, monkeypatch)
    stale = _row(
        network_mode=f"container:{DEAD_ID}", config_files=compose_file, service="qbittorrent",
    )
    healthy = _row(config_files=compose_file, service="qbittorrent")
    _install(monkeypatch, FakeDocker(
        {compose_file: [0, 1]}, f"{ID_A}\n", [f"{stale}\n", f"{healthy}\n"],
    ))

    assert casa_boot.bring_up_all_stacks() == 1
    assert "Failed namespace recreates: 1" in capsys.readouterr().out


def test_a_service_deleted_from_an_ACTIVE_compose_file_does_not_fail_the_boot():
    """The Lidarr case, and the one the first version got wrong.

    A container keeps the compose labels it was created with. Retiring a service by deleting
    it from an otherwise live compose file leaves a stopped container still carrying that
    ACTIVE file's path -- so it passes _belongs_to_active_stack and the final check called it
    down, failing casa-stacks.service on every boot. The earlier test covered a container
    from an inactive STACK, which is a different and easier case: its file is not active at
    all. Here the file is active and only the SERVICE is gone.
    """

    import casa_boot

    class _Fake(FakeDocker):
        pass

    retired = _row(name="CASA_LIDARR", service="lidarr", state="exited", exit_code="0")
    live = _row(service="test")
    fake = _Fake(
        {None: []}, f"{ID_A}\n{ID_B}\n", [f"{retired}\n{live}\n"] * 2,
        declared_services={"test"},          # lidarr is gone from the file
    )
    assert "lidarr" not in fake._declared()
    assert casa_boot._is_orphan_of_removed_service(
        casa_boot.Container("i", "CASA_LIDARR", "", "", "lidarr", "", "exited", "0"),
        {"test"},
    )
    # and a service that IS still declared is never treated as an orphan
    assert not casa_boot._is_orphan_of_removed_service(
        casa_boot.Container("i", "CASA_TEST", "", "", "test", "", "exited", "1"),
        {"test"},
    )


def test_unreadable_service_list_does_not_silently_ignore_containers():
    """Unreadable is not empty. If the declared set cannot be read, nothing is an orphan."""
    import casa_boot
    assert not casa_boot._is_orphan_of_removed_service(
        casa_boot.Container("i", "CASA_LIDARR", "", "", "lidarr", "", "exited", "0"), None,
    )
