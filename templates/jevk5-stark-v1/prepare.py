"""Seal a pinned Stark build, converted BF16 JevK5 and private Python runtime.

The generator uses the standard library. It loads no model and emits only fixed
failure codes and artifact counts. Generated packages belong outside Git.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().parent
PACKAGE_ID = "local.jevk5.stark"
PROFILE = "jevk5-stark-bf16-v1"
MODEL_ID = "JevK5-4B-v0.3-BF16"
MODEL_DIRECTORY = "models/jevk5-bf16"
MODEL_FILES = ("chat_template.jinja", "config.json", "generation_config.json", "jevk5_config.json",
               "model.safetensors", "tokenizer.json", "tokenizer_config.json")
MAX_RUNTIME_FILES = 16384
MAX_RUNTIME_BYTES = 512 * 1024**2
MAX_METADATA_BYTES = 4 * 1024**2
ERRORS = frozenset({"invalid_command_arguments", "destination_exists", "unsafe_path", "unsafe_file",
    "input_directory_required", "input_changed", "model_identity_mismatch", "model_inventory_mismatch",
    "model_provenance_invalid", "runtime_receipt_invalid", "runtime_reaper_required", "runtime_size_exceeded",
    "runtime_unavailable", "runtime_not_relocated", "engine_not_relocated", "engine_identity_mismatch",
    "engine_development_build_rejected", "engine_license_required", "dependency_verification_failed",
    "source_contract_unavailable", "compiler_failed", "signing_failed", "package_validation_failed",
    "package_preparation_failed", "package_preparation_interrupted"})


class PreparationError(Exception):
    """Authored, content-free command failure."""


def fail(code: str) -> None:
    raise PreparationError(code if code in ERRORS else "package_preparation_failed")


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def sha(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        fail("unsafe_file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(path: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    if ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        fail("unsafe_path")
    if directory and not path.is_dir():
        fail("input_directory_required")
    return path


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        fail("unsafe_file")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        fail("unsafe_file")
    return value


def run(args: list[str], *, code: str, cwd: Path | None = None, env: dict | None = None) -> bytes:
    try:
        result = subprocess.run(args, cwd=cwd, env=env or {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        fail(code)
    if result.returncode:
        fail(code)
    return result.stdout


def regular_files(directory: Path, *, runtime: bool = False) -> dict[str, Path]:
    """Resolve only internal links in the trusted Python bundle; emit regular files."""
    result: dict[str, Path] = {}
    count, total = 0, 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        parent = Path(parent)
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        if any((parent / name).is_symlink() for name in dirs):
            fail("unsafe_path")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            source = parent / name
            relative = source.relative_to(directory).as_posix()
            if source.is_symlink():
                if not runtime or not source.resolve().is_relative_to(directory):
                    fail("unsafe_path")
                resolved = source.resolve()
            else:
                resolved = source
            if not stat.S_ISREG(resolved.lstat().st_mode):
                fail("unsafe_file")
            count += 1
            total += resolved.stat().st_size
            if runtime and (count > MAX_RUNTIME_FILES or total > MAX_RUNTIME_BYTES):
                fail("runtime_size_exceeded")
            result[relative] = source
    return result


def record(path: Path) -> dict:
    resolved = path.resolve() if path.is_symlink() else path
    info = resolved.lstat()
    return {"sha256": sha(resolved), "size": info.st_size, "executable": bool(info.st_mode & 0o111),
            "link_sha256": hashlib.sha256(os.readlink(path).encode()).hexdigest() if path.is_symlink() else None}


def inventory(files: dict[str, Path]) -> dict[str, dict]:
    return {name: record(path) for name, path in sorted(files.items())}


def copy_file(source: Path, destination: Path, expected: dict | None = None) -> None:
    if source.is_symlink():
        source = source.resolve()
    expected = expected or record(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = subprocess.run(["/bin/cp", "-c", str(source), str(destination)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if copied.returncode:
        shutil.copyfile(source, destination)
    destination.chmod(0o755 if expected["executable"] else 0o644)
    if sha(destination) != expected["sha256"] or destination.stat().st_size != expected["size"]:
        fail("input_changed")


def dependency_module():
    path = ROOT / "scripts/stark_dependency.py"
    spec = importlib.util.spec_from_file_location("stark_dependency_for_jevk5_package", path)
    if spec is None or spec.loader is None:
        fail("source_contract_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_build(build: Path, lock: dict) -> dict:
    try:
        receipt = dependency_module().verify_build(build, lock)
    except Exception:
        fail("dependency_verification_failed")
    if receipt["source"]["development_only"] or receipt["source"]["dirty"]:
        fail("engine_development_build_rejected")
    if receipt["source"]["revision"] != lock["revision"] or receipt["source"]["tree"] != lock["tree"]:
        fail("engine_identity_mismatch")
    return receipt


def model_records(model: Path, provenance: dict) -> dict[str, dict]:
    conversion = provenance.get("conversion", {})
    if (provenance.get("schema_version") != 1 or conversion.get("status") != "complete"
            or conversion.get("dtype") != "bfloat16" or conversion.get("quantization") != "none"
            or provenance.get("calibration") != {"temperature": 1.22, "knockout_temperature": 0.93}):
        fail("model_provenance_invalid")
    files = conversion.get("files", [])
    if not isinstance(files, list) or len(files) != len(MODEL_FILES):
        fail("model_provenance_invalid")
    expected = {item["name"]: item for item in files}
    if set(expected) != set(MODEL_FILES) or {path.name for path in model.iterdir()} != set(MODEL_FILES):
        fail("model_inventory_mismatch")
    actual = {}
    for name in MODEL_FILES:
        path = model / name
        item = expected[name]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
            fail("model_identity_mismatch")
        actual[name] = record(path)
    return actual


def runtime_inputs(runtime: Path) -> tuple[Path, Path, dict]:
    for candidate in (runtime, runtime / "Contents/Resources/host", runtime / "host"):
        if (candidate / "python/bin/python3.12").is_file() and (candidate / "build.json").is_file():
            runtime = safe_path(candidate, directory=True)
            break
    else:
        fail("runtime_unavailable")
    receipt = read_json(runtime / "build.json")
    hashes = receipt.get("artifact_sha256", {})
    reaper = runtime / "bin/meecp-reap"
    if not reaper.is_file():
        reaper = runtime / "bin/mcpme-reap"
    if not reaper.is_file() or reaper.is_symlink() or not (reaper.stat().st_mode & 0o111):
        fail("runtime_reaper_required")
    relative = reaper.relative_to(runtime).as_posix()
    if receipt.get("python") != "Python 3.12.13" or hashes.get(relative) != sha(reaper):
        fail("runtime_receipt_invalid")
    return runtime, reaper, receipt


def verify_native(path: Path, *, code: str) -> None:
    if "arm64" not in run(["/usr/bin/lipo", "-archs", str(path)], code=code).decode().split():
        fail(code)
    libraries = run(["/usr/bin/otool", "-L", str(path)], code=code).decode().splitlines()[1:]
    for line in libraries:
        library = line.strip().split(" (compatibility")[0]
        if not library.startswith(("/usr/lib/", "/System/Library/")):
            fail(code)


def tools_contract(path: Path) -> list[dict]:
    value = read_json(path)
    tools = value.get("tools")
    if not isinstance(tools, list) or {tool.get("name") for tool in tools} != {"jevk5_decide", "jevk5_status"}:
        fail("source_contract_unavailable")
    return [{"name": tool["name"], "description": tool["description"], "input_schema": tool["inputSchema"],
             "output_schema": tool.get("outputSchema"), "annotations": tool.get("annotations")} for tool in tools]


def validate_output(destination: Path) -> str:
    # The private interpreter supplies locked validators. Preparation may inspect
    # host source; neither installed entrypoint uses the checkout after delivery.
    code = ("import sys;sys.path[:0]=sys.argv[1:3];from pathlib import Path;"
            "from meecp.contracts import validate_package;print(validate_package(Path(sys.argv[3])).digest)")
    result = run([str(destination / "python/bin/python3.12"), "-I", "-S", "-B", "-c", code,
                  str(ROOT / "backend"), str(destination / "python/site-packages"), str(destination)],
                 code="package_validation_failed", cwd=destination)
    digest = result.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        fail("package_validation_failed")
    return digest


def prepare(output: Path, build: Path, model: Path, runtime: Path, *, consecutive_cache: bool = False) -> dict:
    if type(consecutive_cache) is not bool:
        fail("invalid_command_arguments")
    output = safe_path(output)
    if output.exists():
        fail("destination_exists")
    build, model, runtime = (safe_path(path, directory=True) for path in (build, model, runtime))
    if any(output.is_relative_to(path) for path in (build, model, runtime)):
        fail("unsafe_path")
    lock_path = ROOT / "engines/stark.lock.json"
    provenance_path = ROOT / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json"
    lock, provenance = read_json(lock_path), read_json(provenance_path)
    build_receipt = verified_build(build, lock)
    models = model_records(model, provenance)
    runtime, reaper, runtime_receipt = runtime_inputs(runtime)
    runtime_files = regular_files(runtime / "python", runtime=True)
    verify_native(build / "bin/stark-score", code="engine_not_relocated")
    verify_native(reaper, code="runtime_not_relocated")
    source_files = {}
    selected_trees = {"python": (runtime / "python", True), "vendor": (ROOT / "vendor/jevk5", False)}
    for name in ("jevk5_app", "meecp_jevk5", "meecp_stark"):
        folder = safe_path(ROOT / "backend" / name, directory=True)
        selected_trees[name] = (folder, False)
        source_files.update({"backend/" + name + "/" + relative: path for relative, path in regular_files(folder).items()})
    for name in ("__init__.py", "processes.py"):
        source_files["backend/meecp/" + name] = ROOT / "backend/meecp" / name
    source_files["backend/meecp_local_transport.py"] = ROOT / "backend/meecp_local_transport.py"
    source_files.update({"vendor/jevk5/" + relative: path for relative, path in regular_files(ROOT / "vendor/jevk5").items()})
    source_files.update({"engines/stark.lock.json": lock_path, "provenance/JEVK5_MODEL.json": provenance_path,
                        "uv.lock": ROOT / "uv.lock", "python-runtime-origin.json": runtime / "build.json",
                        "licenses/JEVK5_MODEL_LICENSE": ROOT / "experiments/uzu-rust/jev-scoring/licenses/JEVK5_MODEL_LICENSE",
                        "licenses/LALAMO_LICENSE": ROOT / "experiments/uzu-rust/jev-scoring/licenses/LALAMO_LICENSE",
                        "THIRD_PARTY_NOTICES.md": TEMPLATE / "THIRD_PARTY_NOTICES.md"})
    engine_licenses = {name: path for name, path in regular_files(build / "source").items()
                       if Path(name).name in {"LICENSE", "LICENSE-MIT", "LICENSE-APACHE", "NOTICE"}}
    if "LICENSE" not in engine_licenses:
        fail("engine_license_required")
    source_files.update({"licenses/stark/" + relative: path for relative, path in engine_licenses.items()})
    inputs = {**source_files, **{"python/" + relative: path for relative, path in runtime_files.items()},
              **{MODEL_DIRECTORY + "/" + name: model / name for name in MODEL_FILES},
              "bin/stark-score": build / "bin/stark-score", "native/meecp-reap": reaper,
              "provenance/STARK_BUILD_RECEIPT.json": build / "BUILD_RECEIPT.json",
              "provenance/STARK_DEPENDENCY_LOCK.json": build / "DEPENDENCY_LOCK.json"}
    before = inventory(inputs)
    # Bind parsed admission metadata to the exact bytes about to be copied.
    # A change between admission and sealing must not select a new identity.
    if (read_json(lock_path) != lock or read_json(provenance_path) != provenance
            or read_json(build / "BUILD_RECEIPT.json") != build_receipt
            or read_json(runtime / "build.json") != runtime_receipt):
        fail("input_changed")
    trees_before = {name: inventory(regular_files(path, runtime=trusted))
                    for name, (path, trusted) in selected_trees.items()}
    builder_inputs = {"prepare.py": Path(__file__), "launcher.c": TEMPLATE / "launcher.c",
                      "stark_dependency.py": ROOT / "scripts/stark_dependency.py",
                      "package_validator.py": ROOT / "backend/meecp/contracts.py",
                      "package_limits.py": ROOT / "backend/meecp/limits.py",
                      "tools.json": ROOT / "backend/meecp/legacy_jevk5_contract.json"}
    builders_before = inventory(builder_inputs)
    declarations = tools_contract(builder_inputs["tools.json"])
    python_version = run([str(runtime / "python/bin/python3.12"), "-I", "-S", "-B", "--version"], code="runtime_unavailable").decode().strip()
    if python_version != "Python 3.12.13":
        fail("runtime_receipt_invalid")
    if inventory(inputs) != before or inventory(builder_inputs) != builders_before:
        fail("input_changed")
    output.mkdir(parents=True, mode=0o755)
    try:
        for relative, path in sorted(inputs.items()):
            copy_file(path, output / relative, before[relative])
        run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(TEMPLATE / "launcher.c"),
             "-o", str(output / "bin/jevk5-stark-engine")], code="compiler_failed")
        copy_file(output / "bin/jevk5-stark-engine", output / "bin/jevk5-stark-adapter")
        for name in ("jevk5-stark-engine", "jevk5-stark-adapter"):
            run(["/usr/bin/codesign", "--force", "--sign", "-", str(output / "bin" / name)], code="signing_failed")
        source_digest = hashlib.sha256(encoded({"inputs": before, "builders": builders_before})).hexdigest()
        revision = "sha256:" + source_digest
        receipt = {"schema_version": 1, "template_id": PROFILE, "template_version": 1,
                   "source_revision": revision, "inputs": before, "builder_inputs": builders_before,
                   "engine_revision": lock["revision"], "engine_tree": lock["tree"],
                   "model_revision": provenance["source"]["revision"], "consecutive_cache_enabled": consecutive_cache,
                   "cache_default_enabled": False}
        (output / "preparation-receipt.json").write_bytes(encoded(receipt))
        manifest = {"schema_version": 1, "host_api_version": 1, "package_id": PACKAGE_ID, "version": "0.1.0",
            "source_revision": revision, "name": "JevK5 Judge (Stark experimental)", "profile": "model-service",
            "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "processes": [
                {"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/jevk5-stark-engine", "args": [], "cwd": "."},
                 "depends_on": [], "readiness": {"path": "/health", "echo_generation": True}},
                {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/jevk5-stark-adapter", "args": [], "cwd": "."},
                 "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}],
            "adapter_process": "adapter",
            "model": {"artifact": MODEL_DIRECTORY + "/model.safetensors", "model_id": MODEL_ID,
                      "sha256": models["model.safetensors"]["sha256"], "profile": PROFILE, "context_tokens": 8192,
                      "engine_identity": {"name": "stark", "revision": lock["revision"], "tree": lock["tree"],
                                          "worker_sha256": build_receipt["binaries"]["stark-score"],
                                          "dependency_lock_sha256": before["engines/stark.lock.json"]["sha256"],
                                          "worker_protocol": lock["protocol"]["worker_mode"]},
                      "parameters": {"temperature": 1.22, "knockout_temperature": 0.93, "consecutive_cache": consecutive_cache}},
            "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
            "lifecycle": {"max_sessions": 16, "idle_timeout_seconds": 300, "operation_timeout_seconds": 130,
                          "startup_timeout_seconds": 180, "stop_grace_seconds": 5},
            "tools": declarations, "access": {"filesystem": [], "network": ["host-owned loopback engine"], "secrets": [], "writes": False},
            "resources": {"memory_mb": 12288, "cpu_only": False, "gpu": True},
            "licenses": ["THIRD_PARTY_NOTICES.md", "vendor/jevk5/LICENSE", "vendor/jevk5/NOTICE",
                         *sorted(name for name in source_files if name.startswith("licenses/"))]}
        (output / "meecp.package.json").write_bytes(encoded(manifest))
        files = regular_files(output)
        artifacts = []
        for relative, path in sorted(files.items()):
            if relative == "meecp.package.json":
                continue
            origin = revision
            license_path = "THIRD_PARTY_NOTICES.md"
            if relative.startswith("models/"):
                origin, license_path = provenance["source"]["revision"], "licenses/JEVK5_MODEL_LICENSE"
            elif relative.startswith("vendor/jevk5/"):
                origin, license_path = "f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a", "vendor/jevk5/LICENSE"
            elif relative == "bin/stark-score" or relative.startswith("licenses/stark/"):
                origin, license_path = lock["revision"], "licenses/stark/LICENSE"
            value = record(path)
            artifacts.append({"path": relative, "sha256": value["sha256"], "size": value["size"],
                              "executable": value["executable"], "source": origin,
                              "provenance": "Selected immutable inputs in preparation-receipt.json; engine build and model conversion provenance included.",
                              "license": license_path})
        package_lock = {"schema_version": 1, "package_id": PACKAGE_ID, "version": "0.1.0",
                        "manifest_sha256": sha(output / "meecp.package.json"), "artifacts": artifacts,
                        "build": {"recipe": "templates/jevk5-stark-v1/prepare.py", "builder": "meecp experimental Stark JevK5 package v1",
                                  "toolchain": run(["/usr/bin/cc", "--version"], code="compiler_failed").decode().splitlines()[0]}}
        (output / "meecp.lock.json").write_bytes(encoded(package_lock))
        if inventory(inputs) != before or inventory(builder_inputs) != builders_before:
            fail("input_changed")
        if {name: inventory(regular_files(path, runtime=trusted)) for name, (path, trusted) in selected_trees.items()} != trees_before:
            fail("input_changed")
        if model_records(model, provenance) != models or verified_build(build, lock) != build_receipt or read_json(runtime / "build.json") != runtime_receipt:
            fail("input_changed")
        package_digest = validate_output(output)
        if inventory(inputs) != before or inventory(builder_inputs) != builders_before:
            fail("input_changed")
        return {"prepared": True, "package_id": PACKAGE_ID, "package_digest": package_digest, "artifacts": len(artifacts),
                "engine_revision": lock["revision"], "consecutive_cache_enabled": consecutive_cache, "cache_default_enabled": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            fail("invalid_command_arguments")
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--build", type=Path, required=True)
        parser.add_argument("--model", type=Path, required=True)
        parser.add_argument("--runtime", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--consecutive-cache", action="store_true")
        args = parser.parse_args()
        result = prepare(args.output, args.build, args.model, args.runtime, consecutive_cache=args.consecutive_cache)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PreparationError as error:
        print(json.dumps({"prepared": False, "error": str(error)}, sort_keys=True))
    except KeyboardInterrupt:
        print(json.dumps({"prepared": False, "error": "package_preparation_interrupted"}, sort_keys=True))
    except Exception:
        print(json.dumps({"prepared": False, "error": "package_preparation_failed"}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
