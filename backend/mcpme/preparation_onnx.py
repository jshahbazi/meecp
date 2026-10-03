"""Host-owned pinned CPU ONNX recipe. Binary inputs never enter text snapshots.

This builder launches nothing itself. The durable runner owns command execution,
acquisition, probing, thread cancellation and the lifetime of the job reference.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .contracts import Access, PackageManifest, Tool, validate_package
from .preparation_artifacts import CapturedArtifactCollectionV1
from .preparation_builder import CommandSpec, _copy, _digest, _runtime, load_private_toolchain
from .preparation_capture import ArtifactReferenceV1
from .preparation_components import (ComponentReference, PreparationDescriptorV2,
    SourceBinding, SourceFileBinding, parse_component_descriptor)
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .recipes import _read

COMPONENT_ID = "onnx-hotdog-v1"
COMPONENT_VERSION = "1.0.0"
MODEL_SHA256 = "121b1f682e2df1aff1c16dacb7ce4bedc26df016a500b18743349c16724ad85d"
MODEL_BYTES = 10701557
MODEL_LICENSE_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
MIN_MEMORY_MB = 512
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 1024 * 1024
ASSETS = ("LICENSE", "LICENSE-MODEL", "LICENSE-FLATBUFFERS", "LICENSE-HTTPX", "THIRD_PARTY_NOTICES.md", "adapter.py", "detector.py",
          "fixtures.json", "model-metadata.json", "model-provenance.json", "pyproject.toml", "tool.json", "uv.lock", "verify_task.py")
INPUT_SCHEMA = {"type": "object", "properties": {
    "model_name": {"type": "string", "minLength": 1, "maxLength": 64},
    "license_names": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 64},
                      "minItems": 1, "maxItems": 8, "uniqueItems": True},
    "task": {"type": "string", "const": "coco-hotdog"},
    "profile": {"type": "string", "const": "cpu-stdio"}},
    "required": ["model_name", "license_names", "task", "profile"], "additionalProperties": False}


def _asset_root():
    return Path(__file__).resolve().parents[2] / "templates/onnx-hotdog-v1"


def _assets(root):
    return {name: _read(root / name, 4 * 1024 * 1024) for name in ASSETS}


def _supplement_dependency_notices(site, assets, check_canceled):
    for distribution, name in (("flatbuffers-25.12.19.dist-info", "LICENSE-FLATBUFFERS"),
                               ("httpx-0.28.1.dist-info", "LICENSE-HTTPX")):
        location = site / distribution
        if not location.is_dir() or location.is_symlink():
            raise PreparationError("onnx_dependency_identity_mismatch")
        check_canceled()
        (location / "LICENSE-mcpme-supplement.txt").write_bytes(assets[name])
    # This exact frozen wheel retains its notices in the import package rather
    # than its distribution metadata. Index copies of both actual wheel texts;
    # keep the original files as well. No runtime package is imported here.
    location = site / "onnxruntime-1.30.0.dist-info"
    if not location.is_dir() or location.is_symlink():
        raise PreparationError("onnx_dependency_identity_mismatch")
    for source, target in (("LICENSE", "LICENSE-mcpme-wheel.txt"),
                           ("ThirdPartyNotices.txt", "NOTICE-mcpme-wheel.txt")):
        check_canceled()
        content = _read(site / "onnxruntime" / source, 4 * 1024 * 1024)
        if not content:
            raise PreparationError("dependency_notice_required")
        (location / target).write_bytes(content)


def builtin_onnx_components():
    assets = _assets(_asset_root())
    identity = contract_digest("mcpme-onnx-hotdog-component-v1", {
        "implementation": hashlib.sha256(_read(Path(__file__), 1024 * 1024)).hexdigest(),
        "assets": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(assets.items())}})
    return {COMPONENT_ID + "@" + COMPONENT_VERSION: {
        # Recipe step config is distinct from the component's captured-input
        # selection schema. No recipe bundle can configure executable hooks.
        "digest": identity, "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}}


def component_reference():
    return ComponentReference(id=COMPONENT_ID, version=COMPONENT_VERSION,
        digest=builtin_onnx_components()[COMPONENT_ID + "@" + COMPONENT_VERSION]["digest"])


def write_onnx_recipe(destination):
    """Generate the host's immutable declarative bundle; no caller code hooks."""
    from .preparation_contracts import RecipeManifest
    from .recipes import validate_bundle
    if (not destination.is_absolute() or destination.parent.resolve() != destination.parent
            or destination.exists() or destination.is_symlink()):
        raise PreparationError("recipe_destination_exists")
    reference = component_reference()
    manifest = RecipeManifest.model_validate({"schema_version": 1, "id": "mcpme.onnx-hotdog", "version": "1.0.0",
        "kind": "declarative", "worker_api_version": 1,
        "input_schema": {"type": "object", "additionalProperties": False}, "form": [],
        "steps": [{"component": reference.id, "version": reference.version, "digest": reference.digest}],
        "requirements": {"capabilities": ["declarative-composition"]}, "licenses": ["LICENSE", "LICENSE-MODEL"],
        "provenance": {"origin": "mcpme host-owned pinned ONNX hotdog component", "publisher": "mcpme contributors"}})
    assets = _assets(_asset_root())
    files = {"LICENSE": assets["LICENSE"], "LICENSE-MODEL": assets["LICENSE-MODEL"],
        "model-provenance.json": assets["model-provenance.json"],
        "README.md": ("# CPU hotdog recipe\n\nThis host-owned recipe accepts the exact captured YOLO11n ONNX export "
                      "and its full AGPL-3.0 license. The host supplies the adapter, frozen runtime closure and "
                      "synthetic MCP fixtures. A local or supplied hash proves bytes; publisher trust remains "
                      "unverified. Static recipe enablement does not enable execution.\n").encode(),
        "mcpme.recipe.json": canonical_bytes(manifest.model_dump(exclude_none=True))}
    lock = {"schema_version": 1, "manifest_sha256": hashlib.sha256(files["mcpme.recipe.json"]).hexdigest(),
            "artifacts": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "license": "LICENSE-MODEL" if name in {"LICENSE-MODEL", "model-provenance.json"} else "LICENSE",
                "origin": "mcpme host-owned ONNX recipe v1"} for name, data in sorted(files.items())
                if name != "mcpme.recipe.json"]}
    files["mcpme.recipe.lock.json"] = canonical_bytes(lock)
    destination.mkdir(mode=0o700)
    for name, data in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        (destination / name).chmod(0o600)
    return validate_bundle(destination)


def captured_collection(draft):
    try:
        source = draft["source"]
        if source["kind"] == "captured_artifact_collection":
            captured = source
            if type(source.get("contract_version")) is not int or source["contract_version"] != 1:
                raise PreparationError("onnx_captured_input_required")
        elif source["kind"] in {"local_folder", "document"}:
            captured = draft["captured_artifacts"]
            if (set(captured) != {"schema_version", "kind", "digest", "collection"}
                    or type(captured.get("schema_version")) is not int or captured["schema_version"] != 1):
                raise PreparationError("onnx_captured_input_required")
        else:
            raise PreparationError("onnx_captured_input_required")
        if captured["kind"] != "captured_artifact_collection":
            raise PreparationError("onnx_captured_input_required")
        collection = CapturedArtifactCollectionV1.model_validate(captured["collection"])
        if captured["digest"] != collection.digest:
            raise PreparationError("artifact_identity_mismatch")
        if source["kind"] == "captured_artifact_collection" and source["revision"] != "sha256:" + collection.digest:
            raise PreparationError("artifact_identity_mismatch")
        return collection
    except PreparationError:
        raise
    except (ValueError, KeyError, TypeError):
        raise PreparationError("onnx_captured_input_required") from None


def validate_captured_model(collection):
    """Eligibility for this exact reviewed export; format labels prove nothing."""
    if not isinstance(collection, CapturedArtifactCollectionV1):
        raise PreparationError("onnx_captured_input_required")
    models = [entry for entry in collection.entries if entry.role == "model-weights"]
    if len(models) != 1 or any(entry.role not in {"model-weights", "license"} for entry in collection.entries):
        raise PreparationError("onnx_artifact_selection_unsupported")
    model = models[0]
    if (model.format != "onnx" or model.sha256 != MODEL_SHA256 or model.size != MODEL_BYTES):
        raise PreparationError("onnx_model_identity_unsupported")
    if not model.licenses or any(ref.sha256 != MODEL_LICENSE_SHA256 for ref in model.licenses):
        raise PreparationError("onnx_model_license_required")
    return {"model_name": model.name, "license_names": sorted(ref.name for ref in model.licenses),
            "task": "coco-hotdog", "profile": "cpu-stdio"}


def onnx_hotdog_tool():
    return Tool.model_validate(json.loads(_read(_asset_root() / "tool.json", 64 * 1024)))


def onnx_hotdog_fixtures():
    return json.loads(_read(_asset_root() / "fixtures.json", 64 * 1024))


class OnnxHotdogComponent:
    input_schema = INPUT_SCHEMA
    legacy_interface_kinds = frozenset()

    def __init__(self):
        self.reference = component_reference()

    def validate_source(self, descriptor, draft, recipe):
        if not isinstance(descriptor, PreparationDescriptorV2):
            raise PreparationError("unsupported_component_descriptor")
        collection = captured_collection(draft)
        selection = validate_captured_model(collection)
        if (len(descriptor.components) != 1 or descriptor.components[0].component != self.reference
                or descriptor.components[0].input != selection):
            raise PreparationError("component_input_mismatch")
        if descriptor.tools != [onnx_hotdog_tool()]:
            raise PreparationError("onnx_task_schema_mismatch")
        if (descriptor.resources.memory_mb < MIN_MEMORY_MB or descriptor.resources.gpu
                or descriptor.lifecycle.max_sessions != 1
                or descriptor.lifecycle.max_request_bytes != 2 * 1024 * 1024
                or descriptor.lifecycle.operation_timeout_seconds != 60
                or descriptor.access != Access()):
            raise PreparationError("onnx_resource_contract_mismatch")
        if [item.model_dump(exclude_none=True) for item in descriptor.validation] != onnx_hotdog_fixtures():
            raise PreparationError("onnx_fixture_contract_mismatch")
        records = tuple(SourceFileBinding(entry.path, entry.sha256, entry.size) for entry in collection.entries)
        return SourceBinding(*self.reference.key, draft["source"]["revision"], records,
            contract_digest("onnx-hotdog-interface-v1", {"tools": [tool.model_dump(exclude_none=True) for tool in descriptor.tools],
                                                      "input": selection}), None,
            canonical_bytes({"family": "onnx", "collection_digest": collection.digest, "model_sha256": MODEL_SHA256,
                             "task": "coco-hotdog", "publisher_trust": "publisher_unverified"}))


def onnx_hotdog_descriptor(draft, recipe, *, name="Hotdog Detector", memory_mb=MIN_MEMORY_MB):
    """Host-generated complete descriptor; users choose an artifact, not a schema."""
    collection = captured_collection(draft)
    selection = validate_captured_model(collection)
    recipe_ref = {"id": recipe["manifest"]["id"], "version": recipe["manifest"]["version"], "digest": recipe["digest"]}
    return {"schema_version": 2, "recipe": recipe_ref,
        "package": {"id": "local.app-" + draft["draft_id"], "version": "1.0.0", "name": name},
        "components": [{"component": component_reference().model_dump(), "input": selection}],
        "tools": [onnx_hotdog_tool().model_dump(exclude_none=True)], "resources": {"memory_mb": memory_mb},
        "lifecycle": {"max_sessions": 1, "max_request_bytes": 2 * 1024 * 1024, "operation_timeout_seconds": 60},
        "validation": onnx_hotdog_fixtures()}


class OnnxHotdogFormProvider:
    def accepts(self, candidate, recipe):
        return (candidate["kind"] == "onnx-hotdog" and len(recipe["manifest"]["steps"]) == 1
                and recipe["manifest"]["steps"][0]["component"] == COMPONENT_ID)

    def fields(self, draft, candidate, recipe):
        from .preparation_forms import _field
        validate_captured_model(captured_collection(draft))
        requirements = draft.get("requirements", {})
        ceiling = min(4096, requirements.get("memory_mb", 4096))
        blockers = ["resource_requirement_mismatch"] if ceiling < MIN_MEMORY_MB else []
        fields = [_field("onnx-name", label="App name", kind="string", value="Hotdog Detector",
                         constraints={"min_length": 1, "max_length": 256}),
                  _field("onnx-memory", label="Reserved memory (MiB)", kind="integer", value=MIN_MEMORY_MB,
                         constraints={"minimum": MIN_MEMORY_MB, "maximum": max(MIN_MEMORY_MB, ceiling)})]
        if requirements.get("profile") not in {None, "mcp-stdio"}:
            blockers.append("unsupported_execution_profile")
        if requirements.get("tools") and requirements["tools"] != ["detect_hotdog"]:
            blockers.append("tool_requirement_mismatch")
        if requirements.get("secret_references"):
            blockers.append("secret_reference_unavailable")
        return fields, {fields[0].id: ("name",), fields[1].id: ("memory",)}, blockers

    def resolve(self, context, values):
        selected = {binding[0]: values[field_id] for field_id, binding in context.bindings.items() if field_id in values}
        return onnx_hotdog_descriptor(context.draft, context.recipe_record,
                                     name=selected["name"], memory_mb=selected["memory"])

    def restore_fields(self, fields, bindings, descriptor):
        value = parse_component_descriptor(descriptor)
        return [field.model_copy(update={"value": value.package.name if bindings[field.id][0] == "name"
                    else value.resources.memory_mb}) for field in fields]


def release_onnx_inputs(capture_store, job_id, collection_digest):
    """Call only after every build/probe/publication thread and process joins."""
    capture_store.release_reference(collection_digest, ArtifactReferenceV1(kind="job", owner_id=job_id))


class OnnxPreparationFamily:
    """Small host registration seam; the generic runner retains job authority."""
    source_kind = "captured_artifact_collection"

    def __init__(self, capture_store):
        self.capture_store = capture_store
        self.component = OnnxHotdogComponent()
        self.form_provider = OnnxHotdogFormProvider()

    def validate_inputs(self, descriptor, draft):
        self.component.validate_source(descriptor, draft, {})
        collection = captured_collection(draft)
        selection = validate_captured_model(collection)
        # The durable read lease guards GC while the entire actual closed tree
        # is verified. Model parsing is deferred to the private owned runtime.
        with self.capture_store.open_artifact(collection.digest, selection["model_name"]):
            pass
        return collection

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import (CollectionReference, RuntimeReference, adapt_descriptor,
                                         bind_preparation_inputs)
        collection = self.validate_inputs(descriptor, draft)
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        expected = view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio")
        return bind_preparation_inputs(view, draft=draft,
            captured_collection=CollectionReference(kind="captured_artifact_collection", schema_version=1,
                                                   digest=collection.digest),
            runtimes=[RuntimeReference(name="private-python-toolchain", digest=runtime_digest)],
            capabilities=["build", "probe"], expected_package_contract=expected)

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        return prepare_onnx_bound(descriptor, draft, toolchain, output, run, check,
            capture_store=self.capture_store, job_id=job_id, acquire_wheels=acquire_wheels)

    def release(self, job_id, descriptor=None):
        reference = ArtifactReferenceV1(kind="job", owner_id=job_id)
        # References survive host restart. Only the runner, after exact native
        # settlement/join, calls this matching family hook for the job owner.
        with self.capture_store.imports._lock:
            rows = self.capture_store.db.execute(
                "SELECT digest FROM captured_artifact_references WHERE kind='job' AND owner_id=? LIMIT 65", (job_id,)).fetchall()
        if len(rows) > 64:
            raise PreparationError("onnx_job_reference_invalid")
        for row in rows:
            self.capture_store.release_reference(row[0], reference)

    def inspect_source(self, draft):
        collection = captured_collection(draft)
        try:
            selection = validate_captured_model(collection)
        except PreparationError as failure:
            return {"schema_version": 1, "source_revision": draft["source"]["revision"], "candidates": [],
                    "blockers": [failure.code]}
        candidate_id = contract_digest("onnx-hotdog-candidate-v1", {
            "source_revision": draft["source"]["revision"], "collection_digest": collection.digest,
            "component": self.component.reference.model_dump(), "selection": selection})
        return {"schema_version": 1, "source_revision": draft["source"]["revision"], "blockers": [],
            "candidates": [{"candidate_id": candidate_id, "kind": "onnx-hotdog", "module": "YOLO11n CPU",
                            "tools": [onnx_hotdog_tool().model_dump(exclude_none=True)], "unresolved": [],
                            "collection_digest": collection.digest}]}


def _captured_copy(capture_store, collection, entry, target, check):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value, total = hashlib.sha256(), 0
    with capture_store.open_artifact(collection.digest, entry.name) as descriptor:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with target.open("xb") as stream:
            while chunk := os.read(descriptor, 1024 * 1024):
                check()
                total += len(chunk)
                if total > entry.size:
                    raise PreparationError("artifact_identity_mismatch")
                stream.write(chunk); value.update(chunk)
            stream.flush(); os.fsync(stream.fileno())
    check()
    if total != entry.size or value.hexdigest() != entry.sha256:
        raise PreparationError("artifact_identity_mismatch")


def prepare_onnx_bound(descriptor, draft, toolchain, output, run, check_canceled, *,
                       capture_store, job_id, acquire_wheels):
    """Prepare captured bytes with host-pinned code and owned command callbacks."""
    try:
        value = descriptor if isinstance(descriptor, PreparationDescriptorV2) else parse_component_descriptor(descriptor)
        component = OnnxHotdogComponent()
        component.validate_source(value, draft, {})
        collection = captured_collection(draft)
        if (not output.is_absolute() or output.parent.resolve() != output.parent or output.exists()
                or output.parent.is_symlink()):
            raise PreparationError("private_preparation_workspace_required")
        check_canceled()
        current = load_private_toolchain(toolchain.root.parent, check_canceled)
        if current.identity != toolchain.identity:
            raise PreparationError("preparation_toolchain_changed")
        asset_root = current.root / "templates/onnx-hotdog-v1"
        assets = _assets(asset_root)
        expected_assets = _assets(_asset_root())
        if assets != expected_assets:
            raise PreparationError("onnx_template_identity_mismatch")
        capture_store.acquire_reference(collection.digest, ArtifactReferenceV1(kind="job", owner_id=job_id))
        # The runner releases this durable reference after joined failure/success;
        # a failed builder must never prematurely unpin its still-owned input.
        output.mkdir(mode=0o700)
        (output / "source").mkdir(mode=0o700)
        (output / "licenses").mkdir(mode=0o700)
        work = output.parent / (output.name + "-builder")
        work.mkdir(mode=0o700)
        for name, data in assets.items():
            check_canceled()
            if name in {"adapter.py", "detector.py", "tool.json", "model-metadata.json", "model-provenance.json", "fixtures.json", "verify_task.py"}:
                (output / "source" / name).write_bytes(data)
            elif name in {"pyproject.toml", "uv.lock"}:
                (work / name).write_bytes(data)
            elif name.startswith("LICENSE"):
                (output / "licenses" / name).write_bytes(data)
            elif name == "THIRD_PARTY_NOTICES.md":
                (output / "licenses/ONNX-NOTICES.md").write_bytes(data)
        for entry in collection.entries:
            _captured_copy(capture_store, collection, entry, output / "captured" / entry.path, check_canceled)
        model = next(entry for entry in collection.entries if entry.role == "model-weights")
        _copy(output / "captured" / model.path, output / "source/model.onnx", check_canceled)
        (output / "captured/collection.json").write_bytes(canonical_bytes(collection.model_dump(mode="json")))
        def controlled_run(spec):
            result = run(spec)
            check_canceled()
            if spec.operation == "dependency_install":
                # Exact upstream full texts supplement older wheels which omit
                # them. These additions are hash-bound package artifacts too.
                _supplement_dependency_notices(output / "python/site-packages", assets, check_canceled)
            return result
        runtime = _runtime(output, work, current, controlled_run, check_canceled, work, acquire_wheels)
        (output / "bin").mkdir(mode=0o700)
        (output / "bin/service").write_text('#!/bin/sh\nset -eu\nBIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)\nexec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" 2>/dev/null\n')
        (output / "bin/service").chmod(0o700)
        (output / "bootstrap.py").write_text('import logging,os,runpy,sys\nfrom pathlib import Path\n'
            'root=Path(__file__).resolve().parent\nlogging.disable(logging.CRITICAL)\nsys.dont_write_bytecode=True\n'
            'sys.path[:0]=[str(root/"python/site-packages"),str(root/"source")]\nos.chdir(root/"source")\n'
            'runpy.run_path(str(root/"source/adapter.py"),run_name="__main__")\n')
        code = ('import sys;sys.path[:0]=' + repr([str(output / "python/site-packages"), str(output / "source")])
                + ';import json;from verify_task import verify;print(json.dumps(verify(),sort_keys=True))')
        env = {"PATH": "/usr/bin:/bin", "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"),
               "UV_CACHE_DIR": str(work / "cache"), "UV_PYTHON": str(output / "python/bin/python3.12"),
               "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1", "UV_NO_ENV_FILE": "1",
               "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "LC_ALL": "C", "LANG": "C"}
        result = run(CommandSpec((str(output / "python/bin/python3.12"), "-I", "-S", "-B", "-c", code), work, env,
                                 60, "onnx_model_validation", memory_mb=value.resources.memory_mb))
        check_canceled()
        verification = json.loads(result)
        expected = {"schema_version": 1, "model_valid": True, "blank_images": 3, "controlled_tensor_cases": 6,
                    "max_image_cases": 7, "max_input_bytes": MAX_IMAGE_BYTES, "max_pixels": MAX_IMAGE_PIXELS,
                    "boundary_rejections": 3}
        if (not isinstance(verification, dict) or set(verification) != set(expected) | {"peak_rss_bytes", "max_inference_seconds"}
                or any(type(verification.get(key)) is not type(item) or verification[key] != item for key, item in expected.items())
                or type(verification["peak_rss_bytes"]) is not int
                or not 0 < verification["peak_rss_bytes"] <= value.resources.memory_mb * 1024**2
                or type(verification["max_inference_seconds"]) not in {int, float}
                or not math.isfinite(verification["max_inference_seconds"])
                or not 0 < verification["max_inference_seconds"] < 60):
            raise PreparationError("onnx_model_validation_failed")
        (output / "onnx-validation.json").write_bytes(canonical_bytes(verification))
        licenses = ["licenses/" + name for name in ASSETS if name.startswith("LICENSE")]
        licenses += ["licenses/python-LICENSE.txt", "licenses/DEPENDENCIES.md"]
        licenses += ["captured/" + entry.path for entry in collection.entries if entry.role == "license"]
        manifest = PackageManifest.model_validate({"schema_version": 1, "host_api_version": 1,
            "package_id": value.package.id, "version": value.package.version, "source_revision": draft["source"]["revision"],
            "name": value.package.name, "profile": "mcp-stdio",
            "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
            "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
            "lifecycle": value.lifecycle.model_dump(), "tools": [tool.model_dump(exclude_none=True) for tool in value.tools],
            "access": value.access.model_dump(), "configuration": {}, "resources": value.resources.model_dump(),
            "validation": {"fixtures": [{"tool": item.tool, "arguments": item.arguments, "expected": item.expected}
                                         for item in value.validation[:4]]}, "licenses": licenses})
        (output / "mcpme.package.json").write_bytes(canonical_bytes(manifest.model_dump(exclude_none=True)))
        artifacts = []
        captured_licenses = {"captured/" + entry.path: "captured/files/" + entry.licenses[0].name
                             if entry.licenses else "captured/" + entry.path for entry in collection.entries}
        for path in sorted(output.rglob("*")):
            check_canceled()
            if path.is_dir():
                path.chmod(0o700)
                continue
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise PreparationError("unsafe_generated_artifact")
            path.chmod(0o700 if details.st_mode & 0o111 else 0o600)
            relative = path.relative_to(output).as_posix()
            if relative == "mcpme.package.json":
                continue
            license_path = ("licenses/DEPENDENCIES.md" if relative.startswith("python/site-packages/")
                else "licenses/python-LICENSE.txt" if relative.startswith("python/")
                else captured_licenses[relative] if relative in captured_licenses
                else "licenses/LICENSE-MODEL" if relative in {"source/model.onnx", "source/detector.py"}
                else "licenses/LICENSE")
            artifacts.append({"path": relative, "sha256": _digest(path, check_canceled), "size": details.st_size,
                "executable": bool(details.st_mode & 0o111), "source": draft["source"]["revision"],
                "provenance": "Captured content identity (publisher unverified); pinned host ONNX adapter; frozen copied wheels.",
                "license": license_path})
        lock = {"schema_version": 1, "package_id": manifest.package_id, "version": manifest.version,
            "manifest_sha256": _digest(output / "mcpme.package.json", check_canceled), "artifacts": artifacts,
            "build": {"recipe": COMPONENT_ID + " " + COMPONENT_VERSION, "builder": "mcpme controlled ONNX preparation v1",
                      "toolchain": runtime + "; private-toolchain:sha256:" + current.identity}}
        # A runtime closure has thousands of entries and uses the package lock's
        # own limits, rather than the much smaller descriptor metadata ceiling.
        (output / "mcpme.lock.json").write_text(json.dumps(lock, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        check_canceled()
        return validate_package(output)
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise PreparationError("onnx_preparation_failed") from None
