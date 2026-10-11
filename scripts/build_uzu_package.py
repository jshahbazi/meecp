"""Build a relocatable experimental Uzu model-service outside the host bundle."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
from meecp.contracts import validate_package, MANIFEST_NAME, LOCK_NAME


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def command(arguments, **kwargs):
    result = subprocess.run(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    if result.returncode:
        raise ValueError("build_command_failed")


def build(args):
    destination = args.destination.resolve()
    if destination.exists():
        raise ValueError("destination_exists")
    provenance = json.loads((ROOT / "experiments/uzu-rust/MODEL_PROVENANCE.json").read_text())
    for record in provenance["uzu"]["files"]:
        path = args.models / record["path"]
        if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise ValueError("model_identity_mismatch")
    if not args.engine.is_file():
        raise ValueError("engine_missing")
    destination.mkdir(parents=True, mode=0o700)
    for directory in ("bin", "licenses", "backend", "models"):
        (destination / directory).mkdir()
    shutil.copy2(args.engine, destination / "bin/uzu-engine")
    # Preserve the relative notice paths recorded in THIRD_PARTY.json.
    shutil.copytree(ROOT / "experiments/uzu-rust/licenses", destination / "licenses", dirs_exist_ok=True)
    for name in ("THIRD_PARTY.json", "THIRD_PARTY.md"):
        shutil.copy2(ROOT / "experiments/uzu-rust" / name, destination / name)
    shutil.copy2(ROOT / "templates/LICENSE", destination / "licenses/LICENSE-MEECP")
    shutil.copy2(Path(sys.base_prefix) / "lib/python3.12/LICENSE.txt", destination / "licenses/LICENSE-PYTHON")
    shutil.copy2(ROOT / "uv.lock", destination / "uv.lock")
    shutil.copy2(ROOT / "experiments/uzu-rust/Cargo.lock", destination / "Cargo.lock")
    shutil.copy2(ROOT / "experiments/uzu-rust/MODEL_PROVENANCE.json", destination / "model-provenance.json")
    shutil.copytree(args.models / "uzu", destination / "models/uzu")
    # Copy a complete managed standalone interpreter, then install ONLY the
    # locked host adapter dependencies. No system/Homebrew Python at runtime.
    base = Path(sys.base_prefix)
    shutil.copytree(base, destination / "python", symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"))
    target = destination / "python/lib/python3.12/site-packages"
    target.mkdir(parents=True)
    requirements = destination / "requirements.txt"
    command(["uv", "export", "--locked", "--no-dev",
             "--no-emit-project", "--format", "requirements-txt", "--output-file", str(requirements)], cwd=ROOT)
    command(["uv", "pip", "install", "--python", str(destination / "python/bin/python3.12"),
             "--target", str(target), "--require-hashes", "--no-deps", "-r", str(requirements)], cwd=ROOT)
    notices = ["# Adapter dependencies", "", "Installed from the included hashed requirements and uv.lock.",
               "Full available license texts are retained in each distribution's metadata directory.", ""]
    for distribution in sorted(importlib.metadata.distributions(path=[str(target)]),
                               key=lambda item: item.metadata["Name"].lower()):
        notices.append("- " + distribution.metadata["Name"] + " " + distribution.version + ": "
            + (distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or
               "; ".join(value for value in distribution.metadata.get_all("Classifier", []) if "License ::" in value)
               or "See retained distribution metadata and license files."))
    (destination / "licenses/ADAPTER-DEPENDENCIES.md").write_text("\n".join(notices) + "\n")
    for module in ("meecp_uzu", "meecp_textgen", "jevk5_app"):
        shutil.copytree(ROOT / "backend" / module, destination / "backend" / module,
                       ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "provider.py"))
    shutil.copy2(ROOT / "backend/meecp_local_transport.py", destination / "backend/meecp_local_transport.py")
    (destination / "adapter.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.dont_write_bytecode = True\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'backend'))\n"
        "from meecp_uzu.__main__ import main\nraise SystemExit(main())\n")
    command(["/usr/bin/clang", "-O2", "-Wall", "-Wextra", "-o", str(destination / "bin/uzu-adapter"),
             str(ROOT / "experiments/uzu-rust/adapter-launcher.c")])
    for name in ("uzu-engine", "uzu-adapter"):
        command(["/usr/bin/codesign", "--force", "--sign", "-", str(destination / "bin" / name)])
    profile = {"artifact": "models/uzu/model.safetensors", "model_id": "Qwen3.5-0.8B-Mirai-L",
        "sha256": digest(destination / "models/uzu/model.safetensors"),
        "profile": "qwen3.5-0.8b-mirai-l-uzu-metal-text-v1", "context_tokens": 4096,
        "engine_identity": {"runtime": "uzu", "version": "0.6.1",
            "build": "8d01581474760bd1fc7f4a547ff1d02f7dada437", "binary_sha256": digest(destination / "bin/uzu-engine")},
        "parameters": {"max_output_tokens": 256, "queue_limit": 4, "context_guard": 8}}
    write_json(destination / "uzu-profile.json", profile)
    from meecp_uzu.service import make_app
    class SchemaManager:
        pass
    app, raw = make_app(SchemaManager(), "schema-only", port=19385)
    tools = []
    for tool in raw.state.mcp._tool_manager.list_tools():
        tools.append({"name": tool.name, "description": tool.description,
                      "input_schema": tool.parameters, "output_schema": tool.output_schema,
                      "annotations": tool.annotations.model_dump(exclude_none=True)})
    source_files = [ROOT / "scripts/build_uzu_package.py", ROOT / "uv.lock",
                    ROOT / "experiments/uzu-rust/adapter-launcher.c",
                    ROOT / "experiments/uzu-rust/Cargo.toml", ROOT / "experiments/uzu-rust/Cargo.lock"]
    source_files.extend((ROOT / "experiments/uzu-rust/src").rglob("*.rs"))
    for module in ("meecp_uzu", "meecp_textgen", "jevk5_app"):
        source_files.extend((ROOT / "backend" / module).rglob("*.py"))
    source_files.append(ROOT / "backend/meecp_local_transport.py")
    source_hash = hashlib.sha256()
    for path in sorted(source_files):
        source_hash.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        source_hash.update(path.read_bytes() + b"\0")
    manifest = {"schema_version": 1, "package_id": "local.uzu-textgen-experimental", "version": "0.1.0",
        "source_revision": "sha256:" + source_hash.hexdigest(),
        "name": "Uzu text generation (experimental)", "profile": "model-service",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "processes": [
            {"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/uzu-engine", "args": [], "cwd": "."},
             "depends_on": [], "readiness": {"path": "/health", "echo_generation": True}},
            {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/uzu-adapter", "args": [], "cwd": "."},
             "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}],
        "adapter_process": "adapter", "model": profile,
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]}, "tools": tools,
        "resources": {"memory_mb": args.memory_mb, "cpu_only": False, "gpu": True},
        "lifecycle": {"max_request_bytes": 32768, "max_sessions": 8, "operation_timeout_seconds": 120,
                      "startup_timeout_seconds": 180, "stop_grace_seconds": 5},
        "access": {"network": ["host-owned loopback engine"], "filesystem": []},
        "licenses": ["licenses/LICENSE-MEECP", "licenses/LICENSE-UZU", "licenses/LICENSE-MODEL",
                     "licenses/LICENSE-PYTHON", "licenses/ADAPTER-DEPENDENCIES.md", "THIRD_PARTY.md"],
        "validation": {"fixtures": []}}
    write_json(destination / MANIFEST_NAME, manifest)
    # uv's coordination lock can be group-writable; it is not a runtime input.
    (target / ".lock").unlink(missing_ok=True)
    for path in destination.rglob("*"):
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o7022)
    records = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative = path.relative_to(destination).as_posix()
        if relative.startswith("models/"):
            license_path = "licenses/LICENSE-MODEL"
        elif relative == "bin/uzu-engine":
            license_path = "THIRD_PARTY.md"
        elif relative.startswith("licenses/"):
            license_path = "THIRD_PARTY.md"
        elif relative.startswith("python/lib/python3.12/site-packages/"):
            license_path = "licenses/ADAPTER-DEPENDENCIES.md"
        elif relative.startswith("python/"):
            license_path = "licenses/LICENSE-PYTHON"
        else:
            license_path = "licenses/LICENSE-MEECP"
        records.append({"path": relative, "sha256": digest(path), "size": path.stat().st_size,
            "executable": bool(path.stat().st_mode & 0o111), "source": "Pinned experimental Uzu build and locked adapter closure",
            "provenance": "model-provenance.json", "license": license_path})
    write_json(destination / LOCK_NAME, {"schema_version": 1, "package_id": manifest["package_id"],
        "version": manifest["version"], "manifest_sha256": digest(destination / MANIFEST_NAME), "artifacts": records,
        "build": {"recipe": "experimental-uzu-text-v1", "builder": "scripts/build_uzu_package.py",
                  "toolchain": "Rust locked source plus standalone CPython 3.12 and uv.lock"}})
    package = validate_package(destination)
    return {"package": str(destination), "package_digest": package.digest, "files": len(records),
            "declared_memory_mb": args.memory_mb}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=ROOT / "runtime/uzu-rust/target/release/meecp-uzu-worker")
    parser.add_argument("--models", type=Path, default=ROOT / "models/uzu-experiment")
    parser.add_argument("--destination", type=Path, default=ROOT / "dist/uzu-textgen-experimental")
    parser.add_argument("--memory-mb", type=int, default=4096)
    args = parser.parse_args()
    if not 1024 <= args.memory_mb <= 16384:
        return 2
    try:
        print(json.dumps(build(args), sort_keys=True))
        return 0
    except BaseException as error:
        authored_codes = {"destination_exists", "model_identity_mismatch", "engine_missing", "build_command_failed"}
        error_code = getattr(error, "code", "package_failed")
        if isinstance(error, ValueError) and error.args and error.args[0] in authored_codes:
            error_code = error.args[0]
        print(json.dumps({"stage": "package_failed", "error_type": type(error).__name__,
                          "error_code": error_code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
