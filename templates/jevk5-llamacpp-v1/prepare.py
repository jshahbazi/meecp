"""Seal the pinned JevK5 model, verified relocated engine, and private adapter."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent / "assets"
if not ASSETS.is_dir():
    ASSETS = ROOT
sys.path[:0] = [str(ROOT / "backend"), str(ASSETS / "backend"), str(ASSETS / "vendor")]

from jevk5_app.server import make_app
from mcpme.contracts import validate_package
from mcpme.model_intake import (ENGINE_BUILD, MAX_ENGINE_BYTES, MAX_METADATA_BYTES, MAX_WEIGHT_BYTES,
                                MODEL, PROFILE, canonical_digest, copy_artifact, engine_records)


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
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if result.returncode:
        raise ValueError("preparation_command_failed")
    return result.stdout.strip()


def exported_snapshot(export: dict, destination: Path) -> None:
    """Prove the exact raw binary selection before any build/engine execution."""
    if (not isinstance(export, dict) or type(export.get("revision")) is not int or export["revision"] < 1
            or not isinstance(export.get("draft_id"), str) or str(uuid.UUID(export["draft_id"])) != export["draft_id"]):
        raise ValueError("invalid_setup_export")
    source = export.get("source", {})
    records = source.get("files", [])
    if (source.get("kind") != "artifact_collection" or type(source.get("contract_version")) is not int or source["contract_version"] != 1
            or not isinstance(records, list) or not 1 <= len(records) <= 257):
        raise ValueError("model_artifact_collection_required")
    expected = canonical_digest(records)
    if source.get("digest") != expected or source.get("revision") != "sha256:" + expected:
        raise ValueError("source_identity_mismatch")
    requirements = export.get("requirements", {})
    if (requirements.get("engine_profile") != PROFILE or requirements.get("profile") != "model-service"
            or set(requirements.get("tools", [])) != {"jevk5_decide", "jevk5_status"}
            or source.get("model") != MODEL or source.get("engine") != {"profile": PROFILE, "build": ENGINE_BUILD}):
        raise ValueError("model_profile_required")
    snapshot = Path(source["snapshot_path"])
    if not snapshot.is_absolute() or snapshot.resolve() != snapshot or not snapshot.is_dir():
        raise ValueError("source_snapshot_required")
    seen, engine_bytes = set(), 0
    for record in records:
        if (not isinstance(record, dict) or set(record) != {"path", "role", "sha256", "size", "executable"}
                or type(record["size"]) is not int or record["size"] < 0 or type(record["executable"]) is not bool):
            raise ValueError("invalid_source_file_list")
        relative, role = record["path"], record["role"]
        from mcpme.contracts import package_relative_path
        package_relative_path(relative)
        if relative in seen or role not in {"model-weights", "runtime-file", "receipt", "license"}:
            raise ValueError("invalid_source_file_list")
        seen.add(relative)
        if role == "model-weights" and relative != "weights/" + MODEL["file"]:
            raise ValueError("unsupported_model_artifact")
        if role != "model-weights" and not relative.startswith("engine/"):
            raise ValueError("invalid_source_file_list")
        if role == "runtime-file":
            engine_bytes += record["size"]
            if engine_bytes > MAX_ENGINE_BYTES:
                raise ValueError("artifact_size_exceeded")
        limit = MAX_WEIGHT_BYTES if role == "model-weights" else MAX_METADATA_BYTES if role in {"receipt", "license"} else MAX_ENGINE_BYTES
        copied = copy_artifact(snapshot / relative, destination / relative, limit=limit, role=role,
                               expected_sha256=record["sha256"], expected_size=record["size"])
        if copied["executable"] != record["executable"]:
            raise ValueError("source_snapshot_changed")
    if "weights/" + MODEL["file"] not in seen:
        raise ValueError("unsupported_model_artifact")
    root, engine = engine_records(destination / "engine")
    if {"engine/" + relative for relative, _, _ in engine} | {"weights/" + MODEL["file"]} != seen:
        raise ValueError("engine_identity_mismatch")


def verify_engine(resources: Path) -> None:
    engine, records = engine_records(resources)
    for relative, role, sha in records:
        if role == "runtime-file":
            path = engine / relative
            copy_artifact(path, None, limit=MAX_ENGINE_BYTES, role=role, expected_sha256=sha)
            if relative == "llama/llama-server":
                continue  # Legacy ownership reaper is never carried into the new package.
            if "arm64" not in run("/usr/bin/lipo", "-archs", str(path)).split():
                raise ValueError("engine_build_unsupported")
            for line in run("/usr/bin/otool", "-L", str(path)).splitlines()[1:]:
                dependency = line.strip().split(" (compatibility")[0]
                if not dependency.startswith(("@loader_path/", "/usr/lib/", "/System/Library/")):
                    raise ValueError("engine_not_relocated")
    # Executing the explicitly selected engine is confined to this foreground
    # agent workspace generator, never intake/attachment/installation planning.
    result = subprocess.run([str(engine / "llama/llama-server.real"), "--version"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                            cwd=engine / "llama/plugins", env={"PATH": "/usr/bin:/bin"})
    output = (result.stdout + result.stderr)[:65536].decode("utf-8", errors="replace")
    if result.returncode or not re.search(r"version:\s*0\.5\.0\s*\(build 11146,\s*commit 7fe450e19\)", output):
        raise ValueError("engine_build_unsupported")


def prepare(destination: Path, legacy: Path | None, host: Path, setup_export: Path | None = None):
    if destination.exists():
        raise ValueError("destination_exists")
    model = json.loads((ASSETS / "model-manifest.json").read_text())
    if any(model[key] != value for key, value in MODEL.items()):
        raise ValueError("model_identity_mismatch")
    export = None
    inputs = None
    if setup_export is not None:
        if setup_export.stat().st_size > 1024 * 1024:
            raise ValueError("invalid_setup_export")
        export = json.loads(setup_export.read_bytes())
        inputs = tempfile.TemporaryDirectory(prefix="mcpme-jevk5-inputs-", dir=destination.parent)
        try:
            exported_snapshot(export, Path(inputs.name))
        except BaseException:
            inputs.cleanup()
            raise
        source_model = Path(inputs.name) / "weights" / model["file"]
        resources = Path(inputs.name) / "engine"
    else:
        source_model = ASSETS / "models" / model["file"]
        if legacy is None:
            raise ValueError("engine_build_required")
        resources = legacy / "Contents/Resources"
    if source_model.stat().st_size != model["bytes"] or digest(source_model) != model["sha256"]:
        raise ValueError("model_identity_mismatch")
    verify_engine(resources)
    destination.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(host / "python", destination / "python", symlinks=False, ignore=ignore)
    for name in ("jevk5_app", "mcpme_jevk5"):
        shutil.copytree(ASSETS / "backend" / name, destination / "backend" / name, ignore=ignore)
    shutil.copyfile(ASSETS / "backend/mcpme_local_transport.py", destination / "backend/mcpme_local_transport.py")
    shutil.copytree(ASSETS / "vendor", destination / "vendor", ignore=ignore)
    shutil.copytree(resources / "llama", destination / "llama", ignore=ignore)
    # The old fixed-name reaper is intentionally absent: the host owns the real engine.
    (destination / "llama/llama-server").unlink(missing_ok=True)
    shutil.copytree(resources / "licenses", destination / "licenses", ignore=ignore)
    copy(ASSETS / "THIRD_PARTY_NOTICES.md", destination / "THIRD_PARTY_NOTICES.md")
    copy(ASSETS / "model-manifest.json", destination / "model-manifest.json")
    copy(resources / "runtime-manifest.json", destination / "runtime-manifest.json")
    copy(ASSETS / "uv.lock", destination / "uv.lock")
    copy(host / "build.json", destination / "python-runtime-origin.json")
    copy(source_model, destination / "models" / model["file"])
    (destination / "bin").mkdir()
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(ASSETS / "scripts/jevk5-package-launcher.c"), "-o", str(destination / "bin/jevk5-engine"))
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
    for path in sorted([*(destination / "backend").rglob("*.py"), ASSETS / "scripts/jevk5-package-launcher.c"]):
        source_digest.update(path.name.encode() + bytes.fromhex(digest(path)))
    revision = export["source"]["revision"] if export is not None else "sha256:" + source_digest.hexdigest()
    if export is not None:
        # The receipt proves which immutable binary selection and reviewed
        # adapter/scorer template supplied the package. No source bytes/logs.
        receipt = {"schema_version": 1, "template_id": PROFILE, "template_version": 1,
                   "draft_id": export["draft_id"], "draft_revision": export["revision"],
                   "source_revision": revision, "source_files": export["source"]["files"],
                   "adapter_source_sha256": source_digest.hexdigest(), "engine_build": ENGINE_BUILD}
        (destination / "preparation-receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
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
                  "engine_identity": {"runtime": "llama.cpp", "build": ENGINE_BUILD},
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
    package = validate_package(destination)
    if export is not None:
        from mcpme.imports import _model_return
        # A version probe/build cannot silently alter the selected runtime and
        # then describe the edited bytes as the draft's original artifacts.
        _model_return(export, package)
    if inputs is not None:
        inputs.cleanup()
    return package


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-app", type=Path)
    parser.add_argument("--setup-export", type=Path)
    parser.add_argument("--host", type=Path, default=ROOT / "dist/mcpme-host")
    args = parser.parse_args()
    try:
        package = prepare(args.output.absolute(), args.legacy_app.absolute() if args.legacy_app else None,
                          args.host.absolute(), args.setup_export.absolute() if args.setup_export else None)
        print(json.dumps({"prepared": True, "package_digest": package.digest, "artifacts": len(package.lock.artifacts)}))
    except Exception as error:
        codes = {"invalid_setup_export", "model_artifact_collection_required", "source_identity_mismatch",
                 "model_profile_required", "source_snapshot_required", "invalid_source_file_list",
                 "unsupported_model_artifact", "artifact_size_exceeded", "source_snapshot_changed",
                 "engine_identity_mismatch", "engine_build_unsupported", "engine_not_relocated",
                 "engine_build_required", "engine_license_required", "engine_receipt_invalid",
                 "model_identity_mismatch", "model_artifact_mismatch", "model_preparation_receipt_required",
                 "destination_exists", "preparation_command_failed", "insufficient_disk_space"}
        candidate = getattr(error, "code", None)
        if candidate is None and isinstance(error, ValueError):
            candidate = str(error)
        code = candidate if candidate in codes else "package_preparation_failed"
        print(json.dumps({"prepared": False, "error": code}))
        raise SystemExit(2) from None
