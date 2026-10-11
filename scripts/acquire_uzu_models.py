"""Acquire only the immutable, hashed artifacts for the Uzu comparison."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def matches(path, record):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != record["size"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == record["sha256"]


def acquire(destination, record, offline):
    target = destination / record["path"]
    if matches(target, record):
        return "verified"
    if target.exists():
        raise ValueError("existing_artifact_mismatch")
    if offline:
        raise ValueError("artifact_missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".uzu-acquire-", dir=target.parent)
    temporary = Path(temporary)
    try:
        checksum = hashlib.sha256()
        total = 0
        request = urllib.request.Request(record["url"], headers={"User-Agent": "meecp-uzu-experiment"})
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > record["size"]:
                    raise ValueError("artifact_size_mismatch")
                checksum.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != record["size"] or checksum.hexdigest() != record["sha256"]:
            raise ValueError("artifact_identity_mismatch")
        # Never replace an existing file, including one created during acquisition.
        os.link(temporary, target)
        return "acquired"
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "models/uzu-experiment")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        provenance = json.loads((ROOT / "experiments/uzu-rust/MODEL_PROVENANCE.json").read_text())
        outcomes = [acquire(args.destination, record, args.offline)
                    for engine in ("uzu", "gguf") for record in provenance[engine]["files"]]
        print(json.dumps({"stage": "complete", "verified": outcomes.count("verified"),
                          "acquired": outcomes.count("acquired")}))
        return 0
    except BaseException as error:
        codes = {"existing_artifact_mismatch", "artifact_missing", "artifact_size_mismatch",
                 "artifact_identity_mismatch"}
        code = error.args[0] if isinstance(error, ValueError) and error.args and error.args[0] in codes else "acquisition_failed"
        print(json.dumps({"stage": "failed", "error_code": code, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
