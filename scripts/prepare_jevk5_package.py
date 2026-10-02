"""Seal the pinned JevK5 model, verified relocated engine, and private adapter."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "vendor")]

from jevk5_app.server import make_app
from mcpme.contracts import validate_package


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["/bin/cp", "-c", str(source), str(destination)], capture_output=True)
    if result.returncode:
        shutil.copyfile(source, destination)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def prepare(destination: Path, legacy: Path, host: Path):
    if destination.exists():
        raise ValueError("destination_exists")
    model = json.loads((ROOT / "model-manifest.json").read_text())
    source_model = ROOT / "models" / model["file"]
    if source_model.stat().st_size != model["bytes"] or digest(source_model) != model["sha256"]:
        raise ValueError("model_identity_mismatch")
    resources = legacy / "Contents/Resources"
    receipts = json.loads((resources / "runtime-manifest.json").read_text())
    for relative, receipt in receipts["llama"].items():
        if digest(resources / relative) != receipt["sha256"]:
            raise ValueError("runtime_identity_mismatch")
    destination.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(host / "python", destination / "python", symlinks=False, ignore=ignore)
    for name in ("jevk5_app", "mcpme_jevk5"):
        shutil.copytree(ROOT / "backend" / name, destination / "backend" / name, ignore=ignore)
    shutil.copyfile(ROOT / "backend/mcpme_local_transport.py", destination / "backend/mcpme_local_transport.py")
    shutil.copytree(ROOT / "vendor", destination / "vendor", ignore=ignore)
    shutil.copytree(resources / "llama", destination / "llama", ignore=ignore)
    # The old fixed-name reaper is intentionally absent: the host owns the real engine.
    (destination / "llama/llama-server").unlink()
    shutil.copytree(resources / "licenses", destination / "licenses", ignore=ignore)
    copy(ROOT / "THIRD_PARTY_NOTICES.md", destination / "THIRD_PARTY_NOTICES.md")
    copy(ROOT / "model-manifest.json", destination / "model-manifest.json")
    copy(resources / "runtime-manifest.json", destination / "runtime-manifest.json")
    copy(ROOT / "uv.lock", destination / "uv.lock")
    copy(host / "build.json", destination / "python-runtime-origin.json")
    copy(source_model, destination / "models" / model["file"])
    (destination / "bin").mkdir()
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(ROOT / "scripts/jevk5-package-launcher.c"), "-o", str(destination / "bin/jevk5-engine"))
    copy(destination / "bin/jevk5-engine", destination / "bin/jevk5-adapter")
    for name in ("jevk5-engine", "jevk5-adapter"):
        run("/usr/bin/codesign", "--force", "--sign", "-", str(destination / "bin" / name))
    for path in destination.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    _, app = make_app(None, "unused-schema-only", tools_only=True)
    tools = asyncio.run(app.state.mcp.list_tools())
    declarations = []
    for tool in tools:
        value = tool.model_dump(by_alias=True, exclude_none=True)
        declarations.append({"name": value["name"], "description": value["description"],
                             "input_schema": value["inputSchema"], "output_schema": value.get("outputSchema"),
                             "annotations": value.get("annotations")})
    source_digest = hashlib.sha256()
    for path in sorted([*(destination / "backend").rglob("*.py"), ROOT / "scripts/jevk5-package-launcher.c"]):
        source_digest.update(path.name.encode() + bytes.fromhex(digest(path)))
    revision = "sha256:" + source_digest.hexdigest()
    manifest = {"schema_version": 1, "host_api_version": 1, "package_id": "local.jevk5", "version": "1.0.0",
        "source_revision": revision, "name": "JevK5 Judge", "profile": "model-service",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "processes": [
            {"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/jevk5-engine", "args": [], "cwd": "."},
             "depends_on": [], "readiness": {"path": "/props", "echo_generation": False}},
            {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/jevk5-adapter", "args": [], "cwd": "."},
             "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}],
        "adapter_process": "adapter",
        "model": {"artifact": "models/" + model["file"], "model_id": model["model_id"], "sha256": model["sha256"],
                  "profile": model["profile"], "context_tokens": 8192,
                  "engine_identity": {"runtime": "llama.cpp", "build": "11146-7fe450e19305b828c199d602c23a8337aaa1f03b"},
                  "parameters": {"temperature": 1.22, "knockout_temperature": 0.93}},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "lifecycle": {"max_sessions": 16, "idle_timeout_seconds": 300, "operation_timeout_seconds": 130,
                      "startup_timeout_seconds": 180, "stop_grace_seconds": 5},
        "tools": declarations, "access": {"filesystem": [], "network": ["host-owned loopback engine"], "secrets": [], "writes": False},
        "resources": {"memory_mb": 8192, "cpu_only": False, "gpu": True},
        "licenses": ["THIRD_PARTY_NOTICES.md", "vendor/jevk5/LICENSE", *[str(p.relative_to(destination))
                     for p in sorted((destination / "licenses").rglob("*")) if p.is_file()]]}
    (destination / "mcpme.package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    artifacts = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "mcpme.package.json":
            continue
        relative = str(path.relative_to(destination))
        source = revision
        license = "THIRD_PARTY_NOTICES.md"
        if relative.startswith("models/"):
            source, license = model["revision"], "vendor/jevk5/LICENSE"
        elif relative.startswith("vendor/"):
            source, license = model["upstream_commit"], "vendor/jevk5/LICENSE"
        artifacts.append({"path": relative, "sha256": digest(path), "size": path.stat().st_size,
                          "executable": bool(path.stat().st_mode & 0o111), "source": source,
                          "provenance": "Verified local build; original engine hashes in runtime-manifest.json; dependencies in uv.lock.",
                          "license": license})
    lock = {"schema_version": 1, "package_id": "local.jevk5", "version": "1.0.0",
            "manifest_sha256": digest(destination / "mcpme.package.json"), "artifacts": artifacts,
            "build": {"recipe": "scripts/prepare_jevk5_package.py: verified pinned model and relocated runtime, private Python, native exec launchers",
                      "builder": "mcpme JevK5 package v1", "toolchain": run("/usr/bin/cc", "--version").splitlines()[0]}}
    (destination / "mcpme.lock.json").write_text(json.dumps(lock, separators=(",", ":")) + "\n")
    return validate_package(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/jevk5-package")
    parser.add_argument("--legacy-app", type=Path, default=ROOT / "dist/JevK5.app")
    parser.add_argument("--host", type=Path, default=ROOT / "dist/mcpme-host")
    args = parser.parse_args()
    try:
        package = prepare(args.output.resolve(), args.legacy_app.resolve(), args.host.resolve())
        print(json.dumps({"prepared": True, "package_digest": package.digest, "artifacts": len(package.lock.artifacts)}))
    except Exception:
        print(json.dumps({"prepared": False, "error": "package_preparation_failed"}))
        raise SystemExit(2) from None
