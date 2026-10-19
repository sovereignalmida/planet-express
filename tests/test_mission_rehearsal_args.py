"""scripts/mission_containment_rehearsal.py: where it looks for launch.conf."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("mission_containment_rehearsal", ROOT / "scripts/mission_containment_rehearsal.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_explicit_launch_conf_wins():
    assert load().resolve_launch_conf("/x/launch.conf", env={}) == "/x/launch.conf"


def test_without_the_flag_it_follows_the_configs_missions_launch_conf(monkeypatch):
    from planet_express.missions.launcher.config import CONF_PATH
    import config_io
    from types import SimpleNamespace
    monkeypatch.setattr(config_io, "load_config_file",
                        lambda path: SimpleNamespace(missions=SimpleNamespace(launch_conf="/mnt/data/pe/launch/launch.conf")))
    module = load()
    assert module.resolve_launch_conf(None, env={"CASA_CONFIG": "/anything"}) == "/mnt/data/pe/launch/launch.conf"
    assert CONF_PATH != "/mnt/data/pe/launch/launch.conf"


def test_an_unloadable_config_falls_back_to_the_fixed_default(tmp_path):
    from planet_express.missions.launcher.config import CONF_PATH
    assert load().resolve_launch_conf(None, env={"CASA_CONFIG": str(tmp_path / "absent.yaml")}) == CONF_PATH


def test_the_flag_exists_and_reaches_the_preflight():
    text = (ROOT / "scripts/mission_containment_rehearsal.py").read_text()
    assert '"--launch-conf"' in text and "conf=resolve_launch_conf(args.launch_conf)" in text
