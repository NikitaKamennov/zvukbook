"""Build the offline ZIP and per-user installer, without running the application."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / '.local-data/windows-x64'
DOWNLOADS = WINDOWS / 'downloads'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', choices=('full', 'cpu'), default='full')
    args = parser.parse_args()
    cpu_only = args.edition == 'cpu'
    bundle = WINDOWS / ('dist-cpu/Zvukbook' if cpu_only else 'dist/Zvukbook')
    release = WINDOWS / ('release-cpu' if cpu_only else 'release')
    staging = WINDOWS / ('release-cpu-staging' if cpu_only else 'release-staging')
    product = 'Zvukbook-CPU' if cpu_only else 'Zvukbook'
    setup_name, zip_name = product + '-Setup.exe', product + '-Portable.zip'
    readme = 'windows-cpu-portable-readme.txt' if cpu_only else 'windows-portable-readme.txt'
    version = json.loads((ROOT / 'config/windows-release.json').read_text())['version']
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('edition', 'full') != args.edition:
        raise RuntimeError('Bundle edition does not match the requested release.')
    if cpu_only:
        configuration = json.loads((bundle / 'config/windows-desktop.json').read_text(encoding='utf-8'))
        if (set(manifest.get('runtime', {})) != {'cpu'} or manifest.get('available_devices') != ['cpu']
                or configuration.get('edition') != 'cpu' or configuration.get('device') != 'cpu'
                or configuration.get('available_devices') != ['cpu'] or (bundle / 'runtime/cuda').exists()):
            raise RuntimeError('CPU release requires a CPU-only bundle and configuration; nothing was removed.')
    if digest(bundle / 'Zvukbook.exe') != manifest['exe_sha256']:
        raise RuntimeError('Build the current bundle first.')
    if shutil.disk_usage(WINDOWS).free < 8 * 1024 ** 3:
        raise OSError('Keep at least 8 GiB free while preparing release archives.')
    native = runpy.run_path(str(Path(__file__).with_name('prepare_native.py')))
    compiler = native['extract_compiler'](DOWNLOADS, WINDOWS / 'build-tools')
    release.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    partial = staging / (zip_name + '.part')
    print(f'Packing {args.edition} portable ZIP...', flush=True)
    with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(bundle.rglob('*')):
            if path.is_symlink() or path.is_junction():
                raise RuntimeError('A release must not contain symlinks/junctions: ' + str(path))
            if path.is_file():
                archive.write(path, product + '/' + path.relative_to(bundle).as_posix())
    print('Building offline installer...', flush=True)
    subprocess.run([str(compiler), '/Qp', '/DBundleDir=' + str(bundle), '/DReleaseDir=' + str(staging),
                    '/DAppVersion=' + version, '/DEdition=' + args.edition,
                    str(ROOT / 'desktop/installer/zvukbook.iss')], check=True)
    # Publish the new pair only after both packaging operations succeeded.
    os.replace(partial, release / zip_name)
    os.replace(staging / setup_name, release / setup_name)
    shutil.copy2(ROOT / 'docs' / readme, release / 'README.txt')
    files = {name: {'sha256': digest(release/name), 'bytes': (release/name).stat().st_size}
             for name in (setup_name, zip_name)}
    (release / 'SHA256SUMS.txt').write_text(''.join(value['sha256'] + '  ' + name + '\n' for name,value in files.items()), encoding='ascii')
    (release / 'release-manifest.json').write_text(json.dumps({'schema': 'zvukbook-release-v1', 'version': version, 'edition': args.edition,
        'bundle': manifest, 'files': files, 'acceptance': 'manual, not run by the build'}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Release ready: ' + str(release), flush=True)
    for name, value in files.items():
        print(f"  {name}: {value['bytes'] / 1024 ** 2:.1f} MiB", flush=True)


if __name__ == '__main__':
    main()
