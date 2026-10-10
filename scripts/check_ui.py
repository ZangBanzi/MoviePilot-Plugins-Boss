"""Launch a fresh loopback QA host for each browser regression suite."""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.work'


def main():
    WORK.mkdir(exist_ok=True)
    node = os.environ.get('MEDIAARCHIVER_NODE', 'node')
    for suite in ('ui_virtual_library.mjs',):
        port_file = WORK / 'preview-port.txt'
        port_file.unlink(missing_ok=True)
        with (WORK / f'{suite}.server.log').open('w') as log:
            host = subprocess.Popen([sys.executable, 'tests/preview_server.py'], cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if host.poll() is not None:
                        raise RuntimeError(f'QA host failed: {suite}')
                    if port_file.exists():
                        address = f'http://127.0.0.1:{port_file.read_text().strip()}'
                        try:
                            # The plugin gateway deliberately owns '/'; a file
                            # path exercises StaticFiles without the Emby route.
                            with urllib.request.urlopen(address + '/index.html', timeout=1) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('QA host did not become ready')
                subprocess.run([node, f'tests/{suite}'], cwd=ROOT, check=True, timeout=240)
            finally:
                host.terminate()
                try:
                    host.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    host.kill()
                    host.wait()


if __name__ == '__main__':
    main()
