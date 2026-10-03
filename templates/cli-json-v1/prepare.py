"""Unregistered CLI package spike; no automatic capability is enabled here."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from mcpme.contracts import validate_package
from mcpme.preparation_cli import parse_cli_spec
from mcpme.preparation_components import read_snapshot
from mcpme.preparation_contracts import PreparationError


def prepare(export_path, output, *, reaper, launcher, package_id, name, version="1.0.0", runtime_builder=None,
            cli_spec=None, memory_mb=128):
    module_spec = importlib.util.spec_from_file_location("mcpme_cli_base_generator", ROOT / "templates/prepare.py")
    base = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(base)
    export = base.read_json(export_path)
    if output.exists() or output.is_symlink():
        raise PreparationError("destination_exists")
    snapshot = read_snapshot(export).by_path()
    if cli_spec is None:
        contract = snapshot.get("mcpme.cli.json")
        if contract is None:
            raise PreparationError("cli_json_contract_required")
        spec = parse_cli_spec(contract.data)
    else:
        spec = parse_cli_spec(cli_spec)
    if spec.script not in snapshot or spec.source_license not in snapshot or not snapshot[spec.source_license].size:
        raise PreparationError("cli_source_binding_required")
    spec_bytes = json.dumps(spec.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":")).encode()
    # This builder requires host-supplied reviewed helpers. It never compiles or
    # executes selected source, invokes a shell, or downloads a command runtime.
    for helper in (reaper, launcher):
        if helper.resolve() != helper or not helper.is_file() or not os.access(helper, os.X_OK):
            raise PreparationError("cli_native_helper_required")
    tools_path = export_path.parent / (".cli-tool-" + hashlib.sha256(spec_bytes).hexdigest() + ".json")
    if tools_path.exists() or tools_path.is_symlink():
        raise PreparationError("cli_temporary_destination_exists")
    descriptor = os.open(tools_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as tool_file:
        json.dump(spec.tool.model_dump(exclude_none=True), tool_file)
    args = SimpleNamespace(setup_export=export_path, tool_spec=tools_path, output=output,
        package_id=package_id, name=name, version=version, template="existing-mcp", module=None,
        script=spec.script, executable=None, extra_executable=[], function=None, http_factory=None,
        entrypoint_arg=[], source_license=spec.source_license, dependency_project=ROOT,
        memory_mb=memory_mb, max_sessions=1, timeout_seconds=min(300, spec.deadline_seconds + 5),
        access_spec=None, configuration_spec=None, validation_spec=None)
    try:
        base.prepare(args, runtime_builder=runtime_builder)
        shutil.copyfile(Path(__file__).parent / "adapter.py", output / "adapter.py")
        (output / "cli.json").write_bytes(spec_bytes)
        runtime = output / "runtime/mcpme"
        runtime.mkdir(parents=True, mode=0o700)
        modules = ["__init__.py", "cli_execution.py", "preparation_cli.py", "preparation_contracts.py",
                   "contracts.py", "limits.py", "private.py", "ownership_receipts.py", "processes.py"]
        for module in modules:
            shutil.copyfile(ROOT / "backend/mcpme" / module, runtime / module)
        for helper, filename in ((reaper, "mcpme-reap"), (launcher, "mcpme-cli-launch")):
            shutil.copyfile(helper, output / "bin" / filename)
            (output / "bin" / filename).chmod(0o700)
        bootstrap = (output / "bootstrap.py").read_text()
        before = "runpy.run_path(str(root / 'source' / " + repr(spec.script) + "), run_name='__main__')"
        after = "sys.path.insert(0, str(root / 'runtime'))\nrunpy.run_path(str(root / 'adapter.py'), run_name='__main__')"
        if before not in bootstrap:
            raise PreparationError("cli_bootstrap_mismatch")
        (output / "bootstrap.py").write_text(bootstrap.replace(before, after))
        (output / "CLI_SUPPORT.md").write_text(
            "# CLI spike support boundary\n\n"
            "This package exercises macos-no-fork-v1 with a private CPython stdlib command, "
            "fixed argv and one-shot JSON stdin/stdout. The native profile permits package and system reads, "
            "denies forks and network, and permits writes only to /dev/null. Runtime metadata is written by "
            "the trusted adapter to an explicitly supplied owner-only state directory.\n\n"
            "A3 remains open for descendant-capable commands, installed-host wiring, live aggregate memory "
            "enforcement and independent release acceptance. This package does not enable automatic preparation.\n")
        manifest = base.read_json(output / "mcpme.package.json")
        records = []
        for path in sorted(output.rglob("*")):
            if path.is_dir():
                path.chmod(0o700)
                continue
            if path.is_symlink() or not path.is_file():
                raise PreparationError("unsafe_generated_cli_artifact")
            relative = path.relative_to(output).as_posix()
            path.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)
            if relative in {"mcpme.package.json", "mcpme.lock.json"}:
                continue
            license_path = ("source/" + spec.source_license if relative.startswith("source/")
                else "licenses/DEPENDENCIES.md" if relative.startswith("python/site-packages/")
                else "licenses/python-LICENSE.txt" if relative.startswith("python/") else "licenses/template-LICENSE.txt")
            records.append({"path": relative, "sha256": base.digest(path), "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
                "source": ("uv.lock:sha256:" + base.digest(output / "uv.lock") if relative.startswith("python/site-packages/")
                    else "uv-managed:cpython-" + base.PYTHON_VERSION if relative.startswith("python/")
                    else export["source"]["revision"] if relative.startswith("source/") else "mcpme-cli-json-v1-host-component"),
                "provenance": "CLI spike: exact source snapshot, reviewed native helpers, private locked Python closure.",
                "license": license_path})
        lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": version,
            "manifest_sha256": base.digest(output / "mcpme.package.json"), "artifacts": records,
            "build": {"recipe": "cli-json-v1 no-fork support-matrix spike", "builder": "mcpme CLI spike",
                      "toolchain": "private CPython 3.12.13; locked MCP 1.30.0; reviewed macOS native helpers"}}
        (output / "mcpme.lock.json").write_text(json.dumps(lock, separators=(",", ":")) + "\n")
        (output / "mcpme.lock.json").chmod(0o600)
        return validate_package(output)
    except BaseException:
        # Only remove this invocation's new output; pre-existing outputs are
        # rejected by the base generator before any mutation.
        if output.exists() and (output / "CLI_SUPPORT.md").exists():
            shutil.rmtree(output)
        raise
    finally:
        tools_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup-export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reaper", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.setup_export.absolute(), args.output.absolute(), reaper=args.reaper.absolute(),
            launcher=args.launcher.absolute(), package_id=args.package_id, name=args.name)
        print(json.dumps({"prepared": True, "package_digest": result.digest, "artifacts": len(result.lock.artifacts),
                          "release_gate": "A3-open"}))
        return 0
    except Exception:
        print(json.dumps({"prepared": False, "error": "cli_package_preparation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
