"""Real SDK/relocation acceptance for the unregistered CLI no-fork spike.

Uses a private locked runtime, separate actual native executable topology
fixtures, and package-owned native receipts. Prints only bounded metadata.
This does not close A3's descendant-capable or installed-host acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcpme.contracts import validate_package
from mcpme.preparation_cli import CliJsonSpec, CliJsonComponent, inspect_cli_directory
from mcpme.preparation_cli_forms import CliJsonFormProvider
from mcpme.preparation_components import parse_component_descriptor, ComponentRegistry
from mcpme.preparation_contracts import RecipeReference
from mcpme.private import read_private_json
from mcpme.ownership_receipts import verify_receipt


def compile_helper(source, output):
    result = subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(ROOT / source),
                             "-o", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError("cli_fixture_compile_failed")
    output.chmod(0o700)


async def verify(base):
    started = time.monotonic()
    source = base / "original"
    source.mkdir(mode=0o700)
    script = "\n".join(["import json,os,socket,sys", "arguments=json.load(sys.stdin)",
        "denied=[]", "try:", "    os.fork()", "except PermissionError:", "    denied.append('fork')",
        "try:", "    open(" + repr(str(source / "LICENSE")) + ").read()", "except PermissionError:",
        "    denied.append('original_read')", "except FileNotFoundError:", "    denied.append('original_removed')",
        "try:", "    open('forbidden','w').write('fixture')", "except PermissionError:", "    denied.append('write')",
        "try:", "    socket.create_connection(('127.0.0.1',9),timeout=0.1)", "except PermissionError:",
        "    denied.append('network')", "print(json.dumps({'value':arguments['value'],'argument':sys.argv[1],'denied':denied}))"])
    (source / "cli.py").write_text(script + "\n")
    shutil.copyfile(ROOT / "templates/LICENSE", source / "LICENSE")
    contract = CliJsonSpec.model_validate({"schema_version": 1, "runtime": "private-python-stdlib-v1",
        "script": "cli.py", "source_license": "LICENSE", "argv": [{"field": "value"}],
        "topology": "macos-no-fork-v1", "effects": "package-read-only-no-network-v1",
        "tool": {"name": "fixture", "description": "Literal argv and JSON fixture",
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                "value": {"type": "string", "maxLength": 4096}}, "required": ["value"]},
            "output_schema": {"type": "object", "additionalProperties": False, "properties": {
                "value": {"type": "string"}, "argument": {"type": "string"},
                "denied": {"type": "array", "items": {"type": "string"}}}, "required": ["value", "argument", "denied"]}}})
    records = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
               for path in sorted(source.iterdir())]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    export = {"draft_id": "00000000-0000-4000-8000-000000000001", "revision": 1, "name": "CLI fixture", "intent": "Literal argv and JSON fixture",
        "source": {"kind": "local_folder", "snapshot_path": str(source), "files": records, "digest": digest,
                   "revision": "sha256:" + digest}, "requirements": {"profile": "mcp-stdio", "memory_mb": 128}}
    candidates = inspect_cli_directory(export)
    if len(candidates) != 1 or candidates[0]["execution_available"]:
        raise ValueError("cli_static_candidate_mismatch")
    component = CliJsonComponent()
    reference = RecipeReference(id="mcpme.cli-json", version="1.0.0", digest="d" * 64)
    recipe = {"digest": reference.digest, "manifest": {"id": reference.id, "version": reference.version,
        "steps": [{"component": component.reference.id, "version": component.reference.version, "digest": component.reference.digest}]}}
    provider = CliJsonFormProvider()
    fields, bindings, blockers = provider.fields(export, candidates[0], recipe)
    if blockers != ["cli_topology_acceptance_required"]:
        raise ValueError("cli_generated_form_mismatch")
    answers = {field.id: field.value for field in fields}
    for identity, binding in bindings.items():
        key = binding[0]
        if key in {"input_schema", "output_schema"}:
            answers[identity] = contract.tool.model_dump()[key]
        elif key == "tool_name":
            answers[identity] = contract.tool.name
        elif key == "argv":
            answers[identity] = {"argv": [item.model_dump() for item in contract.argv]}
        elif key in {"arguments", "expected"}:
            answers[identity] = {"value": "fixture"} if key == "arguments" else {
                "value": "fixture", "argument": "fixture", "denied": ["fork", "original_removed", "write", "network"]}
    context = SimpleNamespace(bindings=bindings, candidate=candidates[0], form=SimpleNamespace(recipe=reference, import_id=export["draft_id"]))
    descriptor = parse_component_descriptor(provider.resolve(context, answers))
    ComponentRegistry((component,)).validate_source(descriptor, export, recipe)
    export_path = base / "export.json"
    export_path.write_text(json.dumps(export))
    reaper, launcher = base / "mcpme-reap", base / "mcpme-cli-launch"
    compile_helper("scripts/mcpme-reap.c", reaper)
    compile_helper("scripts/mcpme-cli-launch.c", launcher)
    specification = importlib.util.spec_from_file_location("cli_spike_preparer", ROOT / "templates/cli-json-v1/prepare.py")
    preparer = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(preparer)
    output = base / "build/package"
    package = await asyncio.to_thread(preparer.prepare, export_path, output, reaper=reaper, launcher=launcher,
        package_id="test.cli-json", name="CLI fixture", cli_spec=descriptor.components[0].input["spec"])
    declared = package.manifest.tools[0].mcp_schema()
    relocated = base / "unrelated/relocated-package"
    relocated.parent.mkdir(mode=0o700)
    shutil.copytree(output, relocated)
    for path in (output.parent, source, export_path, reaper, launcher):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    if validate_package(relocated).digest != package.digest:
        raise ValueError("cli_relocation_identity_mismatch")
    state = base / "invocation-state"
    state.mkdir(mode=0o700)
    unrelated = base / "unrelated/cwd"
    unrelated.mkdir(mode=0o700)
    literal = "$(false); `false`; *; $HOME"
    calls = 0
    for value in (literal, "restart-fixture"):
        parameters = StdioServerParameters(command=str(relocated / "bin/service"), args=[], cwd=str(unrelated),
            env={"MCPME_CLI_STATE_DIRECTORY": str(state), "PATH": "/usr/bin:/bin", "HOME": str(base / "private-home")})
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                if initialized.protocolVersion != "2025-11-25":
                    raise ValueError("cli_protocol_mismatch")
                tools = await client.list_tools()
                if len(tools.tools) != 1 or tools.tools[0].model_dump(by_alias=True, exclude_none=True) != declared:
                    raise ValueError("cli_full_tool_contract_mismatch")
                result = await client.call_tool("fixture", {"value": value})
                if (result.isError or result.structuredContent != {"value": value, "argument": value,
                        "denied": ["fork", "original_removed", "write", "network"]}):
                    raise ValueError("cli_actual_result_mismatch")
                calls += 1
        journal = read_private_json(state / "invocations.json")
        if any(item["state"] != "settled" or item["reserved_memory_mb"] != 0
               or not verify_receipt(state, item["receipt"]) for item in journal["entries"]):
            raise ValueError("cli_durable_settlement_failed")
    if validate_package(relocated).digest != package.digest:
        raise ValueError("cli_package_mutated")
    return {"verified": True, "support_matrix": "macos-no-fork-v1", "A3": "open",
        "sdk_calls": calls, "restart": True, "relocation": True, "original_source_removed": True,
        "global_tools_excluded": True, "package_unchanged": True, "pending_receipts": 0,
        "generated_questions": True, "source_manifest_required": False,
        "reserved_memory_mb": 0, "declared_memory_mb": 128,
        "memory_enforcement": "post-exit-foreground-measurement-only",
        "measured_foreground_peak_bytes": max(item["measurements"]["peak_memory_bytes"] for item in journal["entries"]),
        "package_digest": package.digest, "artifacts": len(package.lock.artifacts),
        "elapsed_ms": int((time.monotonic() - started) * 1000)}


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Retain a new explicit workspace for inspection")
    args = parser.parse_args()
    try:
        if args.keep:
            base = args.keep.absolute()
            base.mkdir(mode=0o700)
            result = asyncio.run(verify(base))
        else:
            with tempfile.TemporaryDirectory(prefix="mcpme-cli-acceptance-") as temporary:
                result = asyncio.run(verify(Path(temporary).resolve()))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"verified": False, "error": "cli_spike_verification_failed", "A3": "open"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
