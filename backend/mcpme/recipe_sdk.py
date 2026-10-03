"""Offline authoring helpers for metadata-only declarative recipe bundles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from pydantic import ValidationError

from .preparation_contracts import MAX_CONTRACT_BYTES, PreparationError, RecipeManifest, RecipeLock, load_contract
from .private import PrivateStateError, check_private_file, ensure_private_directory
from .recipes import (LOCK_NAME, MANIFEST_NAME, MAX_BUNDLE_BYTES, MAX_ENTRIES, MAX_FILE_BYTES, MAX_FILES,
                      _path, _read, builtin_components, validate_bundle)

COMPONENTS = ("python-function-v1", "existing-mcp-v1", "resolved-python-v1")


def read_contract_file(path: Path) -> dict:
    """Bound reads and reject link/identity races before parsing declarative JSON."""
    return load_contract(_read(Path(path).absolute(), MAX_CONTRACT_BYTES))


def check_recipe(path: Path) -> dict:
    bundle = validate_bundle(path)
    manifest = bundle["manifest"]
    return {"valid": True, "recipe_id": manifest["id"], "version": manifest["version"],
            "digest": bundle["digest"], "kind": manifest["kind"], "runnable": False,
            "verification": "static-bundle-contract", "artifact_count": len(bundle["lock"]["artifacts"]),
            "input_schema": manifest["input_schema"], "form": manifest["form"],
            "requirements": manifest["requirements"]}


def _manifest(value: dict) -> dict:
    try:
        return RecipeManifest.model_validate(value).model_dump(exclude_none=True)
    except ValidationError as failure:
        for item in failure.errors():
            cause = item.get("ctx", {}).get("error")
            if isinstance(cause, PreparationError):
                raise PreparationError(cause.code) from None
        raise PreparationError("invalid_recipe_schema") from None


def _authoring_files(root: Path) -> dict[str, bytes]:
    """Snapshot a bounded private tree before changing only its generated lock."""
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise PreparationError("unsafe_recipe_path")
    files: dict[str, bytes] = {}
    pending = [root]
    total = scanned = 0
    try:
        while pending:
            directory = pending.pop()
            ensure_private_directory(directory)
            for path in sorted(directory.iterdir()):
                scanned += 1
                if scanned > MAX_ENTRIES:
                    raise PreparationError("recipe_size_limit")
                relative = _path(path.relative_to(root).as_posix())
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    check_private_file(path)
                    data = _read(path, MAX_CONTRACT_BYTES if relative in {MANIFEST_NAME, LOCK_NAME} else MAX_FILE_BYTES)
                    total += len(data)
                    if total > MAX_BUNDLE_BYTES or len(files) >= MAX_FILES:
                        raise PreparationError("recipe_size_limit")
                    files[relative] = data
                else:
                    raise PreparationError("unsafe_recipe_file")
    except (OSError, PrivateStateError):
        raise PreparationError("unsafe_recipe_file") from None
    if MANIFEST_NAME not in files or LOCK_NAME not in files:
        raise PreparationError("recipe_metadata_required")
    return files


def _write_new_file(path: Path, data: bytes) -> None:
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _open_directory(path: Path) -> int:
    """Anchor the replacement to an ancestry opened without following links."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        details = os.fstat(descriptor)
        visible = path.lstat()
        if (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700
                or (details.st_dev, details.st_ino) != (visible.st_dev, visible.st_ino)):
            raise PreparationError("unsafe_recipe_path")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def relock_recipe(path: Path) -> dict:
    """Rehash declared artifacts offline, preserving their license and provenance."""
    root = Path(path).absolute()
    files = _authoring_files(root)
    manifest = _manifest(load_contract(files[MANIFEST_NAME]))
    try:
        previous = RecipeLock.model_validate(load_contract(files[LOCK_NAME])).model_dump(exclude_none=True)
    except ValidationError:
        raise PreparationError("invalid_recipe_lock") from None
    records = {}
    for record in previous["artifacts"]:
        relative = record["path"]
        if relative in records or relative in {MANIFEST_NAME, LOCK_NAME}:
            raise PreparationError("duplicate_recipe_artifact")
        if record["license"] not in manifest["licenses"]:
            raise PreparationError("recipe_license_required")
        records[relative] = record
    if any(license_path not in files or not files[license_path] for license_path in manifest["licenses"]):
        raise PreparationError("recipe_license_required")
    if records.keys() != files.keys() - {MANIFEST_NAME, LOCK_NAME}:
        raise PreparationError("recipe_artifact_metadata_required")
    updated = {"schema_version": 1, "manifest_sha256": hashlib.sha256(files[MANIFEST_NAME]).hexdigest(),
               "artifacts": [{**record, "sha256": hashlib.sha256(files[relative]).hexdigest(),
                              "size": len(files[relative])} for relative, record in sorted(records.items())]}
    lock_bytes = json.dumps(updated, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    load_contract(lock_bytes)
    # Validate the exact future tree outside the bundle, then replace only its lock.
    with tempfile.TemporaryDirectory(prefix=".mcpme-recipe-lock-", dir=root.parent) as temporary:
        staging = Path(temporary)
        candidate = staging / "candidate"
        candidate.mkdir(mode=0o700)
        for relative, data in files.items():
            _write_new_file(candidate / relative, lock_bytes if relative == LOCK_NAME else data)
        checked = check_recipe(candidate)
        rollback = staging / "previous-lock.json"
        _write_new_file(rollback, files[LOCK_NAME])
        if _authoring_files(root) != files:
            raise PreparationError("recipe_changed")
        descriptor = _open_directory(root)
        replaced = False
        try:
            os.replace(candidate / LOCK_NAME, LOCK_NAME, dst_dir_fd=descriptor)
            replaced = True
            os.fsync(descriptor)
            current = check_recipe(root)
            if current["digest"] != checked["digest"]:
                raise PreparationError("recipe_changed")
            return {"locked": True, "path": str(root), **current}
        except BaseException:
            if replaced and _read(root / LOCK_NAME, MAX_CONTRACT_BYTES) == lock_bytes:
                os.replace(rollback, LOCK_NAME, dst_dir_fd=descriptor)
                os.fsync(descriptor)
            raise
        finally:
            os.close(descriptor)


def scaffold_recipe(output: Path, recipe_id: str, version: str = "1.0.0",
                    component: str = "python-function-v1") -> dict:
    if component not in COMPONENTS:
        raise PreparationError("recipe_component_unavailable")
    catalog = builtin_components()
    component_version = "1.0.0"
    if component == "resolved-python-v1":
        from .preparation_resolved_python import ResolvedPythonComponent
        component_version = ResolvedPythonComponent().reference.version
    entry = catalog.get(component + "@" + component_version)
    if entry is None:
        raise PreparationError("recipe_component_unavailable")
    component_identity = entry["digest"]
    manifest = _manifest({
        "schema_version": 1, "id": recipe_id, "version": version, "kind": "declarative",
        "worker_api_version": 1,
        "input_schema": {"type": "object", "additionalProperties": False,
            "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 128},
                           "precision": {"type": "integer", "minimum": 0, "maximum": 6}},
            "required": ["label"]},
        "form": [{"field": "label", "label": "Tool label", "kind": "string", "default": "Synthetic tool"},
                 {"field": "precision", "label": "Decimal places", "kind": "integer", "default": 2}],
        "steps": [{"component": component, "version": component_version, "digest": component_identity, "config": {}}],
        "requirements": {"capabilities": ["declarative-composition", component]},
        "licenses": ["LICENSE"], "provenance": {"origin": "mcpme-recipe-sdk-v1", "publisher": "mcpme contributors"},
    })
    destination = Path(output).absolute()
    if destination.parent.resolve() != destination.parent:
        raise PreparationError("unsafe_recipe_path")
    if destination.exists() or destination.is_symlink():
        raise PreparationError("recipe_destination_exists")
    license_bytes = (Path(__file__).resolve().parents[2] / "templates/LICENSE").read_bytes()
    readme = (
        "# " + recipe_id + "\n\n"
        "This declarative recipe composes the pinned " + component + " component. "
        "Its bounded label and precision fields are rendered in generated native forms.\n\n"
        "The fixture contains synthetic metadata only. Static checking and enabling metadata do not run this recipe. "
        "Execution remains unavailable until the independent preparation runner and runtime verification gates pass.\n\n"
        "Edit the input schema, form and nonsecret fixture together; regenerate the closed-tree lock after edits with "
        "`mcpme recipes lock --path /absolute/path/to/recipe --json`. "
        "The lock command preserves each artifact's explicit license and origin; new files need those records declared first. "
        "Publish changed bundle bytes under a new immutable semantic version.\n\n"
        "Run `mcpme recipes check --path /absolute/path/to/recipe --json` before adding it. "
        "Add and enable through local owner authority. Existing metadata-only preparation grants cannot manage recipes.\n"
    ).encode("utf-8")
    notices = ("# Recipe provenance and licenses\n\n"
               "The scaffold and documentation are provided by mcpme contributors under the full MIT license in LICENSE.\n\n"
               "The component implementation stays in the trusted host catalog and is identified by its exact SHA-256. "
               "This bundle includes no interpreter, dependency wheel, model weight or executable worker. "
               "A future app package must retain the complete runtime and dependency notices required by its resolved closure.\n").encode("utf-8")
    fixtures = json.dumps({"schema_version": 1, "synthetic": True,
                          "inputs": {"label": "Synthetic arithmetic", "precision": 2},
                          "expected": {"input_schema_valid": True, "form_fields": ["label", "precision"],
                                       "component": component, "runnable": False}}, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    files = {"LICENSE": license_bytes, "README.md": readme, "THIRD_PARTY_NOTICES.md": notices,
             "fixtures/synthetic.json": fixtures,
             MANIFEST_NAME: json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"}
    lock = {"schema_version": 1, "manifest_sha256": hashlib.sha256(files[MANIFEST_NAME]).hexdigest(),
            "artifacts": [{"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                           "license": "LICENSE", "origin": "mcpme-recipe-sdk-v1"}
                          for relative, data in sorted(files.items()) if relative != MANIFEST_NAME]}
    files[LOCK_NAME] = json.dumps(lock, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    # Failed generation reclaims only the new destination, never an existing tree.
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    try:
        for relative, data in files.items():
            path = destination / relative
            ensure_private_directory(path.parent)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return {"scaffolded": True, "path": str(destination), **check_recipe(destination)}
    except BaseException:
        shutil.rmtree(destination)
        raise
