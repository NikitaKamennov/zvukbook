#!/usr/bin/env python3
"""Download pinned Silero weights and prepare metadata; never execute the model."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify(path, asset):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular file: " + str(path))
    if path.stat().st_size != asset["bytes"] or checksum(path) != asset["sha256"]:
        raise ValueError("Checksum/size mismatch; existing file was not replaced: " + str(path))


def prepare():
    manifest = json.loads((ROOT / "config/silero-v5_6_ru.json").read_text(encoding="utf-8"))
    assets = {asset["file"]: asset for asset in manifest["assets"]}
    directory = ROOT / ".local-data/books-renderer/models/silero-v5_6_ru"
    directory.mkdir(parents=True, exist_ok=True)
    license_source = ROOT / "licenses/silero-v5_6_ru/LICENSE"
    verify(license_source, assets["LICENSE"])
    model = directory / "model.pt"
    if model.exists() or model.is_symlink():
        verify(model, assets["model.pt"])
        print("Existing Silero model verified.")
    else:
        request = Request(assets["model.pt"]["url"], headers={"User-Agent": "Zvukbook-model-setup"})
        descriptor, temporary_name = tempfile.mkstemp(prefix="model-", suffix=".part", dir=directory)
        temporary = Path(temporary_name)
        try:
            print("Downloading pinned Silero weights (about 141 MiB)...", flush=True)
            with os.fdopen(descriptor, "wb") as output, urlopen(request, timeout=60) as response:
                received = 0
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > assets["model.pt"]["bytes"]:
                        raise ValueError("Download exceeds the pinned model size.")
                    output.write(chunk)
            verify(temporary, assets["model.pt"])
            os.replace(temporary, model)
        finally:
            temporary.unlink(missing_ok=True)
    target_license = directory / "LICENSE"
    if target_license.exists() or target_license.is_symlink():
        verify(target_license, assets["LICENSE"])
    else:
        shutil.copyfile(license_source, target_license)
    target_manifest = directory / "manifest.json"
    if target_manifest.exists() or target_manifest.is_symlink():
        if target_manifest.is_symlink():
            raise ValueError("Model manifest must be a regular file.")
        current = json.loads(target_manifest.read_text(encoding="utf-8"))
        if not any(asset.get("file") == "model.pt" and asset.get("sha256") == assets["model.pt"]["sha256"]
                   for asset in current.get("assets", [])):
            raise ValueError("Existing manifest describes a different model; it was not replaced.")
    else:
        target_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Model ready:", directory)
    print("No model, server, OCR or synthesis was executed.")


if __name__ == "__main__":
    try:
        prepare()
    except (OSError, ValueError, URLError) as error:
        print("Model preparation failed:", error, file=sys.stderr)
        sys.exit(1)
