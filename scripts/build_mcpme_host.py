"""Build the relocatable CLI host with a private Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.13"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def build(destination: Path):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build requires Apple silicon macOS")
    if destination.exists():
        raise SystemExit("Destination already exists; select a new output directory")
    destination.mkdir(parents=True)
    runtime = Path(run("uv", "python", "find", "--managed-python", PYTHON_VERSION)).resolve().parent.parent
    shutil.copytree(runtime, destination / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "backend/mcpme", destination / "backend/mcpme",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / "bin").mkdir()
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(ROOT / "scripts/mcpme-reap.c"), "-o", str(destination / "bin/mcpme-reap"))
    requirements = destination / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
        "--output-file", str(requirements))
    run("uv", "pip", "install", "--python", str(runtime / "bin/python3.12"),
        "--target", str(destination / "python/site-packages"), "--require-hashes", "--requirement", str(requirements))
    launcher = destination / "bin/mcpme"
    launcher.write_text('#!/bin/sh\nset -eu\nBIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                        'MCPME_ROOT=$(CDPATH= cd -- "$BIN/.." && pwd)\n'
                        'unset PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP PYTHONINSPECT\n'
                        'export PYTHONPATH="$MCPME_ROOT/backend:$MCPME_ROOT/python/site-packages"\n'
                        'export PYTHONNOUSERSITE=1\n'
                        'export PYTHONDONTWRITEBYTECODE=1\n'
                        'exec "$MCPME_ROOT/python/bin/python3.12" -m mcpme "$@"\n')
    launcher.chmod(0o755)
    shutil.copyfile(ROOT / "uv.lock", destination / "uv.lock")
    shutil.copyfile(ROOT / "pyproject.toml", destination / "pyproject.toml")
    shutil.copytree(ROOT / "templates", destination / "templates",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "README.md", destination / "README.md")
    (destination / "docs").mkdir()
    for document in sorted((ROOT / "docs").glob("*.md")):
        shutil.copyfile(document, destination / "docs" / document.name)
    licenses = destination / "licenses"
    licenses.mkdir()
    shutil.copyfile(runtime / "lib/python3.12/LICENSE.txt", licenses / "python-LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    run("/usr/bin/codesign", "--force", "--sign", "-", str(destination / "bin/mcpme-reap"))
    source_files = [*[path for path in sorted((destination / "backend/mcpme").rglob("*")) if path.is_file()],
                    *sorted((destination / "docs").glob("*.md")), destination / "uv.lock",
                    destination / "pyproject.toml", *[path for path in sorted((destination / "templates").rglob("*")) if path.is_file()],
                    destination / "requirements.txt", destination / "README.md", destination / "bin/mcpme-reap"]
    source_hashes = {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in source_files}
    (destination / "build.json").write_text(json.dumps({"host_version": "0.1.0", "artifact_sha256": source_hashes,
        "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "source_tree_dirty": bool(run("git", "-C", str(ROOT), "status", "--porcelain")),
        "python": run(str(runtime / "bin/python3.12"), "--version"),
        "toolchain": run("/usr/bin/cc", "--version").splitlines()[0],
        "signing": "ad_hoc_reaper_only", "distribution": "development_cli_milestone"}, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mcpme-host")
    args = parser.parse_args()
    print(build(args.output.absolute()))
