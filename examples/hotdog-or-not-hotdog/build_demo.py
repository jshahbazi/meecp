"""Copy the ordinary app and a private Python runtime into a new portable folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SOURCES = ["app.py", "detector.py", "run.py", "index.html", "Launch.command", "Setup.command",
           "prepare_model.py", "build_demo.py", "pyproject.toml", "uv.lock", "requirements.txt",
           "LICENSE", "LICENSE-PYTHON", "LICENSE-FLATBUFFERS", "README.md", "THIRD_PARTY_NOTICES.md",
           "model-provenance.json"]


def build(destination: Path) -> None:
    if destination.exists():
        raise ValueError("destination_exists")
    model = ROOT / "models" / "yolo11n.onnx"
    receipt = json.loads((ROOT / "model-provenance.json").read_text())
    if hashlib.sha256(model.read_bytes()).hexdigest() != receipt["onnx"]["sha256"]:
        raise ValueError("model_integrity_failed")
    destination.mkdir(parents=True)
    for name in SOURCES:
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(ROOT / "tests", destination / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    (destination / "models").mkdir()
    shutil.copy2(model, destination / "models" / model.name)
    # uv's python-build-standalone distribution is relocatable; exclude build caches.
    shutil.copytree(Path(sys.base_prefix), destination / "runtime", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # This marker describes uv's original managed installation. Only our private copy is modified.
    marker = destination / "runtime" / "lib" / "python3.12" / "EXTERNALLY-MANAGED"
    marker.unlink(missing_ok=True)
    runtime = destination / "runtime" / "bin" / "python3"
    subprocess.run(["uv", "pip", "install", "--python", str(runtime), "--require-hashes", "--no-deps",
                    "--only-binary", ":all:", "-r", str(destination / "requirements.txt")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Launch scripts preserve executable modes. Runtime launches need neither uv nor a global Python.
    for script in ["Launch.command", "Setup.command"]:
        os.chmod(destination / script, 0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an ordinary portable macOS app folder.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.output.resolve())
    except Exception:
        print("Demo build failed. Check the model, runtime, dependencies, and unused output location.")
        raise SystemExit(1)
    print("Portable app folder built with private Python, locked dependencies, and YOLO11n.")
