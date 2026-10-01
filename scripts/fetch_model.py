"""Download the pinned public model and verify it before installing locally."""
import hashlib
import json
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "model-manifest.json").read_text())
destination = root / "models" / manifest["file"]
destination.parent.mkdir(exist_ok=True)

def valid(path):
    if not path.exists() or path.stat().st_size != manifest["bytes"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == manifest["sha256"]

if valid(destination):
    print("Pinned model already verified")
else:
    temporary = destination.with_suffix(".download")
    url = f"https://huggingface.co/{manifest['repository']}/resolve/{manifest['revision']}/{manifest['file']}"
    print("Downloading the pinned public JevK5 Q8 model (4.48 GB)")
    urllib.request.urlretrieve(url, temporary)
    if not valid(temporary):
        temporary.unlink(missing_ok=True)
        raise SystemExit("Downloaded model failed size/SHA-256 verification")
    temporary.replace(destination)
    print("Model verified and installed in models/")
