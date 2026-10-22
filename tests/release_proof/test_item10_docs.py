"""Release proof item 10:

    Setup, repair, undo, uninstall, upgrade and docs cover every worker, account, socket, secret, grant and boot-time component
    v4 adds. A v4-to-v3 downgrade is documented (snapshot restore).

Setup, repair and undo are the launcher steps (tested in tests/test_launcher_setup_steps.py); this module checks the
documentation names each of them and the other components, and that the example config block is valid."""
import re

import yaml

from config_schema import MissionsConfig
from planet_express.missions.launcher.setup_steps import Paths, launcher_steps
from release_proof.support import ROOT

INSTALL = (ROOT / "INSTALL.md").read_text()
SECTION = INSTALL[INSTALL.index("## Mission control (v4"):INSTALL.index("## What this does not cover")]


def test_every_launcher_step_is_named_in_the_install_guide():
    paths = Paths(core_uid=1000, core_gid=1000, core_user="planetexpress", flavour="systemd", uid_min=61000, uid_max=61999,
                  sock_root="/run/planetexpress-sessions", cgroup_root="/sys/fs/cgroup/x/sessions",
                  workspace_root="/var/lib/planetexpress/mission-workspaces")
    names = {s.name for s in launcher_steps(paths)}
    assert {"session-uid-range", "launcher-helper", "launcher-sudoers", "cgroup-tree", "workspace-root",
            "socket-root"} <= names
    for name in names | {"worker-sudoers"}:
        assert f"`{name}`" in SECTION, name


def test_the_guide_covers_each_component_and_the_lifecycle():
    for needle in ("missions.enabled", "session_uid_min", "pe-launch", "launch.conf", "/etc/sudoers.d/planetexpress-launch",
                   "Delegate=yes", "launcher-boot.sh", "worker.sock", "proxy.sock", "tool_secret_env", "Standing grants",
                   "chown root:root /mnt/data", "Upgrading to v4", "Downgrading from v4 to v3", "snapshot restore",
                   "Uninstalling mission control", "scripts/state_snapshot.py restore", "mission_containment_rehearsal.py"):
        assert needle in SECTION, needle


def test_the_changelog_records_v4_under_unreleased_with_the_schema_warning():
    text = (ROOT / "CHANGELOG.md").read_text()
    unreleased = text[text.index("## [Unreleased]"):text.index("## [3.1.2]")]
    assert "mission control" in unreleased and "snapshot" in unreleased and "5 to 6" in unreleased


def test_the_example_configs_missions_block_is_valid_when_uncommented():
    lines = (ROOT / "config.example.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# missions:"))
    block = [re.sub(r"^# ?", "", line) for line in lines[start:] if line.startswith("#")]
    doc = yaml.safe_load("\n".join(block))["missions"]
    doc["enabled"] = False                       # enabling also needs a real cgroup v2 mount; the keys are what is checked
    cfg = MissionsConfig.model_validate(doc)
    assert cfg.runtime.argv and cfg.session_uid_min == 61000 and cfg.tripwire_interval_s == 15
    assert cfg.verify.exec_allowlist == [] and cfg.checkpoint.backup_sources == []


def test_the_rehearsal_doc_has_the_commands_for_both_platforms():
    text = (ROOT / "docs/handoff/v4/rehearsals.md").read_text()
    for needle in ("## Ubuntu / systemd", "## MOS / sysvinit", "scripts/run_rehearsal.sh --vm HOST",
                   "scripts/run_privileged_tests.sh --vm HOST", "mission_containment_rehearsal.py", "--flavour mos"):
        assert needle in text, needle
