import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import Store
from scripts.revoke_devices import main


def test_main(tmp_path, capsys):
    store = Store(tmp_path / 'data' / 'core.db')
    assert main(['alice'], store_factory=lambda path: store) == 0
    assert store.device_epoch('alice') == 1
    output = capsys.readouterr().out
    assert '1' in output and 'must log in again' in output


def test_invalid_name():
    def forbidden(path):
        raise AssertionError('must validate before opening the store')
    for args in ([], ['?'], ['Alice'], ['a/b'], ['a', 'b']):
        assert main(args, store_factory=forbidden) == 2
