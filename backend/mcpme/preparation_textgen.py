"""Trusted composite llama.cpp engine and bounded text-generation task family.

Captured models are separately pinned durable inputs. Folder/document records
always describe the original snapshot. The runner owns commands, acquisition,
probe processes, cancellation and release of job references.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import Access, PackageManifest, Tool, validate_package
from .preparation_artifacts import CapturedArtifactCollectionV1
from .preparation_builder import _copy, _digest, load_private_toolchain
from .preparation_textgen_signed_runtime import (ASSET_DIRECTORY, copy_runtime,
    manifest as signed_manifest, verify_runtime as verify_signed_runtime)
from .preparation_capture import ArtifactReferenceV1
from .preparation_components import (ComponentReference, PreparationDescriptorV2, SourceBinding,
    SourceFileBinding, original_file_records, parse_component_descriptor, read_snapshot)
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .recipes import _read
from mcpme_textgen.fixture import (MODEL_BYTES, MODEL_SHA256, LICENSE_SHA256, MODEL_FILENAME,
    TOKENIZER_SHA256, digest, tokenizer_metadata)

ENGINE_ID = "llamacpp-cpu-v1"
TASK_ID = "text-generate-v1"
VERSION = "1.0.8"
MIN_MEMORY_MB = 1024
ASSETS = ("LICENSE", "LICENSE-MODEL", "engine-closure.json", "fixtures.json", "model.json", "model-provenance.json", "launcher.c",
          "tools.json", "pyproject.toml", "uv.lock", "signed-runtime.json", "signed-runtime-provenance.json")
ENGINE_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["model_name", "license_name", "profile"], "properties": {
        "model_name": {"type": "string", "minLength": 1, "maxLength": 64},
        "license_name": {"type": "string", "minLength": 1, "maxLength": 64},
        "profile": {"const": "qwen2.5-0.5b-q4_k_m-cpu-text-v1"}}}
TASK_INPUT = {"context_tokens": 512, "max_output_tokens": 64, "queue_limit": 4}
TASK_SCHEMA = {"type": "object", "additionalProperties": False, "required": list(TASK_INPUT),
               "properties": {key: {"type": "integer", "const": value} for key, value in TASK_INPUT.items()}}
PROCESS_GRAPH = [
    {"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/textgen-engine", "args": [], "cwd": "."},
     "depends_on": [], "readiness": {"path": "/props", "echo_generation": False}},
    {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/textgen-adapter", "args": [], "cwd": "."},
     "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}]


def asset_root():
    return Path(__file__).resolve().parents[2] / "templates" / ASSET_DIRECTORY


def assets(root=None):
    return {name: _read((root or asset_root()) / name, 4 * 1024**2) for name in ASSETS}


def task_tools():
    return [Tool.model_validate(item) for item in json.loads(assets()["tools.json"])]


def task_fixtures():
    return json.loads(assets()["fixtures.json"])


def model_contract():
    return json.loads(assets()["model.json"])


def builtin_textgen_components():
    root = Path(__file__).resolve().parents[1]
    # Include the complete reviewed host module closure: the builder/verifier
    # transitively use contracts, recipes, capture, receipt and ownership code.
    # A hand-maintained direct-import list silently missed those dependencies.
    # Exact wheel/private-Python/native-reaper bytes are independently bound by
    # the job's dependency, toolchain and verifier runtime identities.
    sources = [*sorted((root / "mcpme").glob("*.py")), root / "mcpme_local_transport.py",
        *sorted((root / "mcpme_textgen").glob("*.py")),
        *[root / "jevk5_app" / name for name in ("__init__.py", "server.py", "contracts.py", "state.py")]]
    identity = {"implementation": {p.relative_to(root).as_posix(): hashlib.sha256(_read(p, 1024**2)).hexdigest()
                                   for p in sources},
                "assets": {name: hashlib.sha256(data).hexdigest() for name, data in assets().items()}}
    catalog = {identifier + "@" + VERSION: {"digest": contract_digest("mcpme-" + identifier, identity),
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}
        for identifier in (ENGINE_ID, TASK_ID)}
    catalog[TASK_ID + "@" + VERSION]["dependencies"] = [{"component": ENGINE_ID, "version": VERSION,
        "digest": catalog[ENGINE_ID + "@" + VERSION]["digest"]}]
    return catalog


def references():
    catalog = builtin_textgen_components()
    return tuple(ComponentReference(id=name, version=VERSION, digest=catalog[name + "@" + VERSION]["digest"])
                 for name in (ENGINE_ID, TASK_ID))


def captured_collection(draft):
    try:
        source = draft["source"]
        if source["kind"] == "captured_artifact_collection":
            selected = source
            if type(source.get("contract_version")) is not int or source["contract_version"] != 1:
                raise PreparationError("textgen_capture_required")
        elif source["kind"] in {"local_folder", "document"}:
            selected = draft["captured_artifacts"]
            if (set(selected) != {"schema_version", "kind", "digest", "collection"}
                    or type(selected.get("schema_version")) is not int or selected["schema_version"] != 1):
                raise PreparationError("textgen_capture_required")
        else:
            raise PreparationError("textgen_capture_required")
        if selected["kind"] != "captured_artifact_collection":
            raise PreparationError("textgen_capture_required")
        result = CapturedArtifactCollectionV1.model_validate(selected["collection"])
        if selected["digest"] != result.digest or (source["kind"] == "captured_artifact_collection"
                and source["revision"] != "sha256:" + result.digest):
            raise PreparationError("artifact_identity_mismatch")
        return result
    except PreparationError:
        raise
    except (KeyError, ValueError, TypeError):
        raise PreparationError("textgen_capture_required") from None


def model_selection(collection):
    models = [e for e in collection.entries if e.role == "model-weights"]
    licenses = [e for e in collection.entries if e.role == "license"]
    if len(collection.entries) != 2 or len(models) != 1 or len(licenses) != 1:
        raise PreparationError("textgen_artifact_selection_unsupported")
    model, license = models[0], licenses[0]
    if (model.format != "gguf" or model.sha256 != MODEL_SHA256 or model.size != MODEL_BYTES
            or license.sha256 != LICENSE_SHA256 or len(model.licenses) != 1
            or (model.licenses[0].name, model.licenses[0].sha256) != (license.name, LICENSE_SHA256)):
        raise PreparationError("textgen_model_identity_unsupported")
    return {"model_name": model.name, "license_name": license.name, "profile": model_contract()["profile"]}


def validate_descriptor(descriptor, draft):
    if not isinstance(descriptor, PreparationDescriptorV2):
        raise PreparationError("unsupported_component_descriptor")
    collection = captured_collection(draft)
    selected = {item.component.key: item.input for item in descriptor.components}
    engine, task = references()
    expected = {engine.key: model_selection(collection), task.key: TASK_INPUT}
    if (len(descriptor.components) != 2 or set(selected) != set(expected)
            or any(canonical_bytes(selected[key]) != canonical_bytes(expected[key]) for key in expected)):
        raise PreparationError("component_input_mismatch")
    if descriptor.tools != task_tools() or [v.model_dump(exclude_none=True) for v in descriptor.validation] != task_fixtures():
        raise PreparationError("textgen_task_contract_mismatch")
    if (descriptor.resources.memory_mb < MIN_MEMORY_MB or descriptor.resources.gpu
            or descriptor.access != Access(network=["host-owned loopback engine"])
            or descriptor.lifecycle.max_request_bytes != 32768 or descriptor.lifecycle.max_sessions != 8
            or descriptor.lifecycle.operation_timeout_seconds != 60
            or descriptor.lifecycle.startup_timeout_seconds != 90 or descriptor.lifecycle.stop_grace_seconds != 5):
        raise PreparationError("textgen_resource_contract_mismatch")
    if descriptor.artifacts or descriptor.inputs:
        raise PreparationError("textgen_unbound_inputs")
    if draft["source"]["kind"] in {"local_folder", "document"}:
        read_snapshot(draft)
    return collection


class LlamaCppEngineComponent:
    input_schema = ENGINE_SCHEMA
    legacy_interface_kinds = frozenset()

    def __init__(self):
        self.reference = references()[0]

    def validate_source(self, descriptor, draft, recipe):
        collection = validate_descriptor(descriptor, draft)
        records = tuple(SourceFileBinding(item["path"], item["sha256"], item["size"])
                        for item in original_file_records(draft))
        return SourceBinding(*self.reference.key, draft["source"]["revision"], records,
            contract_digest("textgen-model-service-v1", {"model": model_contract(), "processes": PROCESS_GRAPH,
                "tools": [tool.model_dump(exclude_none=True) for tool in descriptor.tools]}),
            None,
            canonical_bytes({"collection_digest": collection.digest, "model_sha256": MODEL_SHA256,
                "tokenizer_sha256": TOKENIZER_SHA256, "dependency_lock_sha256": hashlib.sha256(assets()["uv.lock"]).hexdigest(),
                "publisher_trust": "publisher_unverified"}))


class TextGenerateComponent(LlamaCppEngineComponent):
    input_schema = TASK_SCHEMA

    def __init__(self):
        self.reference = references()[1]


def textgen_descriptor(draft, recipe, *, name="Local text generation", memory_mb=MIN_MEMORY_MB):
    engine, task = references()
    return {"schema_version": 2,
        "recipe": {"id": recipe["manifest"]["id"], "version": recipe["manifest"]["version"], "digest": recipe["digest"]},
        "package": {"id": "local.app-" + draft["draft_id"], "version": VERSION, "name": name},
        "components": [{"component": engine.model_dump(), "input": model_selection(captured_collection(draft))},
                       {"component": task.model_dump(), "input": TASK_INPUT}],
        "tools": [tool.model_dump(exclude_none=True) for tool in task_tools()],
        "resources": {"memory_mb": memory_mb}, "access": {"network": ["host-owned loopback engine"]},
        "lifecycle": {"max_request_bytes": 32768, "max_sessions": 8, "operation_timeout_seconds": 60,
                      "startup_timeout_seconds": 90, "stop_grace_seconds": 5}, "validation": task_fixtures()}


def write_textgen_recipe(destination):
    from .preparation_contracts import RecipeManifest
    from .recipes import validate_bundle
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent or destination.exists():
        raise PreparationError("recipe_destination_exists")
    manifest = RecipeManifest.model_validate({"schema_version": 1, "id": "mcpme.text-generation", "version": VERSION,
        "kind": "declarative", "worker_api_version": 1,
        "input_schema": {"type": "object", "additionalProperties": False}, "form": [],
        "steps": [{"component": ref.id, "version": ref.version, "digest": ref.digest} for ref in references()],
        "requirements": {"capabilities": ["declarative-composition"]}, "licenses": ["LICENSE", "LICENSE-MODEL"],
        "provenance": {"origin": "mcpme exact CPU llama.cpp and bounded text generation components", "publisher": "mcpme contributors"}})
    files = {name: assets()[name] for name in ("LICENSE", "LICENSE-MODEL", "model.json", "model-provenance.json", "engine-closure.json",
                                            "signed-runtime-provenance.json")}
    signed = signed_manifest()
    files["runtime-profile.json"] = canonical_bytes({key: signed[key] for key in
        ("schema_version", "profile", "archive", "signing", "provenance_sha256")} | {
        "manifest_sha256": hashlib.sha256(assets()["signed-runtime.json"]).hexdigest()})
    files["mcpme.recipe.json"] = canonical_bytes(manifest.model_dump(exclude_none=True))
    files["mcpme.recipe.lock.json"] = canonical_bytes({"schema_version": 1,
        "manifest_sha256": hashlib.sha256(files["mcpme.recipe.json"]).hexdigest(),
        "artifacts": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "license": "LICENSE-MODEL" if name in {"LICENSE-MODEL", "model.json"} else "LICENSE",
            "origin": "Pinned host text generation assets"} for name, data in sorted(files.items()) if name != "mcpme.recipe.json"]})
    destination.mkdir(mode=0o700)
    for name, data in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        (destination / name).chmod(0o600)
    return validate_bundle(destination)


class TextGenerationFamily:
    source_kind = "captured_artifact_collection"
    target_profile = "model-service"

    def __init__(self, capture_store):
        self.capture_store = capture_store
        self.components = (LlamaCppEngineComponent(), TextGenerateComponent())
        self.component = self.components[1]
        self.form_provider = TextGenerationFormProvider()

    def supports_source(self, draft):
        return draft.get("source", {}).get("kind") == self.source_kind or (
            draft.get("source", {}).get("kind") in {"local_folder", "document"} and "captured_artifacts" in draft)

    def validate_inputs(self, descriptor, draft):
        collection = validate_descriptor(descriptor, draft)
        for entry in collection.entries:
            with self.capture_store.open_artifact(collection.digest, entry.name):
                pass
        return collection

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import CollectionReference, RuntimeReference, adapt_descriptor, bind_preparation_inputs
        collection = self.validate_inputs(descriptor, draft)
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        return bind_preparation_inputs(view, draft=draft,
            captured_collection=CollectionReference(kind="captured_artifact_collection", schema_version=1, digest=collection.digest),
            runtimes=[RuntimeReference(name="private-python-toolchain", digest=runtime_digest)], capabilities=["build", "probe"],
            expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="model-service"))

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        return prepare_textgen_bound(descriptor, draft, toolchain, output, run, check,
            acquire_wheels=acquire_wheels, job_id=job_id, capture_store=self.capture_store)

    async def verify(self, package_root, descriptor, reaper, workspace, *, timeout_seconds=60, owner=None):
        from .preparation_textgen_verifier import verify_textgen_package
        from .preparation_textgen_probe_confinement import TextgenProbeConfinementV1
        return await verify_textgen_package(package_root, descriptor, reaper, workspace,
            timeout_seconds=timeout_seconds, owner=owner, confinement_factory=TextgenProbeConfinementV1.from_host)

    def release(self, job_id, descriptor=None):
        with self.capture_store.imports._lock:
            rows = self.capture_store.db.execute(
                "SELECT digest FROM captured_artifact_references WHERE kind='job' AND owner_id=? LIMIT 65", (job_id,)).fetchall()
        if len(rows) > 64:
            raise PreparationError("textgen_job_reference_invalid")
        for row in rows:
            self.capture_store.release_reference(row[0], ArtifactReferenceV1(kind="job", owner_id=job_id))

    def inspect_source(self, draft):
        result = {"schema_version": 1, "source_revision": draft["source"]["revision"], "candidates": [], "blockers": []}
        try:
            collection = captured_collection(draft)
            selection = model_selection(collection)
            result["candidates"] = [{"candidate_id": contract_digest("textgen-candidate-v1", {
                "source_revision": draft["source"]["revision"], "capture": collection.digest,
                "components": [c.reference.model_dump() for c in self.components], "selection": selection}),
                "kind": "llamacpp-text-generation", "module": "Qwen2.5 0.5B CPU", "unresolved": [],
                "tools": [tool.model_dump(exclude_none=True) for tool in task_tools()], "collection_digest": collection.digest}]
        except PreparationError as failure:
            result["blockers"] = [failure.code]
        return result


class TextGenerationFormProvider:
    def accepts(self, candidate, recipe):
        return candidate["kind"] == "llamacpp-text-generation" and {
            (step["component"], step["version"], step["digest"]) for step in recipe["manifest"]["steps"]} == {
                ref.key for ref in references()}

    def fields(self, draft, candidate, recipe):
        from .preparation_forms import _field
        model_selection(captured_collection(draft))
        requirements = draft.get("requirements", {})
        ceiling = min(4096, requirements.get("memory_mb", 4096))
        blockers = ["resource_requirement_mismatch"] if ceiling < MIN_MEMORY_MB else []
        fields = [_field("textgen-name", label="App name", kind="string", value=draft.get("name", "Local text generation"),
                         constraints={"min_length": 1, "max_length": 256}),
                  _field("textgen-memory", label="Reserved memory (MiB)", kind="integer", value=MIN_MEMORY_MB,
                         constraints={"minimum": MIN_MEMORY_MB, "maximum": max(MIN_MEMORY_MB, ceiling)})]
        if requirements.get("profile") not in {None, "model-service"}:
            blockers.append("unsupported_execution_profile")
        if requirements.get("tools") and requirements["tools"] != [tool.name for tool in task_tools()]:
            blockers.append("tool_requirement_mismatch")
        if requirements.get("secret_references"):
            blockers.append("secret_reference_unavailable")
        return fields, {fields[0].id: ("name",), fields[1].id: ("memory",)}, blockers

    def resolve(self, context, values):
        selected = {binding[0]: values[field_id] for field_id, binding in context.bindings.items() if field_id in values}
        return textgen_descriptor(context.draft, context.recipe_record, name=selected["name"], memory_mb=selected["memory"])

    def restore_fields(self, fields, bindings, descriptor):
        value = parse_component_descriptor(descriptor)
        return [field.model_copy(update={"value": value.package.name if bindings[field.id][0] == "name"
                    else min(value.resources.memory_mb, field.constraints["maximum"])}) for field in fields]


def _copy_capture(store, collection, entry, destination, check):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    hasher, count = hashlib.sha256(), 0
    with store.open_artifact(collection.digest, entry.name) as descriptor:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with destination.open("xb") as stream:
            while chunk := os.read(descriptor, 1024**2):
                check(); count += len(chunk)
                if count > entry.size:
                    raise PreparationError("artifact_identity_mismatch")
                hasher.update(chunk); stream.write(chunk)
            stream.flush(); os.fsync(stream.fileno())
    if count != entry.size or hasher.hexdigest() != entry.sha256:
        raise PreparationError("artifact_identity_mismatch")


def validate_model_package(package, descriptor, *, require_receipt=False):
    """Bind topology/model/runtime to the component, beyond the base contract."""
    selected = {item.component.key: item.input for item in descriptor.components}
    engine, task = references()
    if (set(selected) != {engine.key, task.key}
            or canonical_bytes(selected[task.key]) != canonical_bytes(TASK_INPUT)):
        raise PreparationError("component_input_mismatch")
    manifest = package.manifest
    if (manifest.profile != "model-service" or manifest.adapter_process != "adapter" or manifest.model is None
            or canonical_bytes(manifest.model.model_dump(exclude_none=True)) != canonical_bytes(model_contract())
            or [p.model_dump(exclude_none=True) for p in manifest.processes] != PROCESS_GRAPH
            or manifest.tools != descriptor.tools or manifest.lifecycle != descriptor.lifecycle
            or manifest.access != descriptor.access or manifest.resources.model_dump() != descriptor.resources.model_dump()
            or (manifest.package_id, manifest.version, manifest.name) != (descriptor.package.id, descriptor.package.version, descriptor.package.name)):
        raise PreparationError("textgen_package_contract_mismatch")
    records = {item.path: item for item in package.lock.artifacts}
    closure = json.loads(assets()["engine-closure.json"])
    if {name for name in records if name.startswith("llama/")} != {
            name for name in closure if name.startswith("llama/") and name != "llama/llama-server"}:
        raise PreparationError("textgen_engine_identity_mismatch")
    for name, expected in closure.items():
        if name == "llama/llama-server":
            if name in records:
                raise PreparationError("textgen_legacy_supervisor_forbidden")
            continue
        if name not in records or (records[name].sha256, records[name].size) != (expected["sha256"], expected["bytes"]):
            raise PreparationError("textgen_engine_identity_mismatch")
    model = records.get("models/" + MODEL_FILENAME)
    if model is None or (model.sha256, model.size) != (MODEL_SHA256, MODEL_BYTES):
        raise PreparationError("textgen_model_identity_unsupported")
    collection = CapturedArtifactCollectionV1.model_validate(json.loads(_read(package.root / "capture.json", 1024**2)))
    if canonical_bytes(selected[engine.key]) != canonical_bytes(model_selection(collection)):
        raise PreparationError("textgen_capture_binding_mismatch")
    license = records.get("licenses/Qwen-LICENSE")
    if license is None or license.sha256 != LICENSE_SHA256:
        raise PreparationError("textgen_model_identity_unsupported")
    root = Path(__file__).resolve().parents[1]
    source_paths = [*sorted((root / "mcpme_textgen").glob("*.py")),
                   *[root / "jevk5_app" / name for name in ("__init__.py", "server.py", "contracts.py", "state.py")]]
    expected_sources = {"backend/" + path.relative_to(root).as_posix(): hashlib.sha256(_read(path, 1024**2)).hexdigest()
                        for path in source_paths}
    if {path for path in records if path.startswith("backend/")} != set(expected_sources):
        raise PreparationError("textgen_adapter_identity_mismatch")
    if any(records[path].sha256 != expected for path, expected in expected_sources.items()):
        raise PreparationError("textgen_adapter_identity_mismatch")
    origin = json.loads(_read(package.root / "origin.json", 1024**2))
    if (origin.get("engine_closure") != closure
            or origin.get("launcher_source_sha256") != hashlib.sha256(assets()["launcher.c"]).hexdigest()):
        raise PreparationError("textgen_launcher_identity_mismatch")
    verify_signed_runtime(package.root)
    if require_receipt:
        from .preparation_inputs import parse_preparation_receipt
        from .preparations import RECEIPT_NAME
        receipt = parse_preparation_receipt(_read(package.root / RECEIPT_NAME, 1024**2))
        if (receipt.schema_version != 2 or receipt.inputs.descriptor_digest != descriptor.digest
                or receipt.inputs.recipe != descriptor.recipe
                or receipt.binding.source_revision != manifest.source_revision
                or receipt.inputs.captured_collection is None
                or receipt.inputs.captured_collection.digest != collection.digest
                or {item.key for item in receipt.inputs.components} != set(selected)):
            raise PreparationError("textgen_receipt_binding_mismatch")


def prepare_textgen_bound(descriptor, draft, toolchain, output, run, check, *, capture_store, job_id, acquire_wheels):
    try:
        value = descriptor if isinstance(descriptor, PreparationDescriptorV2) else parse_component_descriptor(descriptor)
        collection = validate_descriptor(value, draft)
        if not output.is_absolute() or output.parent.resolve() != output.parent or output.exists():
            raise PreparationError("private_preparation_workspace_required")
        current = load_private_toolchain(toolchain.root.parent, check)
        if current.identity != toolchain.identity:
            raise PreparationError("preparation_toolchain_changed")
        selected_assets = assets(current.root / "templates" / ASSET_DIRECTORY)
        if selected_assets != assets():
            raise PreparationError("textgen_template_identity_mismatch")
        runtime_records = {name.removeprefix("textgen-runtime/"): item for name, item in current.artifacts.items()
                           if name.startswith("textgen-runtime/")}
        root = Path(__file__).resolve().parents[1]
        source_names = {"backend/" + path.relative_to(root).as_posix() for path in (root / "mcpme_textgen").glob("*.py")}
        source_names.update("backend/jevk5_app/" + name for name in ("__init__.py", "server.py", "contracts.py", "state.py"))
        expected_runtime = set(signed_manifest()["artifacts"])
        if set(runtime_records) != expected_runtime:
            raise PreparationError("textgen_private_engine_required")
        capture_store.acquire_reference(collection.digest, ArtifactReferenceV1(kind="job", owner_id=job_id))
        output.mkdir(mode=0o700)
        work = output.parent / (output.name + "-builder")
        work.mkdir(mode=0o700)
        runtime = copy_runtime(current.root / "textgen-runtime", output, check=check)
        for name in source_names:
            check()
            _copy(root / name.removeprefix("backend/"), output / name, check)
        # Bind copied adapter/policy code to the same trusted component identity.
        for path in (output / "backend").rglob("*.py"):
            if _digest(path, check) != _digest(root / path.relative_to(output / "backend"), check):
                raise PreparationError("textgen_adapter_identity_mismatch")
        for entry in collection.entries:
            target = output / ("models/" + MODEL_FILENAME if entry.role == "model-weights" else "licenses/Qwen-LICENSE")
            _copy_capture(capture_store, collection, entry, target, check)
        if digest(tokenizer_metadata(output / "models" / MODEL_FILENAME)) != TOKENIZER_SHA256:
            raise PreparationError("textgen_tokenizer_identity_mismatch")
        (output / "licenses/LICENSE").write_bytes(selected_assets["LICENSE"])
        (output / "capture.json").write_bytes(canonical_bytes(collection.model_dump(mode="json")))
        (output / "model-provenance.json").write_bytes(selected_assets["model-provenance.json"])
        manifest = PackageManifest.model_validate({"schema_version": 1, "host_api_version": 1,
            "package_id": value.package.id, "version": value.package.version, "name": value.package.name,
            "source_revision": draft["source"]["revision"], "profile": "model-service",
            "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "processes": PROCESS_GRAPH, "adapter_process": "adapter", "model": model_contract(),
            "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
            "lifecycle": value.lifecycle.model_dump(), "tools": [tool.model_dump(exclude_none=True) for tool in value.tools],
            "resources": value.resources.model_dump(), "access": value.access.model_dump(),
            "validation": {"fixtures": [{"tool": f.tool, "arguments": f.arguments, "expected": f.expected} for f in value.validation]},
            "licenses": [p.relative_to(output).as_posix() for p in sorted((output / "licenses").rglob("*")) if p.is_file()]})
        (output / "mcpme.package.json").write_bytes(canonical_bytes(manifest.model_dump(exclude_none=True)))
        records = []
        for path in sorted(output.rglob("*")):
            check()
            if path.is_dir():
                path.chmod(0o700); continue
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise PreparationError("unsafe_generated_artifact")
            path.chmod(0o700 if details.st_mode & 0o111 else 0o600)
            name = path.relative_to(output).as_posix()
            if name == "mcpme.package.json":
                continue
            license = ("licenses/Qwen-LICENSE" if name.startswith("models/") else
                       "licenses/DEPENDENCIES.md" if name.startswith("python/site-packages/") else
                       "licenses/python-LICENSE.txt" if name.startswith("python/") else
                       "licenses/openssl-LICENSE.txt" if name.startswith(("llama/lib/libcrypto", "llama/lib/libssl")) else
                       "licenses/libomp-LICENSE.TXT" if name.startswith("llama/lib/libomp") else
                       "licenses/ggml-LICENSE" if name.startswith(("llama/lib/libggml", "llama/plugins/")) else
                       "licenses/llama.cpp-LICENSE" if name.startswith("llama/") else
                       name if name.startswith("licenses/") else "licenses/LICENSE")
            records.append({"path": name, "sha256": _digest(path, check), "size": details.st_size,
                "executable": bool(details.st_mode & 0o111), "source": draft["source"]["revision"],
                "provenance": "Pinned host engine/task closure; separately captured exact model; frozen copied wheels.", "license": license})
        lock = {"schema_version": 1, "package_id": manifest.package_id, "version": manifest.version,
            "manifest_sha256": _digest(output / "mcpme.package.json", check), "artifacts": records,
            "build": {"recipe": "mcpme.text-generation " + VERSION, "builder": "mcpme controlled text generation v1",
                      "toolchain": runtime + "; private-toolchain:sha256:" + current.identity}}
        (output / "mcpme.lock.json").write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n")
        package = validate_package(output)
        validate_model_package(package, value)
        return package
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise PreparationError("textgen_preparation_failed") from None
