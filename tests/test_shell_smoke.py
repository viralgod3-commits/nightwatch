import os
from pathlib import Path
import subprocess
import sys


def test_shell_navigation_and_cooperative_shutdown():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root) + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, str(root / 'tests' / 'shell_smoke.py')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"shutdown": "passed"' in result.stdout
