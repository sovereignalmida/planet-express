import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_reexec_preserves_pid_and_environment(tmp_path):
    script = tmp_path / 'restart.py'
    script.write_text('''import os
from planet_express.core.reexec import reexec
print(os.getpid(), os.environ.get("REEXEC_TEST_MARKER", "first"))
if "REEXEC_TEST_MARKER" not in os.environ:
    os.environ["REEXEC_TEST_MARKER"] = "second"
    reexec()
''')
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    env.pop('REEXEC_TEST_MARKER', None)
    result = subprocess.run([sys.executable, str(script)], env=env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    first, second = [line.split() for line in result.stdout.splitlines()]
    assert first[0] == second[0]
    assert first[1] == 'first'
    assert second[1] == 'second'
