"""Deterministic release ZIP and SHA256 manifest; never packages local data."""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_files():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    return sorted({name.decode() for name in names.split(b'\0') if name and
                   name.decode() != 'SHA256SUMS' and (ROOT / name.decode()).is_file()})


def manifest(files):
    return ''.join(f'{hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}  {name}\n' for name in files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    files = source_files()
    text = manifest(files)
    checksum = ROOT / 'SHA256SUMS'
    if args.check:
        if not checksum.is_file() or checksum.read_text() != text:
            raise SystemExit('SHA256SUMS 与当前完整文件清单不一致，请重新打包')
        print(f'校验通过：{len(files)} 个文件')
        return
    version = json.loads((ROOT / 'package.v2.json').read_text())['MediaArchiver']['version']
    for name, pattern in [('plugins.v2/mediaarchiver/__init__.py', r'plugin_version = "([^"]+)"')]:
        match = re.search(pattern, (ROOT / name).read_text())
        if not match or match[1] != version:
            raise SystemExit(f'版本不一致：{name}')
    if json.loads((ROOT / 'frontend/package.json').read_text())['version'] != version:
        raise SystemExit('前端版本不一致')
    checksum.write_text(text)
    output = ROOT / 'releases' / f'MediaArchiver-v{version}.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files + ['SHA256SUMS']:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / name).read_bytes())
    with zipfile.ZipFile(output) as archive:
        for name in files:
            if hashlib.sha256(archive.read(name)).hexdigest() != hashlib.sha256((ROOT / name).read_bytes()).hexdigest():
                raise SystemExit(f'发布包校验失败：{name}')
    print(f'发布包：{output}\n完整校验通过：{len(files)} 个文件')


if __name__ == '__main__':
    main()
