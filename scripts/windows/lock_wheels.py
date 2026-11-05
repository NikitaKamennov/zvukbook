"""Make a hash lock from a dedicated wheel download directory; no package imports."""
import argparse
import email
import hashlib
from pathlib import Path
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
records = {}
for wheel in sorted(args.directory.glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"Unexpected wheel metadata: {wheel.name}")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
    key = metadata["Name"].lower().replace("_", "-")
    if key in records:
        raise ValueError(f"Multiple wheels for {key}")
    with wheel.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    records[key] = f"{metadata['Name']}=={metadata['Version']} --hash=sha256:{sha}"
if not records:
    raise ValueError("No wheels were downloaded.")
args.output.write_text("# CPython 3.12 / Windows x64. Official PyPI wheels; generated from downloaded METADATA.\n"
                       + "\n".join(records[key] for key in sorted(records)) + "\n", encoding="utf-8")
