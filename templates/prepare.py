"""Explicit external-agent package preparation; never called by host intake/install."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from mcpme.contracts import (  # noqa: E402
    Access, Configuration, Lifecycle, PackageError, PackageManifest, Resources, Tool, Validation,
    package_relative_path, validate_package,
)

PYTHON_VERSION = "3.12.13"
SDK_VERSION = "1.30.0"
TEMPLATE_VERSION = 1


class PreparationError(ValueError):
    """Content-free errors suitable for the agent handoff."""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise PreparationError("invalid_preparation_specification")
            value[key] = item
        return value

    if path.stat().st_size > 1024 * 1024:
        raise PreparationError("preparation_specification_too_large")
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          PreparationError("invalid_preparation_specification")))


def run(*args: str) -> str:
    """Capture and discard execution diagnostics; never replay raw build errors."""
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=False)
    if result.returncode:
        raise PreparationError("preparation_command_failed")
    return result.stdout.strip()


def source_snapshot(export: dict, destination: Path) -> list[dict]:
    """Copy only exact exported files, without importing or executing the source."""
    source = export["source"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}|[0-9a-f]{40}", source["revision"]):
        raise PreparationError("source_unresolved")
    snapshot = Path(source["snapshot_path"])
    if not snapshot.is_absolute() or snapshot.is_symlink() or not snapshot.is_dir():
        raise PreparationError("source_snapshot_required")
    records = source["files"]
    if not isinstance(records, list) or not records or len(records) > 2048:
        raise PreparationError("invalid_source_file_list")
    canonical = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (source.get("digest", canonical) != canonical
            or source["revision"].startswith("sha256:") and source["revision"] != "sha256:" + canonical):
        raise PreparationError("source_identity_mismatch")
    if sum(record["size"] for record in records) > 64 * 1024 * 1024:
        raise PreparationError("source_size_exceeded")
    seen = set()
    destination.mkdir(mode=0o700)
    for record in records:
        relative = record["path"]
        package_relative_path(relative)
        if relative in seen:
            raise PreparationError("invalid_source_file_list")
        seen.add(relative)
        if (not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or type(record["size"]) is not int or not 0 <= record["size"] <= 16 * 1024 * 1024):
            raise PreparationError("invalid_source_file_list")
        path = snapshot / relative
        for parent in path.parents:
            if parent == snapshot:
                break
            if parent.is_symlink():
                raise PreparationError("unsafe_source_file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PreparationError("unsafe_source_file")
            data = stream.read(record["size"] + 1)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(getattr(before, key) != getattr(after, key)
                   or getattr(after, key) != getattr(current, key) for key in attrs):
                raise PreparationError("source_snapshot_changed")
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise PreparationError("source_snapshot_changed")
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.write_bytes(data)
        output.chmod(0o600)
    return records


def prepare_runtime(destination: Path, dependency_project: Path) -> str:
    """Seal managed CPython and hash-locked wheels; no user interpreter at runtime."""
    if not (dependency_project / "uv.lock").is_file() or not (dependency_project / "pyproject.toml").is_file():
        raise PreparationError("locked_dependency_project_required")
    executable = Path(run("uv", "python", "find", "--no-project", "--managed-python", PYTHON_VERSION)).resolve()
    runtime = executable.parent.parent
    shutil.copytree(runtime, destination / "python", symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    requirements = destination / "requirements.txt"
    run("uv", "export", "--project", str(dependency_project), "--locked", "--no-dev",
        "--no-emit-project", "--format", "requirements.txt", "--output-file", str(requirements))
    # Wheels only, hashes required, and copies instead of links to the builder's cache.
    run("uv", "pip", "install", "--python", str(executable), "--target",
        str(destination / "python/site-packages"), "--require-hashes", "--only-binary", ":all:",
        "--link-mode", "copy", "--requirement", str(requirements))
    shutil.copyfile(dependency_project / "uv.lock", destination / "uv.lock")
    shutil.copyfile(dependency_project / "pyproject.toml", destination / "dependency-project.toml")
    version = run(str(destination / "python/bin/python3.12"), "-I", "-S", "-B", "-c",
                  "import sys; sys.path.insert(0, sys.argv[1]); "
                  "from importlib.metadata import version; print(version('mcp'))",
                  str(destination / "python/site-packages"))
    if version != SDK_VERSION:
        raise PreparationError("unsupported_mcp_sdk_version")
    (destination / "python-runtime-origin.json").write_text(json.dumps({
        "python_version": PYTHON_VERSION, "managed_interpreter_sha256": digest(executable),
        "uv_version": run("uv", "--version"), "uv_lock_sha256": digest(destination / "uv.lock"),
        "mcp_sdk_version": SDK_VERSION, "installation": "hash-checked locked wheels; copied private runtime",
    }, indent=2) + "\n")
    license_dir = destination / "licenses"
    shutil.copyfile(runtime / "lib/python3.12/LICENSE.txt", license_dir / "python-LICENSE.txt")
    (license_dir / "DEPENDENCIES.md").write_text(
        "# Locked Python dependencies\n\n"
        "The private interpreter is CPython " + PYTHON_VERSION + ". Its license is python-LICENSE.txt.\n\n"
        "Exact wheel versions, locators and hashes are recorded in uv.lock and requirements.txt. "
        "Wheel license files and metadata remain in python/site-packages/*.dist-info. "
        "The MCP Python SDK is pinned to " + SDK_VERSION + ".\n")
    return run(str(destination / "python/bin/python3.12"), "--version")


def write_launcher(destination: Path, *, module: str | None = None,
                   script: str | None = None, callable_adapter: bool = False, http_adapter: bool = False):
    (destination / "bin").mkdir(mode=0o700)
    launcher = destination / "bin/service"
    launcher.write_text('#!/bin/sh\nset -eu\n'
                        'BIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                        'PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)\n'
                        'exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" "$@" 2>/dev/null\n')
    launcher.chmod(0o700)
    bootstrap = [
        '"""Relocatable private-runtime bootstrap. No checkout or global site packages."""',
        "import logging, os, runpy, sys", "from pathlib import Path",
        "root = Path(__file__).resolve().parent", "logging.disable(logging.CRITICAL)",
        "sys.dont_write_bytecode = True",
        "sys.path[:0] = [str(root / 'python/site-packages'), str(root / 'source')]",
        "os.chdir(root / 'source')",
    ]
    if http_adapter:
        sys_path = "sys.path.insert(0, str(root))"
        bootstrap.extend([sys_path, "runpy.run_path(str(root / 'http_runtime.py'), run_name='__main__')"])
    elif callable_adapter:
        bootstrap.append("runpy.run_path(str(root / 'adapter.py'), run_name='__main__')")
    elif module:
        bootstrap.append("runpy.run_module(" + repr(module) + ", run_name='__main__', alter_sys=True)")
    else:
        bootstrap.append("runpy.run_path(str(root / 'source' / " + repr(script) + "), run_name='__main__')")
    (destination / "bootstrap.py").write_text("\n".join(bootstrap) + "\n")


def prepare(args: argparse.Namespace):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise PreparationError("unsupported_build_platform")
    export = read_json(args.setup_export)
    if not isinstance(export, dict) or type(export["revision"]) is not int or export["revision"] < 1:
        raise PreparationError("invalid_setup_export")
    requirements = export.get("requirements", {})
    is_http = bool(getattr(args, "http_factory", None))
    profile = "mcp-http" if is_http else "mcp-stdio"
    if requirements.get("profile") not in {None, profile}:
        raise PreparationError("unsupported_execution_profile")
    if is_http and (args.template != "existing-mcp" or not args.module or args.script or args.executable
                    or args.entrypoint_arg or args.extra_executable
                    or not re.fullmatch(r"[A-Za-z_]\w*", args.http_factory)):
        raise PreparationError("http_factory_contract_required")
    if requirements.get("secret_references") and (not is_http or not isinstance(requirements["secret_references"], dict)):
        raise PreparationError("secret_reference_unavailable")
    tools_raw = read_json(args.tool_spec)
    tools = [Tool.model_validate(value) for value in (tools_raw if isinstance(tools_raw, list) else [tools_raw])]
    if args.template == "python-function" and len(tools) != 1:
        raise PreparationError("one_callable_tool_required")
    if args.module and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", args.module):
        raise PreparationError("invalid_python_module")
    if args.template == "python-function" and not re.fullmatch(r"[A-Za-z_]\w*", args.function):
        raise PreparationError("invalid_python_callable")
    access = Access.model_validate(read_json(args.access_spec) if args.access_spec else {})
    configuration = Configuration.model_validate(read_json(args.configuration_spec) if args.configuration_spec else {})
    validation = Validation.model_validate(read_json(args.validation_spec) if args.validation_spec else {})
    memory = args.memory_mb or requirements.get("memory_mb") or 128
    resources = Resources(memory_mb=memory, cpu_only=True)
    lifecycle = Lifecycle(max_sessions=args.max_sessions, operation_timeout_seconds=args.timeout_seconds)
    destination = args.output.absolute()
    if destination.exists():
        raise PreparationError("destination_exists")
    destination.mkdir(parents=True, mode=0o700)
    records = source_snapshot(export, destination / "source")
    names = {record["path"] for record in records}
    package_relative_path(args.source_license)
    if args.source_license not in names or not (destination / "source" / args.source_license).stat().st_size:
        raise PreparationError("source_license_required")
    (destination / "licenses").mkdir(mode=0o700)
    shutil.copyfile(ROOT / "templates/LICENSE", destination / "licenses/template-LICENSE.txt")
    receipt = {"template": args.template + "-v1", "template_version": TEMPLATE_VERSION,
               "draft_id": export["draft_id"], "revision": export["revision"],
               "source_revision": export["source"]["revision"], "files": records}
    (destination / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    licenses = ["source/" + args.source_license, "licenses/template-LICENSE.txt"]
    if args.executable:
        package_relative_path(args.executable)
        if args.executable not in names:
            raise PreparationError("entrypoint_source_required")
        for name in [args.executable, *args.extra_executable]:
            package_relative_path(name)
            if name not in names:
                raise PreparationError("entrypoint_source_required")
            (destination / "source" / name).chmod(0o700)
        entrypoint = {"executable": "source/" + args.executable, "args": args.entrypoint_arg,
                      "cwd": "source"}
        toolchain = "prebuilt executable; selected artifacts verified against exported snapshot"
    else:
        if args.module:
            module_path = args.module.replace(".", "/")
            if module_path + ".py" not in names and module_path + "/__init__.py" not in names:
                raise PreparationError("entrypoint_source_required")
        if args.script:
            package_relative_path(args.script)
            if args.script not in names:
                raise PreparationError("entrypoint_source_required")
        toolchain = prepare_runtime(destination, args.dependency_project.absolute())
        licenses.extend(["licenses/python-LICENSE.txt", "licenses/DEPENDENCIES.md"])
        is_callable = args.template == "python-function"
        write_launcher(destination, module=args.module, script=args.script, callable_adapter=is_callable, http_adapter=is_http)
        if is_callable:
            shutil.copyfile(ROOT / "templates/python-function-v1/adapter.py", destination / "adapter.py")
            (destination / "callable.json").write_text(json.dumps({"module": args.module,
                "function": args.function, "tool": tools[0].model_dump(exclude_none=True)}) + "\n")
        elif is_http:
            shutil.copyfile(ROOT / "backend/mcpme/http_service.py", destination / "http_runtime.py")
            shutil.copyfile(ROOT / "backend/mcpme/policy.py", destination / "http_policy.py")
            (destination / "http-factory.json").write_text(json.dumps({"module": args.module, "factory": args.http_factory}) + "\n")
        entrypoint = {"executable": "bin/service", "args": args.entrypoint_arg, "cwd": "."}
    execution = ({"processes": [{"id": "service", "role": "adapter", "entrypoint": entrypoint, "depends_on": [],
                                 "readiness": {"path": "/health", "echo_generation": True}}],
                  "adapter_process": "service", "http": {"configuration": "descriptor-v1", "session_termination": "delete"}}
                 if is_http else {"entrypoint": entrypoint})
    manifest = PackageManifest.model_validate({
        "schema_version": 1, "host_api_version": 1, "package_id": args.package_id,
        "version": args.version, "source_revision": export["source"]["revision"],
        "name": args.name, "profile": profile,
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        **execution, "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "lifecycle": lifecycle.model_dump(), "tools": [tool.model_dump(exclude_none=True) for tool in tools],
        "access": access.model_dump(), "configuration": configuration.model_dump(), "validation": validation.model_dump(),
        "resources": resources.model_dump(), "licenses": licenses,
    })
    (destination / "mcpme.package.json").write_text(json.dumps(manifest.model_dump(exclude_none=True), indent=2) + "\n")
    artifacts = []
    for path in sorted(destination.rglob("*")):
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            if path.is_symlink():
                raise PreparationError("unsafe_generated_artifact")
            path.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)
            relative = path.relative_to(destination).as_posix()
            if relative == "mcpme.package.json":
                continue
            license_path = ("source/" + args.source_license if relative.startswith("source/")
                            else "licenses/DEPENDENCIES.md" if relative.startswith("python/site-packages/")
                            else "licenses/python-LICENSE.txt" if relative.startswith("python/")
                            else "licenses/template-LICENSE.txt")
            artifacts.append({"path": relative, "sha256": digest(path), "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
                "source": "uv.lock:sha256:" + digest(destination / "uv.lock") if relative.startswith("python/site-packages/")
                          else "uv-managed:cpython-" + PYTHON_VERSION if relative.startswith("python/")
                          else manifest.source_revision,
                "provenance": "Exported source hashes; versioned adapter; pinned private runtime and uv wheel closure.",
                "license": license_path})
    lock = {"schema_version": 1, "package_id": manifest.package_id, "version": manifest.version,
            "manifest_sha256": digest(destination / "mcpme.package.json"), "artifacts": artifacts,
            "build": {"recipe": "templates/prepare.py " + args.template + " v1",
                      "builder": "mcpme external-agent preparation v1", "toolchain": toolchain}}
    (destination / "mcpme.lock.json").write_text(json.dumps(lock, separators=(",", ":")) + "\n")
    return validate_package(destination)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="template", required=True)
    for name in ("python-function", "existing-mcp"):
        command = commands.add_parser(name)
        command.add_argument("--setup-export", type=Path, required=True)
        command.add_argument("--tool-spec", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--package-id", required=True)
        command.add_argument("--name", required=True)
        command.add_argument("--version", default="1.0.0")
        command.add_argument("--source-license", required=True)
        command.add_argument("--dependency-project", type=Path, default=ROOT)
        command.add_argument("--memory-mb", type=int)
        command.add_argument("--max-sessions", type=int, default=8)
        command.add_argument("--timeout-seconds", type=int, default=60)
        command.add_argument("--access-spec", type=Path)
        command.add_argument("--configuration-spec", type=Path, help="Closed typed nonsecret configuration declaration JSON.")
        command.add_argument("--validation-spec", type=Path, help="Closed synthetic smoke fixture JSON, at most four declared tool calls.")
        command.add_argument("--entrypoint-arg", action="append", default=[])
        command.add_argument("--extra-executable", action="append", default=[])
        if name == "python-function":
            command.add_argument("--module", required=True)
            command.add_argument("--function", required=True)
            command.set_defaults(script=None, executable=None, http_factory=None)
        else:
            entry = command.add_mutually_exclusive_group(required=True)
            entry.add_argument("--module")
            entry.add_argument("--script")
            entry.add_argument("--executable")
            command.add_argument("--http-factory", help="Selected module factory returning a tools-only low-level MCP Server; prepare one private HTTP resident.")
            command.set_defaults(function=None)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        package = prepare(args)
        print(json.dumps({"prepared": True, "package_digest": package.digest,
                          "artifacts": len(package.lock.artifacts)}))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, PackageError) else (
            str(error) if isinstance(error, PreparationError) else "package_preparation_failed")
        print(json.dumps({"prepared": False, "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
