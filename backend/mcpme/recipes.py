"""Static, immutable recipe bundles sharing import admission transactions.

Enablement in this milestone establishes declarative metadata compatibility.
It never enables code execution or treats bundle-supplied assertions as evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import ValidationError as ModelValidationError

from .contracts import PackageError, package_relative_path
from .imports import ImportStore
from .private import ensure_private_directory
from .preparation_contracts import (PreparationError, RecipeManifest, RecipeLock, RUNNER_IDENTITY,
                                    VERIFIER_IDENTITY, contract_digest, load_contract)

MANIFEST_NAME = "mcpme.recipe.json"
LOCK_NAME = "mcpme.recipe.lock.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_FILES = 2048
MAX_ENTRIES = 4096
MAX_RECIPES = 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# Recognized shipped v1 catalog, used only to verify old evidence unchanged.
# This is a migration identity, never a runtime dispatch or recipe-name rule.
LEGACY_COMPONENT_KEYS = ("python-function-v1@1.0.0", "existing-mcp-v1@1.0.0")


def builtin_components() -> dict:
    root = Path(__file__).resolve().parents[2]
    paths = {"python-function-v1": root / "templates/python-function-v1/adapter.py",
             "existing-mcp-v1": root / "templates/prepare.py"}
    from .preparation_resolved_python import component_catalog
    from .preparation_agent_adapter import component_catalog as agent_adapter_components
    from .preparation_agent_unlocked import component_catalog as agent_unlocked_components
    from .preparation_onnx import builtin_onnx_components
    from .preparation_textgen import builtin_textgen_components
    from .preparation_command import component_catalog as command_components
    from .preparation_service import component_catalog as service_components
    return {**component_catalog(), **agent_adapter_components(), **agent_unlocked_components(), **builtin_onnx_components(), **builtin_textgen_components(), **command_components(), **service_components(), **{name + "@1.0.0": {"digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "input_schema": {"type": "object", "additionalProperties": False},
                              "dependencies": []} for name, path in paths.items()}}


class RecipeError(PreparationError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_digest(domain: str, value: Any) -> str:
    return contract_digest(domain, value)


def _text(value: Any, *, maximum: int = 4096) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise RecipeError("invalid_recipe_schema")


def _path(value: Any) -> str:
    if not _text(value):
        raise RecipeError("unsafe_recipe_path")
    try:
        package_relative_path(value)
    except PackageError:
        raise RecipeError("unsafe_recipe_path") from None
    return value


def _json(data: bytes) -> dict:
    return load_contract(data)


def _manifest(value: dict) -> dict:
    try:
        manifest = RecipeManifest.model_validate(value).model_dump(exclude_none=True)
    except ModelValidationError as failure:
        for item in failure.errors():
            cause = item.get("ctx", {}).get("error")
            if isinstance(cause, PreparationError):
                raise RecipeError(cause.code) from None
        raise RecipeError("invalid_recipe_schema") from None
    if any("<" in field["label"] or ">" in field["label"] for field in manifest["form"]):
        raise RecipeError("invalid_recipe_form")
    return manifest


def _read(path: Path, limit: int) -> bytes:
    """Open the entire ancestry without following links, then check identity."""
    parent_fd = None
    try:
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o6000:
                raise RecipeError("unsafe_recipe_file")
            if before.st_size > limit:
                raise RecipeError("recipe_size_limit")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if len(data) > limit or any(getattr(before, key) != getattr(after, key)
                                      or getattr(after, key) != getattr(current, key) for key in attrs):
                raise RecipeError("recipe_changed")
            visible = path.parent.stat()
            opened = os.fstat(parent_fd)
            if path.resolve() != path or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise RecipeError("recipe_changed")
            return data
    except OSError:
        raise RecipeError("unsafe_recipe_file") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def validate_bundle(root: Path) -> dict:
    """Read the exact closed tree into bounded memory without executing anything."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise RecipeError("unsafe_recipe_path")
    files = {}
    pending = [root]
    scanned = 0
    total = 0
    try:
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                scanned += 1
                if scanned > MAX_ENTRIES:
                    raise RecipeError("recipe_size_limit")
                relative = _path(path.relative_to(root).as_posix())
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    data = _read(path, MAX_METADATA_BYTES if relative in {MANIFEST_NAME, LOCK_NAME} else MAX_FILE_BYTES)
                    total += len(data)
                    if total > MAX_BUNDLE_BYTES or len(files) >= MAX_FILES:
                        raise RecipeError("recipe_size_limit")
                    files[relative] = data
                else:
                    raise RecipeError("unsafe_recipe_file")
    except OSError:
        raise RecipeError("unsafe_recipe_file") from None
    if MANIFEST_NAME not in files or LOCK_NAME not in files:
        raise RecipeError("recipe_metadata_required")
    manifest = _manifest(_json(files[MANIFEST_NAME]))
    try:
        lock = RecipeLock.model_validate(_json(files[LOCK_NAME])).model_dump(exclude_none=True)
    except ModelValidationError:
        raise RecipeError("invalid_recipe_lock") from None
    _keys(lock, {"schema_version", "manifest_sha256", "artifacts"})
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise RecipeError("unsupported_recipe_schema")
    if lock["manifest_sha256"] != hashlib.sha256(files[MANIFEST_NAME]).hexdigest():
        raise RecipeError("recipe_manifest_hash_mismatch")
    if not isinstance(lock["artifacts"], list) or not 1 <= len(lock["artifacts"]) <= MAX_FILES:
        raise RecipeError("invalid_recipe_lock")
    records = {}
    for record in lock["artifacts"]:
        _keys(record, {"path", "sha256", "size", "license", "origin"})
        relative = _path(record["path"])
        if relative in records or relative in {MANIFEST_NAME, LOCK_NAME}:
            raise RecipeError("duplicate_recipe_artifact")
        if (not isinstance(record["sha256"], str) or not _DIGEST.fullmatch(record["sha256"])
                or type(record["size"]) is not int or not 0 <= record["size"] <= MAX_FILE_BYTES
                or not _text(record["origin"])):
            raise RecipeError("invalid_recipe_lock")
        _path(record["license"])
        records[relative] = record
    if records.keys() != files.keys() - {MANIFEST_NAME, LOCK_NAME}:
        raise RecipeError("recipe_artifact_tree_mismatch")
    for relative, record in records.items():
        if len(files[relative]) != record["size"] or hashlib.sha256(files[relative]).hexdigest() != record["sha256"]:
            raise RecipeError("recipe_artifact_hash_mismatch")
        if record["license"] not in manifest["licenses"]:
            raise RecipeError("recipe_license_required")
    if any(license_file not in files or not files[license_file] for license_file in manifest["licenses"]):
        raise RecipeError("recipe_license_required")
    # Re-read through race-safe ancestry to catch edits during tree discovery.
    if any(_read(root / relative, len(data)) != data for relative, data in files.items()):
        raise RecipeError("recipe_changed")
    tree = [{"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for relative, data in sorted(files.items())]
    return {"manifest": manifest, "lock": lock, "digest": canonical_digest("recipe-bundle-v1", tree), "files": files}


class RecipeStore:
    def __init__(self, imports: ImportStore, *, component_catalog: dict | None = None,
                 runner_identity: str | None = None, verifier_identity: str | None = None,
                 policy_identity: str | None = None):
        self.imports = imports
        self.root = ensure_private_directory(imports.root / "recipes")
        self.component_catalog = json.loads(json.dumps(builtin_components() if component_catalog is None else component_catalog))
        self.identities = {"runner": runner_identity or RUNNER_IDENTITY, "verifier": verifier_identity or VERIFIER_IDENTITY,
                           "policy": policy_identity or canonical_digest("recipe-policy-v1", {"kind": "declarative", "runnable": False})}
        if any(not _DIGEST.fullmatch(value) for value in self.identities.values()):
            raise RecipeError("invalid_recipe_evidence_identity")
        with imports._transaction():
            imports._connection.execute("CREATE TABLE IF NOT EXISTS recipes (recipe_id TEXT NOT NULL, version TEXT NOT NULL, "
                                        "data TEXT NOT NULL, PRIMARY KEY(recipe_id,version))")

    def _get(self, recipe_id: str, version: str) -> dict:
        if not isinstance(recipe_id, str) or not isinstance(version, str):
            raise RecipeError("invalid_recipe_identity")
        row = self.imports._connection.execute("SELECT data FROM recipes WHERE recipe_id=? AND version=?",
                                               (recipe_id, version)).fetchone()
        if row is None:
            raise RecipeError("recipe_not_found")
        return json.loads(row["data"])

    def _save(self, record: dict) -> None:
        self.imports._connection.execute("INSERT OR REPLACE INTO recipes VALUES (?,?,?)",
            (record["recipe_id"], record["version"], json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)))

    def _compatibility(self, manifest: dict) -> list[str]:
        blockers = []
        if manifest["kind"] != "declarative":
            blockers.append("unsupported_recipe_kind")
        requirements = manifest["requirements"]
        if (requirements["os"] != "macos" or requirements["architecture"] != "arm64"
                or requirements["package_schema"] not in {1, 2} or requirements["protocol"] != "2025-11-25"):
            blockers.append("recipe_incompatible")
        if set(requirements.get("capabilities", [])) - {"declarative-composition", *(key.split("@", 1)[0] for key in self.component_catalog)}:
            blockers.append("recipe_capability_unavailable")
        visited = set()
        visiting = set()
        def visit(reference):
            key = reference["component"] + "@" + reference["version"]
            component = self.component_catalog.get(key)
            if component is None:
                raise RecipeError("recipe_component_unavailable")
            if reference["digest"] != component.get("digest"):
                raise RecipeError("recipe_component_digest_mismatch")
            # New package contracts require an explicit trusted component
            # declaration. Existing catalogs retain schema-1 semantics.
            if requirements["package_schema"] not in component.get("package_schemas", [1]):
                raise RecipeError("recipe_incompatible")
            if key in visiting:
                raise RecipeError("recipe_dependency_cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in component.get("dependencies", []):
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
        try:
            for step in manifest["steps"]:
                visit(step)
                component = self.component_catalog[step["component"] + "@" + step["version"]]
                schema = component.get("input_schema", {"type": "object", "additionalProperties": False})
                Draft202012Validator(schema).validate(step["config"])
        except RecipeError as error:
            blockers.append(error.code)
        except (ValidationError, SchemaError, KeyError, RecursionError):
            blockers.append("invalid_recipe_component_config")
        return blockers

    def add(self, path: Path) -> dict:
        bundle = validate_bundle(path)
        manifest = bundle["manifest"]
        digest = bundle["digest"]
        # All copying/validation precedes the shared admission transaction.
        staging = self.root / ("attempt-" + secrets.token_hex(12))
        destination = self.root / digest
        try:
            ensure_private_directory(staging)
            for relative, data in bundle["files"].items():
                target = staging / relative
                ensure_private_directory(target.parent)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            if validate_bundle(staging)["digest"] != digest:
                raise RecipeError("recipe_changed")
            if destination.exists() and validate_bundle(destination)["digest"] != digest:
                raise RecipeError("recipe_changed")
            now = time.time()
            record = {"schema_version": 1, "recipe_id": manifest["id"], "version": manifest["version"],
                      "digest": digest, "path": str(destination), "manifest": manifest, "state": "proposed",
                      "epoch": 1, "revoked": False, "runnable": False, "evidence": None, "created_at": now, "updated_at": now,
                      "blockers": self._compatibility(manifest)}
            with self.imports._transaction():
                existing = self.imports._connection.execute("SELECT data FROM recipes WHERE recipe_id=? AND version=?",
                    (manifest["id"], manifest["version"])).fetchone()
                if existing is not None:
                    known = json.loads(existing["data"])
                    if known["digest"] != digest:
                        raise RecipeError("recipe_version_collision")
                    return known
                if self.imports._connection.execute("SELECT count(*) FROM recipes").fetchone()[0] >= MAX_RECIPES:
                    raise RecipeError("recipe_limit")
                if not destination.exists():
                    os.rename(staging, destination)
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                self._save(record)
            return record
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def list(self) -> list[dict]:
        with self.imports._lock:
            return [json.loads(row["data"]) for row in self.imports._connection.execute(
                "SELECT data FROM recipes ORDER BY recipe_id,version")]

    def list_summaries(self) -> list[dict]:
        """Bound discovery responses without repeating forms and schemas."""
        with self.imports._lock:
            result = []
            for row in self.imports._connection.execute("SELECT data FROM recipes ORDER BY recipe_id,version"):
                record = json.loads(row["data"])
                result.append({key: record[key] for key in ("recipe_id", "version", "digest", "state", "revoked", "runnable")})
            return result

    def inspect(self, recipe_id: str, version: str) -> dict:
        with self.imports._lock:
            return self._get(recipe_id, version)

    def enable(self, recipe_id: str, version: str) -> dict:
        before = self.inspect(recipe_id, version)
        bundle = validate_bundle(Path(before["path"]))
        if bundle["digest"] != before["digest"]:
            raise RecipeError("recipe_changed")
        blockers = self._compatibility(bundle["manifest"])
        if blockers:
            raise RecipeError(blockers[0])
        with self.imports._transaction():
            record = self._get(recipe_id, version)
            if record["state"] == "removed" or record.get("revoked"):
                raise RecipeError("recipe_unavailable")
            record.update(state="enabled", epoch=record["epoch"] + 1, updated_at=time.time(), blockers=[],
                          evidence=self.static_evidence(record))
            self._save(record)
            return record

    def _change(self, recipe_id: str, version: str, state: str) -> dict:
        with self.imports._transaction():
            record = self._get(recipe_id, version)
            if record["state"] == state:
                return record
            if record.get("revoked") and state != "removed":
                raise RecipeError("recipe_revoked")
            if record["state"] == "removed" and state != "revoked":
                raise RecipeError("recipe_unavailable")
            record.update(state=state, epoch=record["epoch"] + 1, updated_at=time.time(),
                          revoked=record.get("revoked", False) or state == "revoked")
            self._save(record)
            return record

    def disable(self, recipe_id: str, version: str) -> dict:
        return self._change(recipe_id, version, "disabled")

    def revoke(self, recipe_id: str, version: str) -> dict:
        return self._change(recipe_id, version, "revoked")

    def remove(self, recipe_id: str, version: str) -> dict:
        # Retain bytes and evidence; generated packages/jobs may still reference them.
        return self._change(recipe_id, version, "removed")

    def deprecate(self, recipe_id: str, version: str) -> dict:
        return self._change(recipe_id, version, "deprecated")

    def _component_closure(self, manifest):
        pending, closure = list(manifest["steps"]), {}
        while pending:
            reference = pending.pop()
            key = reference["component"] + "@" + reference["version"]
            component = self.component_catalog.get(key)
            if component is None or component.get("digest") != reference["digest"]:
                raise RecipeError("recipe_evidence_stale")
            if key in closure:
                continue
            closure[key] = component
            if len(closure) > 128:
                raise RecipeError("recipe_evidence_stale")
            pending.extend(component.get("dependencies", []))
        return closure

    def static_evidence(self, record):
        """Exact selected/transitive implementations, independent of additions."""
        return {"schema_version": 2, "recipe_digest": record["digest"],
            "component_closure_digest": canonical_digest("recipe-component-closure-v1", self._component_closure(record["manifest"])),
            "identities": self.identities, "gate": "static-declarative-compatibility-v2",
            "outcome": "passed", "runnable": False}

    def _evidence_matches(self, record):
        evidence = record.get("evidence")
        if not isinstance(evidence, dict):
            return False
        if evidence.get("schema_version") == 2:
            return evidence == self.static_evidence(record)
        closure = self._component_closure(record["manifest"])
        accepted = {canonical_digest("recipe-components-v1", self.component_catalog)}
        # Old arbitrary catalogs cannot be guessed from a digest. Only the
        # known shipped v1 catalog can be reconstructed and checked exactly.
        if set(closure) <= set(LEGACY_COMPONENT_KEYS) and all(key in self.component_catalog for key in LEGACY_COMPONENT_KEYS):
            legacy = {key: self.component_catalog[key] for key in LEGACY_COMPONENT_KEYS}
            accepted.add(canonical_digest("recipe-components-v1", legacy))
        return any(evidence == {"schema_version": 1, "recipe_digest": record["digest"],
            "component_catalog_digest": digest, "identities": self.identities,
            "gate": "static-declarative-compatibility-v1", "outcome": "passed", "runnable": False}
            for digest in accepted)

    def assert_admissible(self, recipe_id: str, version: str, digest: str, *, existing_job: bool = False) -> dict:
        """Metadata-only check suitable inside the caller's admission transaction."""
        with self.imports._lock:
            record = self._get(recipe_id, version)
            if record["digest"] != digest:
                raise RecipeError("recipe_digest_mismatch")
            if record.get("revoked"):
                raise RecipeError("recipe_revoked")
            allowed = {"enabled", "disabled", "deprecated", "removed"} if existing_job else {"enabled"}
            if record["state"] not in allowed:
                raise RecipeError("recipe_disabled")
            if not self._evidence_matches(record):
                raise RecipeError("recipe_evidence_stale")
            return record

    def resolve(self, recipe_id: str, version: str, expected_digest: str, *, existing_job: bool = False) -> dict:
        record = self.inspect(recipe_id, version)
        if validate_bundle(Path(record["path"]))["digest"] != record["digest"]:
            raise RecipeError("recipe_changed")
        return self.assert_admissible(recipe_id, version, expected_digest, existing_job=existing_job)
