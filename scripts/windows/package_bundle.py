"""Prepare isolated Windows dependencies and assemble a repeatable folder bundle."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / ".local-data/windows-x64"
DOWNLOADS = WINDOWS / "downloads"
MODEL_SHA = "04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e"
NATIVE = runpy.run_path(str(Path(__file__).with_name('prepare_native.py')))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments):
    subprocess.run([str(arg) for arg in arguments], check=True)


def remove_owned(path):
    expected = path.absolute()
    resolved = path.resolve()
    if (not resolved.is_relative_to(WINDOWS.resolve()) or resolved == WINDOWS.resolve()
            or path.is_symlink() or path.is_junction() or resolved != expected):
        raise ValueError(f"Refusing to remove an unexpected target: {path}")
    if path.exists():
        shutil.rmtree(path)


def fetch_all(include_release_tools=False):
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "config/windows-downloads.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        if record.get("purpose") == "release" and not include_release_tools:
            continue
        target = DOWNLOADS / record["file"]
        if target.is_file() and sha(target) == record["sha256"]:
            continue
        if target.exists():
            raise ValueError(f"Existing download checksum mismatch: {target}")
        partial = target.with_name(target.name + ".part")
        print("Downloading", record["file"], flush=True)
        run(["curl.exe", "--fail", "--location", "--silent", "--show-error", "--user-agent", "Wget/1.21.4",
             "--output", partial, record["url"]])
        if sha(partial) != record["sha256"]:
            raise ValueError(f"Download SHA mismatch: {record['file']}; no extraction was attempted")
        os.replace(partial, target)
    return manifest


def prepare_environments(devices):
    signatures = {}
    for device in devices:
        signature = {name: sha(ROOT / name) for name in (
            "requirements/windows-probe/common.lock", f"requirements/windows-probe/{device}.lock", "requirements/windows-app.lock")}
        signatures[device] = signature
        environment = WINDOWS / "probe" / ("runtime-" + device)
        marker = environment / "build-ready.json"
        if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")) == signature:
            continue
        if not environment.exists():
            run([sys.executable, "-I", "-m", "venv", environment])
        python = environment / "Scripts/python.exe"
        for lock in (ROOT / f"requirements/windows-probe/{device}.lock", ROOT / "requirements/windows-app.lock"):
            expected = {}
            files = [lock]
            if "windows-probe" in str(lock):
                files.append(ROOT / "requirements/windows-probe/common.lock")
                expected["torch"] = "2.11.0+" + ("cpu" if device == "cpu" else "cu128")
            for requirement in files:
                for line in requirement.read_text(encoding="utf-8").splitlines():
                    match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ]+)", line)
                    if match:
                        expected[match[1]] = match[2]
            check = subprocess.run([str(python), "-I", "-c",
                                    "import importlib.metadata as m,json,sys; "
                                    "p=json.loads(sys.argv[1]); "
                                    "sys.exit(0 if all(m.version(k)==v for k,v in p.items()) else 1)",
                                    json.dumps(expected)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode == 0:
                continue
            run([python, "-I", "-m", "pip", "--isolated", "install", "--no-input", "--no-cache-dir",
                 "--only-binary=:all:", "--require-hashes", "--index-url", "https://pypi.org/simple", "-r", lock])
        marker.write_text(json.dumps(signature, indent=2), encoding="utf-8")
    return signatures


def extract_tools():
    seven = DOWNLOADS / "7zip/7z.exe"
    if not seven.is_file():
        run([DOWNLOADS / "7zr.exe", "x", DOWNLOADS / "7z2605-x64.exe", "-o" + str(seven.parent), "-y"])
    for filename, folder, expected in (
        ("tesseract-5.5.3-github.exe", "tesseract-extracted", "tesseract.exe"),
        ("djvulibre-3.5.29-curl.exe", "djvulibre-extracted", "ddjvu.exe"),
    ):
        target = DOWNLOADS / folder
        if not (target / expected).is_file():
            run([seven, "x", DOWNLOADS / filename, "-o" + str(target), "-y"])


def copy_tree(source, target, ignore=None):
    if target.exists():
        remove_owned(target)
    shutil.copytree(source, target, ignore=ignore)


def assemble(signatures, executable, downloads_manifest, edition):
    bundle = WINDOWS / ("dist-cpu/Zvukbook" if edition == "cpu" else "dist/Zvukbook")
    devices = ("cpu",) if edition == "cpu" else ("cpu", "cuda")
    bundle.mkdir(parents=True, exist_ok=True)
    minimum_gib = 8 if edition == "cpu" else 15
    if shutil.disk_usage(WINDOWS).free < minimum_gib * 1024 ** 3:
        raise OSError(f"Keep at least {minimum_gib} GiB free while assembling the {edition} bundle.")
    if edition == "cpu" and (bundle / "runtime/cuda").exists():
        raise RuntimeError("The CPU output contains a CUDA runtime. Use a clean dist-cpu directory; nothing was removed.")
    for device in devices:
        target = bundle / "runtime" / device
        signature = dict(signatures[device], python_embed_sha256=downloads_manifest["files"][0]["sha256"])
        marker = target / "runtime-ready.json"
        if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")) == signature:
            continue
        if target.exists():
            remove_owned(target)
        target.mkdir(parents=True)
        with zipfile.ZipFile(DOWNLOADS / "python-3.12.10-embed-amd64.zip") as archive:
            archive.extractall(target)
        source = WINDOWS / "probe" / ("runtime-" + device) / "Lib/site-packages"
        shutil.copytree(source, target / "Lib/site-packages", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "pip", "pip-*.dist-info"))
        (target / "python312._pth").write_text("python312.zip\n.\nLib\\site-packages\n..\\..\\app\nimport site\n", encoding="utf-8")
        marker.write_text(json.dumps(signature, indent=2), encoding="utf-8")
    staging = bundle / ("app.staging-" + uuid4().hex)
    staging.mkdir()
    shutil.copytree(ROOT / "book_renderer", staging / "book_renderer", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    current_app = bundle / "app"
    if current_app.exists():
        remove_owned(current_app)
    staging.rename(current_app)
    model = bundle / "models/silero-v5_6_ru"
    model.mkdir(parents=True, exist_ok=True)
    if not (model / "model.pt").is_file():
        source = ROOT / ".local-data/books-renderer/models/silero-v5_6_ru"
        if sha(source / "model.pt") != MODEL_SHA:
            raise ValueError("Existing Silero model checksum mismatch; nothing was downloaded or replaced")
        for name in ("model.pt", "LICENSE", "manifest.json"):
            shutil.copy2(source / name, model / name)
    if sha(model / "model.pt") != MODEL_SHA:
        raise ValueError("Bundle Silero model checksum mismatch")
    tools = bundle / "tools"
    tools.mkdir(exist_ok=True)
    browser = tools / 'webview2'
    browser_source = NATIVE['browser_folder'](DOWNLOADS)
    browser_marker = browser / 'zvukbook-browser-version.txt'
    if not browser_marker.is_file() or browser_marker.read_text() != '156.0.4314.58':
        copy_tree(browser_source, browser)
        browser_marker.write_text('156.0.4314.58')
    crt = NATIVE['extract_crt'](DOWNLOADS, DOWNLOADS / '7zip/7z.exe')
    if not (tools / "ffmpeg/bin/ffmpeg.exe").is_file():
        extracted = DOWNLOADS / "ffmpeg-for-bundle"
        if not extracted.exists():
            with zipfile.ZipFile(DOWNLOADS / "ffmpeg-release-essentials.zip") as archive:
                archive.extractall(extracted)
        candidates = list(extracted.iterdir())
        if len(candidates) != 1:
            raise ValueError("Unexpected FFmpeg archive layout")
        copy_tree(candidates[0], tools / "ffmpeg")
    if not (tools / "tesseract/tesseract.exe").is_file():
        copy_tree(DOWNLOADS / "tesseract-extracted", tools / "tesseract",
                  ignore=shutil.ignore_patterns("$PLUGINSDIR", "*uninstall*", "*.traineddata"))
    shutil.copy2(DOWNLOADS / "rus-4.1.0.traineddata", tools / "tesseract/tessdata/rus.traineddata")
    if not (tools / "djvulibre/ddjvu.exe").is_file():
        copy_tree(DOWNLOADS / "djvulibre-extracted", tools / "djvulibre",
                  ignore=shutil.ignore_patterns("$PLUGINSDIR", "*uninstall*", "djview.exe"))
    # DOC was deferred by the owner; remove only the obsolete Windows bundle tool.
    if (tools / "libreoffice").exists():
        remove_owned(tools / "libreoffice")
    for destination in [bundle / 'runtime' / device for device in devices] + [tools / 'tesseract', tools / 'djvulibre']:
        for dll in sorted(crt.glob('*.dll')):
            shutil.copy2(dll, destination / dll.name)
    (bundle / 'licenses').mkdir(exist_ok=True)
    shutil.copytree(crt, bundle / 'licenses/msvc', dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.dll'))
    readme = 'windows-cpu-portable-readme.txt' if edition == 'cpu' else 'windows-portable-readme.txt'
    shutil.copy2(ROOT / 'docs' / readme, bundle / 'START-HERE.txt')
    shutil.copy2(ROOT / 'LICENSE', bundle / 'LICENSE')
    (bundle / "config").mkdir(exist_ok=True)
    configuration = json.loads((ROOT / "config/windows-desktop.json").read_text(encoding="utf-8"))
    configuration.update(edition=edition, available_devices=list(devices), device="cpu")
    (bundle / "config/windows-desktop.json").write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "docs/windows-build.md", bundle / "README-Windows.md")
    (bundle / "THIRD-PARTY.md").write_text(
        "# Bundled components\n\n"
        "Zvukbook application code: MIT; see LICENSE in the bundle root.\n\n"
        "Python: runtime/*/LICENSE.txt. PyTorch and Python packages: runtime/*/Lib/site-packages, package dist-info licenses.\n\n"
        "Silero model: models/silero-v5_6_ru/LICENSE and manifest.json; original weights are unchanged.\n\n"
        "FFmpeg: tools/ffmpeg/LICENSE and README. This is the Gyan essentials GPL build with libmp3lame.\n\n"
        "Tesseract: tools/tesseract/doc and associated notices (Apache-2.0); Russian tessdata_fast 4.1.0 (Apache-2.0).\n\n"
        "DjVuLibre: tools/djvulibre/COPYING.txt and doc (GPL).\n\n"
        "Microsoft Visual C++ 14.44.35211: app-local DLLs; licenses/msvc/LICENSE.rtf.\n\n"
        "Microsoft WebView2 Fixed Version 156.0.4314.58: tools/webview2 and its included notices. No global runtime installation.\n\n"
        "Go/Wails dependency versions: locks/go.mod and locks/go.sum. Wails: MIT.\n\n"
        "Downloads and checksums: locks/windows-downloads.json; Python wheels: locks/*.lock.\n",
        encoding="utf-8")
    (bundle / "locks").mkdir(exist_ok=True)
    for lock in ("common", *devices):
        shutil.copy2(ROOT / f"requirements/windows-probe/{lock}.lock", bundle / "locks" / (lock + ".lock"))
    shutil.copy2(ROOT / "requirements/windows-app.lock", bundle / "locks/windows-app.lock")
    shutil.copy2(ROOT / "config/windows-downloads.json", bundle / "locks/windows-downloads.json")
    for name in ("go.mod", "go.sum"):
        shutil.copy2(ROOT / "desktop" / name, bundle / "locks" / name)
    partial_exe = bundle / "Zvukbook.exe.part"
    shutil.copy2(executable, partial_exe)
    os.replace(partial_exe, bundle / "Zvukbook.exe")
    manifest = {"schema": "zvukbook-windows-bundle-v1", "built_utc": datetime.now(timezone.utc).isoformat(),
                "model_id": "v5_6_ru", "model_sha256": MODEL_SHA, "default_device": "cpu", "device_selection_required": True,
                "edition": edition, "available_devices": list(devices),
                "runtime": signatures, "exe_sha256": sha(bundle / "Zvukbook.exe"),
                "application_files": {str(path.relative_to(bundle)).replace("\\", "/"): sha(path)
                                      for path in sorted(current_app.rglob("*")) if path.is_file()},
                "native_files": {str(path.relative_to(bundle)).replace('\\','/'):sha(path)
                                 for path in sorted((bundle/'runtime/cpu').glob('*.dll'))
                                 if path.name in {dll.name for dll in crt.glob('*.dll')}},
                "tools": {"ffmpeg": "9.0.2-essentials GPLv3", "tesseract": "5.5.3.20260724", "djvulibre": "3.5.29", "webview2": "156.0.4314.58", "msvc": "14.44.35211"}}
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Built:", bundle / "Zvukbook.exe", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--edition", choices=("full", "cpu"), default="full")
    parser.add_argument("--include-release-tools", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:3] != (3, 12, 10):
        raise RuntimeError("Use CPython 3.12.10 x64 on Windows")
    if shutil.which("curl.exe") is None:
        raise RuntimeError("Windows curl.exe is required for verified downloads")
    manifest = fetch_all(include_release_tools=args.include_release_tools)
    devices = ("cpu",) if args.edition == "cpu" else ("cpu", "cuda")
    signatures = prepare_environments(devices)
    extract_tools()
    assemble(signatures, args.exe.resolve(strict=True), manifest, args.edition)


if __name__ == "__main__":
    main()
