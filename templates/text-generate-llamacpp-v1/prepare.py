"""Seal the exact CPU Qwen fixture and a private host-owned MCP adapter.

This external development generator grants no automatic recipe capability. It
does not alter source artifacts or acquire model/runtime/dependency files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "vendor")]

from meecp.contracts import validate_package
from meecp.model_intake import copy_artifact
from meecp.preparation_builder import _dependency_notices
from meecp_textgen.fixture import (LICENSE_SHA256, MODEL_BYTES, MODEL_FILENAME, MODEL_REVISION,
                                  MODEL_SHA256, TOKENIZER_SHA256, digest, file_hash, tokenizer_metadata)
from meecp_textgen.service import make_app, model_declaration


def require(value):
    if not value:
        raise ValueError("fixture_verification_failed")


def command(*args):
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    require(result.returncode == 0)


def prepare(destination: Path, weights: Path, engine_build: Path, host: Path):
    destination = destination.absolute()
    require(destination.parent.resolve() == destination.parent and not destination.exists())
    source = engine_build.resolve()
    if (source / "Contents/Resources").is_dir():
        source = source / "Contents/Resources"
    host = host.resolve()
    if (host / "Contents/Resources/host").is_dir():
        host = host / "Contents/Resources/host"
    origin = json.loads((host / "build.json").read_text())
    require(origin["source_tree_dirty"] is False)
    # Select a clean exact host closure for private Python and locked wheels.
    # All copied Python bytes and wheel notices are additionally sealed below.
    for relative, expected in origin["artifact_sha256"].items():
        require(file_hash(host / relative) == expected)
    require(file_hash(host / "uv.lock") == file_hash(ROOT / "uv.lock"))
    closure = json.loads((ROOT / "tests/fixtures/text_generation/engine-closure.json").read_text())
    require(file_hash(ROOT / "vendor/qwen2.5-0.5b/LICENSE") == LICENSE_SHA256)
    with tempfile.TemporaryDirectory(prefix=".textgen-build-", dir=destination.parent) as staging:
        output = Path(staging) / "package"
        output.mkdir(mode=0o700)
        for relative, identity in closure.items():
            # The host's native reaper replaces the legacy private supervisor.
            # Still verify the selected complete engine identity before omission.
            copy_artifact(source / relative, None if relative == "llama/llama-server" else output / relative,
                limit=128 * 1024**2, role="runtime-file", expected_sha256=identity["sha256"],
                expected_size=identity["bytes"])
        copy_artifact(weights.resolve(), output / "models" / MODEL_FILENAME, limit=MODEL_BYTES,
            role="model-weights", expected_sha256=MODEL_SHA256, expected_size=MODEL_BYTES)
        require(digest(tokenizer_metadata(output / "models" / MODEL_FILENAME)) == TOKENIZER_SHA256)
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(host / "python", output / "python", symlinks=False, ignore=ignored)
        shutil.copytree(ROOT / "backend/meecp_textgen", output / "backend/meecp_textgen", ignore=ignored)
        # Reuse the existing route/authentication policy without modifying it.
        (output / "backend/jevk5_app").mkdir()
        for name in ("__init__.py", "server.py", "contracts.py", "state.py"):
            shutil.copyfile(ROOT / "backend/jevk5_app" / name, output / "backend/jevk5_app" / name)
        (output / "licenses").mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "vendor/qwen2.5-0.5b/LICENSE", output / "licenses/Qwen-LICENSE")
        shutil.copyfile(host / "python/lib/python3.12/LICENSE.txt", output / "licenses/python-LICENSE.txt")
        shutil.copyfile(host / "uv.lock", output / "uv.lock")
        shutil.copyfile(host / "requirements.txt", output / "requirements.txt")
        shutil.copyfile(host / "build.json", output / "python-runtime-origin.json")
        _dependency_notices(output / "python/site-packages", output, lambda: None)
        (output / "THIRD_PARTY_NOTICES.md").write_text(
            "# Exact local text generation package\n\n"
            "Qwen2.5 0.5B Q4_K_M is Apache-2.0; see licenses/Qwen-LICENSE. "
            "llama.cpp, ggml, libomp and OpenSSL notices are retained in licenses/. "
            "Private CPython licensing is in licenses/python-LICENSE.txt. "
            "Locked Python wheel metadata and license files are retained in python/site-packages; "
            "dependency-notices.json binds each distribution and license text. "
            "The MCP wrapper and bootstrap come from the selected meecp source revision.\n")
        (output / "bin").mkdir()
        command("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                str(ROOT / "scripts/textgen-package-launcher.c"), "-o", str(output / "bin/textgen-engine"))
        shutil.copyfile(output / "bin/textgen-engine", output / "bin/textgen-adapter")
        (output / "bin/textgen-adapter").chmod(0o755)
        for name in ("textgen-engine", "textgen-adapter"):
            command("/usr/bin/codesign", "--force", "--sign", "-", str(output / "bin" / name))
        _, app = make_app(None, "schema-only", port=1)
        tools = asyncio.run(app.state.mcp.list_tools())
        declarations = []
        for tool in tools:
            value = tool.model_dump(by_alias=True, exclude_none=True)
            declarations.append({"name": value["name"], "description": value["description"],
                "input_schema": value["inputSchema"], "output_schema": value.get("outputSchema"),
                "annotations": value.get("annotations")})
        source_files = {str(p.relative_to(output)): file_hash(p)
                        for p in sorted((output / "backend").rglob("*.py"))}
        source_files["generator"] = file_hash(Path(__file__))
        source_files["launcher"] = file_hash(ROOT / "scripts/textgen-package-launcher.c")
        bindings = {"model_sha256": MODEL_SHA256, "model_revision": MODEL_REVISION,
                    "tokenizer_sha256": TOKENIZER_SHA256, "selected_engine_closure": closure,
                    "source_files": source_files, "private_python_source_commit": origin["source_commit"]}
        revision = "sha256:" + digest(bindings)
        (output / "text-generation-origin.json").write_text(json.dumps(bindings, sort_keys=True, indent=2) + "\n")
        manifest = {"schema_version": 1, "host_api_version": 1,
            "package_id": "local.qwen-text-fixture", "version": "1.0.0", "source_revision": revision,
            "name": "Local text generation fixture", "profile": "model-service",
            "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "processes": [
                {"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/textgen-engine"},
                 "depends_on": [], "readiness": {"path": "/props", "echo_generation": False}},
                {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/textgen-adapter"},
                 "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}],
            "adapter_process": "adapter", "model": model_declaration(),
            "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
            "lifecycle": {"max_request_bytes": 32768, "max_sessions": 8, "idle_timeout_seconds": 300,
                          "operation_timeout_seconds": 60, "startup_timeout_seconds": 90, "stop_grace_seconds": 5},
            "tools": declarations, "access": {"filesystem": [], "network": ["host-owned loopback engine"],
                                               "secrets": [], "writes": False},
            "resources": {"memory_mb": 1024, "cpu_only": True, "gpu": False},
            "licenses": ["THIRD_PARTY_NOTICES.md", *[str(p.relative_to(output))
                          for p in sorted((output / "licenses").rglob("*")) if p.is_file()]]}
        (output / "meecp.package.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        artifacts = []
        for path in sorted(output.rglob("*")):
            if path.is_dir():
                path.chmod(0o755)
            elif path.is_file():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
                if path.name == "meecp.package.json":
                    continue
                relative = str(path.relative_to(output))
                artifacts.append({"path": relative, "sha256": file_hash(path), "size": path.stat().st_size,
                    "executable": bool(path.stat().st_mode & 0o111), "source": MODEL_REVISION if relative.startswith("models/") else revision,
                    "provenance": "Exact selected local closure; identities in text-generation-origin.json and dependency-notices.json.",
                    "license": "licenses/Qwen-LICENSE" if relative.startswith("models/") else "THIRD_PARTY_NOTICES.md"})
        lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
                "manifest_sha256": file_hash(output / "meecp.package.json"), "artifacts": artifacts,
                "build": {"recipe": "text-generate-llamacpp-v1: exact CPU fixture; external development generator",
                          "builder": "meecp text fixture v1", "toolchain": "Apple clang; selected private CPython 3.12"}}
        (output / "meecp.lock.json").write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n")
        validate_package(output)
        require(not destination.exists())
        os.rename(output, destination)
    return validate_package(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--engine-build", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    args = parser.parse_args()
    try:
        package = prepare(args.output, args.weights, args.engine_build, args.host)
        print(json.dumps({"prepared": True, "package_digest": package.digest, "artifacts": len(package.lock.artifacts)}))
        return 0
    except Exception as error:
        print(json.dumps({"prepared": False, "error": "package_preparation_failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
