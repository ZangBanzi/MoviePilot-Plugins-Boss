"""Run pytest and the legacy regressions in separate, reproducible processes."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    commands = [[sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']]
    commands.extend([sys.executable, str(ROOT / 'tests' / script)] for script in (
        'test_virtual_library.py', 'test_accuracy.py', 'test_animated_cover.py', 'test_ranking_failures.py'))
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
