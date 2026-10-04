"""Copy original source plus separately reviewed wrapper and resolved inputs.

No selected module is imported here. Every process remains a closed command
request to the ordinary owned preparation runner. Published generators retain
their exact bytes; the trusted copy seam is confined to this new component.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .contracts import PackageError, validate_package, MAX_LOCK_METADATA_BYTES
from .preparation_agent_adapter_store import CODE_PATH, _derived_draft, original_snapshot
from .preparation_agent_unlocked_store import FILES
from .preparation_agent_unlocked_contracts import DEPENDENCY_NAMESPACE
from .preparation_application_builder import _write_file, _private_directory
from .preparation_application_source import _open_directory, _digest
from .preparation_builder import _runtime, load_private_toolchain
from .preparation_contracts import PreparationError, canonical_bytes
from .recipes import _read


def _copied_draft(root, files):
    """Temporary byte proof for the complete private copy, including binaries."""
    records, metadata = [], []
    for name,data in sorted(files.items()):
        records.append({"path":name,"sha256":hashlib.sha256(data).hexdigest(),"size":len(data)})
        try:
            data.decode("utf-8")
            kind = "binary" if b"\0" in data else "text"
        except UnicodeError:
            kind = "binary"
        metadata.append({"path":name,"executable":False,"kind":kind})
    digest = _digest(records,metadata)
    return {"source":{"kind":"application_directory","contract_version":2,"snapshot_path":str(root),
        "files":records,"file_metadata":metadata,"bytes":sum(map(len,files.values())),"digest":digest,"revision":"sha256:"+digest}}


def copy_unlocked_snapshot(export, destination, result, derived_path, check):
    snapshot, baseline = original_snapshot(export, check)
    derived_records = [{"path": FILES[item.name], "sha256": item.sha256, "size": item.size} for item in result.derived_inputs]
    derived, derived_baseline = original_snapshot(_derived_draft(derived_path, sorted(derived_records, key=lambda item: item["path"])), check)
    code = derived.by_path()[CODE_PATH]
    files = {item.path: item.data for item in snapshot.files}
    if any(path.split("/")[0] in {CODE_PATH, CODE_PATH[:-3], DEPENDENCY_NAMESPACE} for path in files):
        raise PreparationError("agent_adapter_namespace_collision")
    files[CODE_PATH] = code.data
    for name in ("pyproject.toml", "uv.lock"):
        files[DEPENDENCY_NAMESPACE + "/" + name] = derived.by_path()[name].data
    records = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in sorted(files.items())]
    parent = root = None
    try:
        parent = _open_directory(Path(destination).parent)
        _private_directory(parent)
        os.mkdir(Path(destination).name, mode=0o700, dir_fd=parent)
        root = os.open(Path(destination).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        identity = _private_directory(root)
        for name, data in files.items():
            check(); _write_file(root, name, data)
        copied, _ = original_snapshot(_copied_draft(Path(destination), files), check)
        if {item.path: item.data for item in copied.files} != files:
            raise PreparationError("agent_adapter_input_changed")
        visible = _open_directory(Path(destination))
        try:
            if _private_directory(visible) != identity:
                raise PreparationError("agent_adapter_input_changed")
        finally:
            os.close(visible)
        if original_snapshot(export, check)[1] != baseline or original_snapshot(
                _derived_draft(derived_path, sorted(derived_records, key=lambda item: item["path"])), check)[1] != derived_baseline:
            raise PreparationError("agent_adapter_input_changed")
        os.fsync(root); check()
        return records
    finally:
        for descriptor in (root, parent):
            if descriptor is not None:
                os.close(descriptor)


def prepare_agent_unlocked_adapter(descriptor, draft, result, derived_path, toolchain, output, run, check, *, acquire_wheels):
    generator_name = "mcpme._agent_adapter_generator_" + uuid.uuid4().hex
    try:
        output = Path(output)
        if not output.is_absolute() or output.parent.resolve() != output.parent or output.exists():
            raise PreparationError("private_preparation_workspace_required")
        current = load_private_toolchain(toolchain.root.parent, check)
        if current.identity != toolchain.identity:
            raise PreparationError("preparation_toolchain_changed")
        original_snapshot(draft, check)
        work = output.parent / (output.name + "-builder")
        work.mkdir(mode=0o700)
        exported, tools, access, fixtures = (work / name for name in ("setup-export.json", "tools.json", "access.json", "validation.json"))
        for path, value in ((exported, {"api_version": 1, "draft_id": draft["draft_id"], "revision": draft["revision"],
                "source": draft["source"], "requirements": draft["requirements"]}),
                (tools, [item.model_dump(exclude_none=True) for item in descriptor.tools]),
                (access, descriptor.access.model_dump()), (fixtures, {"fixtures": [
                    {"tool": item.tool, "arguments": item.arguments, "expected": item.expected} for item in descriptor.validation[:4]]})):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        implementation_path = toolchain.root / "templates/prepare.py"
        implementation = _read(implementation_path, 1024 * 1024)
        if hashlib.sha256(implementation).hexdigest() != current.artifacts["templates/prepare.py"]["sha256"]:
            raise PreparationError("preparation_toolchain_changed")
        spec = importlib.util.spec_from_file_location(generator_name, implementation_path)
        if spec is None:
            raise PreparationError("private_preparation_toolchain_required")
        generator = importlib.util.module_from_spec(spec)
        sys.modules[generator_name] = generator
        exec(compile(implementation, str(implementation_path), "exec"), generator.__dict__)
        generator.source_snapshot = lambda export, destination, check_canceled=None: copy_unlocked_snapshot(
            export, destination, result, derived_path, check)
        project = output / "source" / DEPENDENCY_NAMESPACE
        args = argparse.Namespace(template="python-function", setup_export=exported, tool_spec=tools,
            output=output, package_id=descriptor.package.id, name=descriptor.package.name, version=descriptor.package.version,
            source_license=result.specification.source_license, dependency_project=project, memory_mb=descriptor.resources.memory_mb,
            max_sessions=descriptor.lifecycle.max_sessions, timeout_seconds=descriptor.lifecycle.operation_timeout_seconds,
            access_spec=access, configuration_spec=None, validation_spec=fixtures, entrypoint_arg=[], extra_executable=[],
            module=result.specification.module, function=result.specification.function, script=None, executable=None, http_factory=None)
        def runtime(destination, dependency_project):
            if hashlib.sha256(_read(dependency_project / "uv.lock", 16 * 1024 * 1024)).hexdigest() != next(item.sha256 for item in result.derived_inputs if item.name == "dependency-lock"):
                raise PreparationError("dependency_lock_mismatch")
            return _runtime(destination, dependency_project, current, run, check, work, acquire_wheels)
        package = generator.prepare(args, runtime_builder=runtime, check_canceled=check,
            lifecycle_override=descriptor.lifecycle.model_dump())
        # The generated wrapper is a derived artifact, never an original file.
        # Preserve this distinction in the package's provenance and licenses.
        source_receipt = json.loads(_read(output / "preparation.json", 1024 * 1024))
        source_receipt["files"] = draft["source"]["files"]
        source_receipt["derived_collection"] = result.collection.model_dump()
        (output / "preparation.json").write_bytes(canonical_bytes(source_receipt))
        license_data = _read(derived_path / "LICENSE", 16384)
        license_path = "licenses/agent-generated-LICENSE.txt"
        (output / license_path).write_bytes(license_data)
        (output / "agent-adapter-derivation.json").write_bytes(canonical_bytes({"result_digest": result.digest,
            "request": result.request.model_dump(mode="json"), "collection": result.collection.model_dump(),
            "derived_inputs": [item.model_dump(mode="json") for item in result.derived_inputs]}))
        manifest = package.manifest.model_dump(exclude_none=True)
        manifest["licenses"].append(license_path)
        manifest_data = canonical_bytes(manifest)
        (output / "mcpme.package.json").write_bytes(manifest_data)
        lock = package.lock.model_dump()
        lock["manifest_sha256"] = hashlib.sha256(manifest_data).hexdigest()
        source_label = "agent-derived:sha256:" + result.collection.digest
        derived_paths = {"source/" + CODE_PATH, "source/"+DEPENDENCY_NAMESPACE+"/pyproject.toml",
            "source/"+DEPENDENCY_NAMESPACE+"/uv.lock", "agent-adapter-derivation.json", license_path}
        records = {item["path"]: item for item in lock["artifacts"]}
        for name in ("preparation.json", *sorted(derived_paths)):
            data = _read(output / name, 16 * 1024**2 if name.endswith("/uv.lock") else 1024 * 1024)
            (output / name).chmod(0o600)
            dependency = name in {"source/"+DEPENDENCY_NAMESPACE+"/pyproject.toml", "source/"+DEPENDENCY_NAMESPACE+"/uv.lock"}
            records[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "executable": False,
                "source": source_label if name in derived_paths else draft["source"]["revision"],
                "provenance": "Host-generated project and lock bound to reviewed resolver receipt" if dependency else
                    "Untrusted proposed adapter bound to reviewed attempt and original source" if name in derived_paths
                    else "Original immutable source and separate derived collection",
                "license": "licenses/template-LICENSE.txt" if dependency else license_path if name in derived_paths
                    else "source/" + result.specification.source_license}
        lock["artifacts"] = [records[name] for name in sorted(records)]
        lock["build"].update(recipe="agent-unlocked-python-adapter-v1 1.0.0", builder="mcpme governed unlocked adapter builder v1")
        # Package locks bind the complete runtime closure and have their own
        # existing larger bound; preparation-contract limits do not apply.
        lock_data = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        if len(lock_data) > MAX_LOCK_METADATA_BYTES:
            raise PreparationError("preparation_package_lock_limit")
        (output / "mcpme.lock.json").write_bytes(lock_data)
        check()
        return validate_package(output)
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, (PreparationError, PackageError)):
            raise
        if error.__class__.__name__ == "PreparationError" and re.fullmatch(r"[a-z][a-z0-9_]{1,127}", str(error)):
            raise PreparationError(str(error)) from None
        raise PreparationError("agent_adapter_build_failed") from None
    finally:
        sys.modules.pop(generator_name, None)
