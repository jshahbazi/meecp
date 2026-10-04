"""Immutable untrusted adapter inputs, separately bound to one agent attempt.

Only trusted host code calls freeze. No source, response or credential bytes
enter the ownership journal. Proposed code is retained in a distinct private
collection and never imports, executes or registers itself during derivation.
"""
from __future__ import annotations

import ast
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from typing import Literal
import uuid

from pydantic import Field, model_validator

from .contracts import Digest, Tool
from .preparation_agent_contracts import AgentProposalV1
from .preparation_components import ComponentReference, read_snapshot
from .preparation_contracts import (PreparationError, RecipeReference, SourceIdentity, UUID,
    canonical_bytes, check_schema, contract_digest, load_contract, relative_path, schema_accepts)
from .preparation_inputs import CollectionReference, DerivedInputReference, InputContract
from .preparation_selection_snapshot import selection_snapshot_metadata
from .private import ensure_private_directory

MODULE = "mcpme_generated_agent_adapter"
CODE_PATH = MODULE + ".py"
FILES = ("LICENSE", "adapter.json", CODE_PATH)
MAX_DERIVATIONS = 1024


class AdapterSpecificationV1(InputContract):
    schema_version: Literal[1]
    module: Literal["mcpme_generated_agent_adapter"]
    function: Literal["invoke"]
    source_license: str = Field(min_length=1, max_length=256)
    dependency_project: str = Field(min_length=1, max_length=256)
    dependency_lock: str = Field(min_length=1, max_length=256)
    dependency_lock_sha256: Digest
    tool: Tool
    arguments: dict
    expected: dict

    @property
    def digest(self):
        return contract_digest("agent-python-adapter-specification-v1", self.model_dump(mode="json", exclude_none=True))


class AdapterRequestV1(InputContract):
    schema_version: Literal[1]
    import_id: UUID
    revision: int = Field(ge=1)
    source_revision: SourceIdentity
    source_identity_digest: Digest
    job_id: UUID
    plan_digest: Digest
    request_digest: Digest
    proposal_digest: Digest
    attempt_id: UUID
    generation: UUID
    recipe: RecipeReference
    recipe_epoch: int = Field(ge=1)
    producer: ComponentReference
    policy_digest: Digest
    expires_at: int = Field(ge=1)

    @property
    def digest(self):
        return contract_digest("agent-python-adapter-request-v1", self.model_dump(mode="json"))


class AdapterResultV1(InputContract):
    schema_version: Literal[1]
    request: AdapterRequestV1
    specification: AdapterSpecificationV1
    collection: CollectionReference
    derived_inputs: tuple[DerivedInputReference, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def closure(self):
        names = {"adapter-license": "LICENSE", "adapter-interface": "adapter.json", "adapter-source": CODE_PATH}
        if {item.name for item in self.derived_inputs} != set(names) or self.collection.kind != "derived_input_collection":
            raise PreparationError("agent_adapter_input_changed")
        records = []
        for item in self.derived_inputs:
            if (item.role != "adapter" or item.collection_digest != self.collection.digest
                    or item.producer != self.request.producer or item.source_revision != self.request.source_revision
                    or item.request_digest != self.request.digest or item.policy_digest != self.request.policy_digest
                    or item.parent_digests != (self.request.source_revision.removeprefix("sha256:"),)):
                raise PreparationError("agent_adapter_input_changed")
            records.append({"path": names[item.name], "sha256": item.sha256, "size": item.size})
        if contract_digest("agent-python-adapter-collection-v1", sorted(records, key=lambda item: item["path"])) != self.collection.digest:
            raise PreparationError("agent_adapter_input_changed")
        interface = next(item for item in self.derived_inputs if item.name == "adapter-interface")
        data = canonical_bytes(self.specification.model_dump(mode="json", exclude_none=True))
        if interface.size != len(data) or interface.sha256 != hashlib.sha256(data).hexdigest():
            raise PreparationError("agent_adapter_input_changed")
        return self

    @property
    def digest(self):
        return contract_digest("agent-python-adapter-result-v1", self.model_dump(mode="json"))


def _metadata(draft):
    # The inventory itself already enforces entry/file/depth limits. Persist a
    # digest of its complete identity, avoiding an oversized metadata journal.
    data = json.dumps(asdict(selection_snapshot_metadata(draft)), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(b"agent-adapter-snapshot-metadata-v1\0" + data).hexdigest()


def original_snapshot(draft, check=lambda: None):
    """Bind the same metadata baseline around the full original byte proof."""
    check()
    before = _metadata(draft)
    snapshot = read_snapshot(draft)
    check()
    if _metadata(draft) != before:
        raise PreparationError("source_identity_mismatch")
    check()
    return snapshot, before


def _derived_draft(root, records):
    # Reuse the exact bounded nofollow snapshot proof for the private derived
    # tree. This identity is never installed as the import's original source.
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source": {"kind": "local_folder", "snapshot_path": str(root), "files": records,
        "digest": digest, "revision": "sha256:" + digest}}


def _json_file(task, path):
    if len(task.files) != 1 or task.files[0].path != path:
        raise PreparationError("agent_adapter_proposal_invalid")
    return load_contract(task.files[0].content.encode())


def specification_from_proposal(proposal, snapshot):
    """Closed role composition: none of the proposed files are ignored."""
    value = AgentProposalV1.model_validate(proposal.model_dump(mode="json"))
    if any(item.unresolved for item in value.tasks):
        raise PreparationError("agent_candidate_unresolved")
    interface, dependencies, fixtures, adapter = value.tasks
    interface = _json_file(interface, "proposed/interface/interface.json")
    dependencies = _json_file(dependencies, "proposed/dependencies/dependencies.json")
    fixtures = _json_file(fixtures, "proposed/fixtures/fixtures.json")
    files = {item.path: item.content.encode() for item in adapter.files}
    if set(files) != {"proposed/adapter/adapter.json", "proposed/adapter/wrapper.py", "proposed/adapter/LICENSE"}:
        raise PreparationError("agent_adapter_proposal_invalid")
    metadata = load_contract(files["proposed/adapter/adapter.json"])
    if set(interface) != {"tool"} or set(fixtures) != {"arguments", "expected"} or set(dependencies) != {"project", "lock", "lock_sha256"}:
        raise PreparationError("agent_adapter_proposal_invalid")
    if set(metadata) != {"schema_version", "module", "function", "source_license"}:
        raise PreparationError("agent_adapter_proposal_invalid")
    specification = AdapterSpecificationV1.model_validate({**metadata, "tool": interface["tool"],
        "arguments": fixtures["arguments"], "expected": fixtures["expected"],
        "dependency_project": dependencies["project"], "dependency_lock": dependencies["lock"],
        "dependency_lock_sha256": dependencies["lock_sha256"]})
    original = snapshot.by_path()
    for path in (specification.source_license, specification.dependency_project, specification.dependency_lock):
        relative_path(path)
        if path not in original or not original[path].size:
            raise PreparationError("agent_adapter_original_input_required")
    if (Path(specification.dependency_project).name != "pyproject.toml"
            or Path(specification.dependency_lock).name != "uv.lock"
            or Path(specification.dependency_project).parent != Path(specification.dependency_lock).parent
            or original[specification.dependency_lock].sha256 != specification.dependency_lock_sha256):
        raise PreparationError("locked_dependency_project_required")
    if any(path == CODE_PATH or path.split("/")[0] in {MODULE, MODULE + ".py"} for path in original):
        raise PreparationError("agent_adapter_namespace_collision")
    code, license_text = files["proposed/adapter/wrapper.py"], files["proposed/adapter/LICENSE"]
    if not code or not license_text or len(code) > 16384 or len(license_text) > 16384:
        raise PreparationError("agent_adapter_proposal_invalid")
    # Eligibility only. Function bodies remain untrusted and are executed only
    # by the ordinary independently confined preparation verifier.
    try:
        tree = ast.parse(code)
        from .preparation_inspection import _bound_tree, _callable
        _bound_tree(tree, nodes=2048, depth=64)
        allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef)
        if any(not isinstance(node, allowed) for node in tree.body):
            raise PreparationError("agent_adapter_static_eligibility_required")
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if len(functions) != 1 or functions[0].name != "invoke":
            raise PreparationError("agent_adapter_static_eligibility_required")
        schema, _ = _callable(functions[0])
        if schema != specification.tool.input_schema or specification.tool.output_schema is None:
            raise PreparationError("agent_adapter_interface_mismatch")
    except (SyntaxError, UnicodeError, RecursionError):
        raise PreparationError("agent_adapter_static_eligibility_required") from None
    check_schema(specification.tool.input_schema)
    check_schema(specification.tool.output_schema)
    if (not schema_accepts(specification.tool.input_schema, specification.arguments)
            or not schema_accepts(specification.tool.output_schema, specification.expected)):
        raise PreparationError("agent_adapter_fixture_mismatch")
    return specification, {"adapter.json": canonical_bytes(specification.model_dump(mode="json", exclude_none=True)),
        CODE_PATH: code, "LICENSE": license_text}


class AgentAdapterDerivationStore:
    """Durable local ownership with no process discovery or source mutation."""
    def __init__(self, imports, recipes):
        self.imports, self.recipes, self.db = imports, recipes, imports._connection
        self.root = ensure_private_directory(imports.root / "agent-adapter-inputs")
        with imports._transaction():
            if not hasattr(imports, "_agent_adapter_workers"):
                imports._agent_adapter_workers = {}
            self.workers = imports._agent_adapter_workers
            self.db.execute("CREATE TABLE IF NOT EXISTS agent_adapter_derivations (id TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
            for row in self.db.execute("SELECT data FROM agent_adapter_derivations").fetchall():
                item = json.loads(row[0])
                if item["state"] in {"writing", "deleting"} and item["id"] not in self.workers:
                    # The old owner is gone; no unknown artifact is deleted or
                    # adopted. Explicit recover checks the exact owned inode.
                    item.update(state="interrupted", ownership_settled=False)
                    self._save(item)

    def _save(self, item):
        self.db.execute("UPDATE agent_adapter_derivations SET data=? WHERE id=?", (canonical_bytes(item).decode(), item["id"]))

    def _get(self, identity):
        row = self.db.execute("SELECT data FROM agent_adapter_derivations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise PreparationError("agent_adapter_input_unavailable")
        return json.loads(row[0])

    def get(self, identity):
        with self.imports._lock:
            return self._get(identity)

    def _current(self, request, draft, *, accepted=False):
        current = self.imports._get(request.import_id, draft["revision"])
        self.imports._mutable(current)
        if (current["source"] != draft["source"] or current["source"]["revision"] != request.source_revision
                or contract_digest("agent-host-source-v1", current["source"]) != request.source_identity_digest):
            raise PreparationError("agent_adapter_source_mismatch")
        recipe = self.recipes.assert_admissible(request.recipe.id, request.recipe.version, request.recipe.digest)
        if recipe["epoch"] != request.recipe_epoch:
            raise PreparationError("agent_recipe_changed")
        if (not accepted and time.time() >= request.expires_at) or current["expires_at"] <= time.time():
            raise PreparationError("agent_authority_expired")
        if current["revision"] != request.revision and not accepted:
            raise PreparationError("agent_adapter_revision_mismatch")
        return current

    def freeze(self, job, plan, proposal, producer, draft, check=lambda: None):
        """Called from the host's joined validating worker, never a route body."""
        request = AdapterRequestV1(schema_version=1, import_id=job["import_id"], revision=job["revision"],
            source_revision=job["source_revision"], source_identity_digest=plan["source_identity_digest"],
            job_id=job["job_id"], plan_digest=plan["plan_digest"], request_digest=plan["request_digest"],
            proposal_digest=proposal.digest, attempt_id=proposal.attempt_id, generation=proposal.generation,
            recipe=plan["recipe"], recipe_epoch=plan["recipe_epoch"], producer=producer.model_dump(),
            policy_digest=contract_digest("agent-python-adapter-policy-v1", {"limits": plan["limits"], "namespace": MODULE}),
            expires_at=plan["expires_at"])
        if (proposal.request_digest != request.request_digest or proposal.attempt_id != job["attempt_id"]
                or proposal.digest != job["proposal_digest"]):
            raise PreparationError("agent_adapter_attempt_mismatch")
        check()
        snapshot, baseline = original_snapshot(draft, check)
        specification, content = specification_from_proposal(proposal, snapshot)
        records = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in sorted(content.items())]
        collection = CollectionReference(kind="derived_input_collection", schema_version=1,
            digest=contract_digest("agent-python-adapter-collection-v1", records))
        result = AdapterResultV1(schema_version=1, request=request.model_dump(mode="json"),
            specification=specification.model_dump(mode="json"), collection=collection.model_dump(),
            derived_inputs=[DerivedInputReference(name={"LICENSE": "adapter-license", "adapter.json": "adapter-interface", CODE_PATH: "adapter-source"}[item["path"]],
                role="adapter", collection_digest=collection.digest, sha256=item["sha256"], size=item["size"], producer=producer.model_dump(),
                source_revision=request.source_revision, request_digest=request.digest, policy_digest=request.policy_digest,
                parent_digests=[request.source_revision.removeprefix("sha256:")]).model_dump(mode="json") for item in records])
        with self.imports._transaction():
            self._current(request, draft)
            row = self.db.execute("SELECT id FROM agent_adapter_derivations WHERE job=?", (job["job_id"],)).fetchone()
            old = self._get(row[0]) if row is not None else None
            if old is not None and old["result_digest"] != result.digest:
                raise PreparationError("agent_adapter_attempt_mismatch")
        if old is not None:
            return self.resolve(old["id"], result.digest, draft, full=True)[0]
        with self.imports._transaction():
            self._current(request, draft)
            if self.db.execute("SELECT count(*) FROM agent_adapter_derivations").fetchone()[0] >= MAX_DERIVATIONS:
                raise PreparationError("agent_adapter_input_limit")
            identity = str(uuid.uuid4())
            item = {"schema_version": 1, "id": identity, "job_id": job["job_id"], "state": "writing",
                "ownership_settled": False, "request": request.model_dump(mode="json"), "result_digest": result.digest,
                "result": result.model_dump(mode="json"), "records": records, "source_metadata": baseline,
                "derived_metadata": None, "root_identity": None, "file_identities": {}}
            self.db.execute("INSERT INTO agent_adapter_derivations VALUES (?,?,?)", (identity, job["job_id"], canonical_bytes(item).decode()))
            self.workers[identity] = threading.Event()
        external_check = check
        def check():
            external_check()
            if self.workers[identity].is_set():
                raise PreparationError("agent_cancel_requested")
        descriptor = None
        try:
            check()
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir(identity, mode=0o700, dir_fd=parent)
                descriptor = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                details = os.fstat(descriptor)
                item["root_identity"] = [details.st_dev, details.st_ino, details.st_mode]
                # Durable ownership precedes every proposed byte write.
                with self.imports._transaction():
                    self._save(item)
                os.fsync(parent)
            finally:
                os.close(parent)
            for name, data in content.items():
                check()
                opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600, dir_fd=descriptor)
                with os.fdopen(opened, "wb") as stream:
                    details = os.fstat(stream.fileno())
                    item["file_identities"][name] = [details.st_dev, details.st_ino, details.st_mode]
                    # Exact owned file identity is durable before its bytes.
                    with self.imports._transaction():
                        self._save(item)
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.fsync(descriptor)
            check()
            derived = _derived_draft(self.root / identity, records)
            copied, metadata = original_snapshot(derived, check)
            if {file.path: file.data for file in copied.files} != content:
                raise PreparationError("agent_adapter_input_changed")
            if original_snapshot(draft, check)[1] != baseline:
                raise PreparationError("source_identity_mismatch")
            with self.imports._transaction():
                self._current(request, draft)
                check()
                item.update(state="ready", ownership_settled=True, derived_metadata=metadata)
                self._save(item)
            return result
        except BaseException:
            with self.imports._transaction():
                item.update(state="interrupted", ownership_settled=False)
                self._save(item)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with self.imports._lock:
                self.workers.pop(identity, None)

    def _accepted(self, request, result, draft):
        if draft["revision"] == request.revision:
            return False
        row = self.db.execute("SELECT data FROM preparation_selections WHERE import_id=?", (request.import_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_adapter_revision_mismatch")
        selected = json.loads(row[0])
        if (draft["revision"] < request.revision + 1 or selected["revision"] != draft["revision"]
                or selected["source_revision"] != request.source_revision or selected["candidate_id"] != result.digest
                or selected["descriptor"].get("recipe") != request.recipe.model_dump()
                or selected["descriptor"].get("components") != [{"component": request.producer.model_dump(),
                    "input": {"derivation_id": self._identity_for(result), "derivation_digest": result.digest}}]):
            raise PreparationError("agent_adapter_revision_mismatch")
        return True

    def _identity_for(self, result):
        row = self.db.execute("SELECT id FROM agent_adapter_derivations WHERE job=?", (result.request.job_id,)).fetchone()
        if row is None:
            raise PreparationError("agent_adapter_input_unavailable")
        return row[0]

    def resolve(self, identity, digest, draft, *, full=True, check=lambda: None):
        with self.imports._lock:
            item = self._get(identity)
            if item["state"] != "ready" or not item["ownership_settled"] or item["result_digest"] != digest:
                raise PreparationError("agent_adapter_input_unavailable")
            result = AdapterResultV1.model_validate(item["result"])
            if result.digest != digest:
                raise PreparationError("agent_adapter_input_changed")
            self._current(result.request, draft, accepted=self._accepted(result.request, result, draft))
        derived = _derived_draft(self.root / identity, item["records"])
        if _metadata(draft) != item["source_metadata"] or _metadata(derived) != item["derived_metadata"]:
            raise PreparationError("agent_adapter_input_changed")
        if full:
            if original_snapshot(draft, check)[1] != item["source_metadata"] or original_snapshot(derived, check)[1] != item["derived_metadata"]:
                raise PreparationError("agent_adapter_input_changed")
        check()
        with self.imports._lock:
            self._current(result.request, draft, accepted=self._accepted(result.request, result, draft))
            if self._get(identity) != item:
                raise PreparationError("agent_adapter_input_changed")
        return result, self.root / identity

    def cancel(self, identity):
        with self.imports._transaction():
            item = self._get(identity)
            if identity in self.workers:
                self.workers[identity].set()
            if item["state"] == "ready":
                item["state"] = "canceled"; self._save(item)
            elif item["state"] in {"writing", "interrupted"}:
                item.update(state="interrupted", ownership_settled=False); self._save(item)
            return item

    def recover(self, identity):
        """Retain incomplete bytes; settle only the exact recorded local inode."""
        with self.imports._transaction():
            item = self._get(identity)
            if identity in self.workers:
                raise PreparationError("agent_adapter_ownership_unsettled")
            if item["state"] != "interrupted":
                return item
            path = self.root / identity
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    details = os.fstat(descriptor)
                    if [details.st_dev, details.st_ino, details.st_mode] != item["root_identity"]:
                        raise PreparationError("agent_adapter_ownership_unsettled")
                finally:
                    os.close(descriptor)
            except OSError:
                raise PreparationError("agent_adapter_ownership_unsettled") from None
            item.update(state="canceled", ownership_settled=True); self._save(item)
            return item

    def delete(self, identity):
        """Delete only a settled canceled collection through its owned FDs."""
        with self.imports._transaction():
            item = self._get(identity)
            if item["state"] == "deleted":
                return item
            if item["state"] != "canceled" or not item["ownership_settled"]:
                raise PreparationError("agent_adapter_ownership_unsettled")
            if identity in self.workers:
                raise PreparationError("agent_adapter_ownership_unsettled")
            item.update(state="deleting", ownership_settled=False); self._save(item)
            self.workers[identity] = threading.Event()
        parent = descriptor = None
        try:
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptor = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            details = os.fstat(descriptor)
            if [details.st_dev, details.st_ino, details.st_mode] != item["root_identity"]:
                raise PreparationError("agent_adapter_ownership_unsettled")
            names = os.listdir(descriptor)
            if len(names) > len(FILES) or not set(names) <= set(item["file_identities"]):
                raise PreparationError("agent_adapter_ownership_unsettled")
            for name in names:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600:
                    raise PreparationError("agent_adapter_ownership_unsettled")
                if [details.st_dev, details.st_ino, details.st_mode] != item["file_identities"][name]:
                    raise PreparationError("agent_adapter_ownership_unsettled")
            for name in names:
                os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
            visible = os.stat(identity, dir_fd=parent, follow_symlinks=False)
            if [visible.st_dev, visible.st_ino, visible.st_mode] != item["root_identity"]:
                raise PreparationError("agent_adapter_ownership_unsettled")
            os.rmdir(identity, dir_fd=parent); os.fsync(parent)
            with self.imports._transaction():
                item.update(state="deleted", ownership_settled=True); self._save(item)
            return item
        except BaseException:
            with self.imports._transaction():
                item.update(state="interrupted", ownership_settled=False); self._save(item)
            raise
        finally:
            for held in (descriptor, parent):
                if held is not None:
                    os.close(held)
            with self.imports._lock:
                self.workers.pop(identity, None)
