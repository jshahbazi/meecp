"""Durable immutable inputs for staged proposals and reviewed resolution.

The database contains identity, policy, ownership and byte-proof metadata.
Proposed source, dependency strings and fixture contents remain in separate
private artifacts. Neither staging nor materialization executes selected code.
"""
from __future__ import annotations

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

from .contracts import Digest
from .preparation_agent_adapter_store import (AdapterRequestV1, CODE_PATH, MODULE,
    _derived_draft, _metadata, original_snapshot)
from .preparation_agent_unlocked_contracts import (UnlockedAdapterSpecificationV1,
    canonical_dependency_project, specification_from_proposal)
from .preparation_components import ComponentReference
from .preparation_contracts import PreparationError, UUID, canonical_bytes, contract_digest, load_contract
from .preparation_inputs import CollectionReference, DerivedInputReference, InputContract, RuntimeReference
from .preparation_resolution import ResolutionPolicyV1, ResolutionResultV1
from .private import ensure_private_directory
from .preparation_application_source import _open_directory
from .preparation_selection_snapshot import _identity

FILES = {"adapter-license": "LICENSE", "adapter-interface": "adapter.json", "adapter-source": CODE_PATH,
    "dependency-project": "pyproject.toml", "dependency-lock": "uv.lock"}
MAX_INPUTS = 2048


def resolution_input_metadata(directory, resolution):
    """Metadata-only continuity for the exact frozen resolver collection."""
    expected = {FILES[item.name]:item for item in resolution.derived_inputs}
    if set(expected) != {"pyproject.toml","uv.lock"}:
        raise PreparationError("agent_unlocked_resolution_mismatch")
    root = None
    try:
        ancestors=[]; root=_open_directory(Path(directory),ancestors)
        before=os.fstat(root)
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700 or set(os.listdir(root)) != set(expected):
            raise PreparationError("agent_unlocked_resolution_mismatch")
        entries=[]
        for name,reference in sorted(expected.items()):
            named=os.stat(name,dir_fd=root,follow_symlinks=False)
            opened=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
            try:
                held=os.fstat(opened)
                if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                        or stat.S_IMODE(held.st_mode) != 0o400 or not 0 < held.st_size <= 16*1024**2
                        or held.st_size != reference.size or _identity(held) != _identity(named)
                        or _identity(held) != _identity(os.stat(name,dir_fd=root,follow_symlinks=False))):
                    raise PreparationError("agent_unlocked_resolution_mismatch")
                entries.append([name,list(_identity(held))])
            finally:
                os.close(opened)
        current_ancestors=[]; current=_open_directory(Path(directory),current_ancestors)
        try:
            if (current_ancestors != ancestors or _identity(os.fstat(current)) != _identity(before)
                    or _identity(os.fstat(root)) != _identity(before) or set(os.listdir(root)) != set(expected)):
                raise PreparationError("agent_unlocked_resolution_mismatch")
        finally:
            os.close(current)
        return contract_digest("agent-unlocked-resolution-metadata-v1",{
            "path":str(directory),"ancestors":[list(item) for item in ancestors],"root":list(_identity(before)),"files":entries})
    except (OSError,KeyError,TypeError,ValueError) as error:
        if isinstance(error,PreparationError):
            raise
        raise PreparationError("agent_unlocked_resolution_mismatch") from None
    finally:
        if root is not None:
            os.close(root)


def read_resolution_inputs(directory, resolution, check, *, with_metadata=False):
    """Read the resolver's already frozen 0400 artifacts with held FD proof."""
    expected = {FILES[item.name]: item for item in resolution.derived_inputs}
    if set(expected) != {"pyproject.toml", "uv.lock"}:
        raise PreparationError("agent_unlocked_resolution_mismatch")
    root = None
    try:
        baseline=resolution_input_metadata(directory,resolution)
        ancestors = []
        root = _open_directory(Path(directory), ancestors)
        initial = os.fstat(root)
        if initial.st_uid != os.getuid() or stat.S_IMODE(initial.st_mode) != 0o700 or set(os.listdir(root)) != set(expected):
            raise PreparationError("agent_unlocked_resolution_mismatch")
        content = {}
        for name, reference in expected.items():
            check()
            before = os.stat(name, dir_fd=root, follow_symlinks=False)
            opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            try:
                held = os.fstat(opened)
                if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                        or stat.S_IMODE(held.st_mode) != 0o400 or _identity(held) != _identity(before)
                        or held.st_size != reference.size or not 0 < held.st_size <= 16 * 1024**2):
                    raise PreparationError("agent_unlocked_resolution_mismatch")
                parts, remaining = [], held.st_size
                while remaining:
                    check(); part = os.read(opened, min(remaining, 65536))
                    if not part:
                        raise PreparationError("agent_unlocked_resolution_mismatch")
                    parts.append(part); remaining -= len(part)
                data = b"".join(parts)
                if (os.read(opened, 1) or _identity(os.fstat(opened)) != _identity(held)
                        or _identity(os.stat(name, dir_fd=root, follow_symlinks=False)) != _identity(held)
                        or hashlib.sha256(data).hexdigest() != reference.sha256):
                    raise PreparationError("agent_unlocked_resolution_mismatch")
                content[name] = data
            finally:
                os.close(opened)
        visible_ancestors = []
        visible = _open_directory(Path(directory), visible_ancestors)
        try:
            if (visible_ancestors != ancestors or _identity(os.fstat(visible)) != _identity(initial)
                    or _identity(os.fstat(root)) != _identity(initial) or set(os.listdir(root)) != set(expected)):
                raise PreparationError("agent_unlocked_resolution_mismatch")
        finally:
            os.close(visible)
        check()
        if resolution_input_metadata(directory,resolution) != baseline:
            raise PreparationError("agent_unlocked_resolution_mismatch")
        return (content,baseline) if with_metadata else content
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("agent_unlocked_resolution_mismatch") from None
    finally:
        if root is not None:
            os.close(root)


class UnlockedCompletionRequestV1(AdapterRequestV1):
    proposal_input_digest: Digest
    resolution_job_id: UUID
    resolution_plan_digest: Digest
    resolution_result_digest: Digest
    resolver_request_digest: Digest
    resolver_runtime: RuntimeReference
    resolver: ComponentReference
    resolution_policy: ResolutionPolicyV1

    @property
    def digest(self):
        return contract_digest("agent-unlocked-completion-request-v1", self.model_dump(mode="json"))


class UnlockedResultV1(InputContract):
    schema_version: Literal[1]
    request: AdapterRequestV1 | UnlockedCompletionRequestV1
    specification: UnlockedAdapterSpecificationV1
    collection: CollectionReference
    derived_inputs: tuple[DerivedInputReference, ...] = Field(min_length=3, max_length=5)
    resolution: ResolutionResultV1 | None = None

    @model_validator(mode="after")
    def closure(self):
        ready = isinstance(self.request, UnlockedCompletionRequestV1)
        expected = set(FILES) if ready else {name for name in FILES if name.startswith("adapter-")}
        if (self.collection.kind != "derived_input_collection" or {item.name for item in self.derived_inputs} != expected
                or len(self.derived_inputs) != len(expected) or ready != (self.resolution is not None)):
            raise PreparationError("agent_unlocked_input_changed")
        if ready:
            request, resolved = self.request, self.resolution
            if (resolved.digest != request.resolution_result_digest or resolved.request.digest != request.resolver_request_digest
                    or resolved.request.producer != request.resolver or resolved.request.runtime != request.resolver_runtime
                    or resolved.request.policy != request.resolution_policy
                    or resolved.request.import_id != request.import_id or resolved.request.revision != request.revision
                    or resolved.request.source_revision != request.source_revision
                    or tuple(resolved.request.policy.operations) != ("resolve_dependencies",)
                    or resolved.tools or resolved.protocol_version is not None):
                raise PreparationError("agent_unlocked_resolution_mismatch")
        records, dependencies = [], []
        for item in self.derived_inputs:
            parents = [self.request.source_revision.removeprefix("sha256:")]
            if ready:
                parents.append(self.request.resolver_runtime.digest)
                if item.name.startswith("adapter-"):
                    parents.extend(dependencies)
            role = item.name if item.name.startswith("dependency-") else "adapter"
            if (item.role != role or item.producer != self.request.producer
                    or item.source_revision != self.request.source_revision or item.request_digest != self.request.digest
                    or item.policy_digest != self.request.policy_digest or item.collection_digest != self.collection.digest
                    or item.parent_digests != tuple(parents)):
                raise PreparationError("agent_unlocked_input_changed")
            if item.name.startswith("dependency-"):
                dependencies.append(item.digest)
            records.append({"path": FILES[item.name], "sha256": item.sha256, "size": item.size})
        if ready and [item.name for item in self.derived_inputs[:2]] != ["dependency-project", "dependency-lock"]:
            raise PreparationError("agent_unlocked_input_changed")
        if contract_digest("agent-unlocked-input-collection-v1", sorted(records, key=lambda item: item["path"])) != self.collection.digest:
            raise PreparationError("agent_unlocked_input_changed")
        interface = next(item for item in self.derived_inputs if item.name == "adapter-interface")
        data = canonical_bytes(self.specification.model_dump(mode="json", exclude_none=True))
        if interface.size != len(data) or interface.sha256 != hashlib.sha256(data).hexdigest():
            raise PreparationError("agent_unlocked_input_changed")
        if ready:
            for original in self.resolution.derived_inputs:
                cloned = next(item for item in self.derived_inputs if item.name == original.name)
                if (cloned.sha256, cloned.size) != (original.sha256, original.size):
                    raise PreparationError("agent_unlocked_resolution_mismatch")
        return self

    @property
    def digest(self):
        return contract_digest("agent-unlocked-result-v1", self.model_dump(mode="json", exclude_none=True))


def _result(request, specification, content, resolution=None):
    records = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in sorted(content.items())]
    collection = CollectionReference(kind="derived_input_collection", schema_version=1,
        digest=contract_digest("agent-unlocked-input-collection-v1", records))
    names = ["dependency-project", "dependency-lock"] if resolution is not None else []
    names += ["adapter-license", "adapter-interface", "adapter-source"]
    by_path, dependencies, references = {item["path"]: item for item in records}, [], []
    for name in names:
        item = by_path[FILES[name]]
        parents = [request.source_revision.removeprefix("sha256:")]
        if resolution is not None:
            parents.append(request.resolver_runtime.digest)
            if name.startswith("adapter-"):
                parents.extend(dependencies)
        reference = DerivedInputReference(name=name, role=name if name.startswith("dependency-") else "adapter",
            collection_digest=collection.digest, sha256=item["sha256"], size=item["size"], producer=request.producer.model_dump(),
            source_revision=request.source_revision, request_digest=request.digest, policy_digest=request.policy_digest,
            parent_digests=parents)
        references.append(reference)
        if name.startswith("dependency-"):
            dependencies.append(reference.digest)
    return UnlockedResultV1(schema_version=1, request=request.model_dump(mode="json"),
        specification=specification.model_dump(mode="json"), collection=collection.model_dump(),
        derived_inputs=[item.model_dump(mode="json") for item in references],
        resolution=resolution.model_dump(mode="json") if resolution else None)


class AgentUnlockedInputStore:
    def __init__(self, imports, recipes):
        self.imports, self.recipes, self.db = imports, recipes, imports._connection
        self.root = ensure_private_directory(imports.root / "agent-unlocked-inputs")
        with imports._transaction():
            if not hasattr(imports, "_agent_unlocked_workers"):
                imports._agent_unlocked_workers = {}
            self.workers = imports._agent_unlocked_workers
            self.db.execute("CREATE TABLE IF NOT EXISTS agent_unlocked_inputs (id TEXT PRIMARY KEY, scope TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
            for row in self.db.execute("SELECT data FROM agent_unlocked_inputs").fetchall():
                item = json.loads(row[0])
                if item["state"] in {"writing", "deleting"} and item["id"] not in self.workers:
                    item.update(state="interrupted", ownership_settled=False); self._save(item)

    def _save(self, item):
        self.db.execute("UPDATE agent_unlocked_inputs SET data=? WHERE id=?", (canonical_bytes(item).decode(), item["id"]))

    def _get(self, identity):
        row = self.db.execute("SELECT data FROM agent_unlocked_inputs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise PreparationError("agent_unlocked_input_unavailable")
        return json.loads(row[0])

    def get(self, identity):
        with self.imports._lock:
            return self._get(identity)

    def for_job(self, job_id, phase):
        if phase not in {"proposal", "completed"}:
            raise PreparationError("agent_unlocked_input_unavailable")
        with self.imports._lock:
            row = self.db.execute("SELECT id FROM agent_unlocked_inputs WHERE scope=?", (job_id + ":" + phase,)).fetchone()
            return self._get(row[0]) if row else None

    def _current(self, request, draft, *, accepted=False):
        current = self.imports._get(request.import_id, draft["revision"])
        self.imports._mutable(current)
        if (current["source"] != draft["source"] or current["source"]["revision"] != request.source_revision
                or contract_digest("agent-host-source-v1", current["source"]) != request.source_identity_digest):
            raise PreparationError("agent_unlocked_source_mismatch")
        recipe = self.recipes.assert_admissible(request.recipe.id, request.recipe.version, request.recipe.digest)
        if recipe["epoch"] != request.recipe_epoch:
            raise PreparationError("agent_recipe_changed")
        if (not accepted and time.time() >= request.expires_at) or current["expires_at"] <= time.time():
            raise PreparationError("agent_authority_expired")
        if not accepted and current["revision"] != request.revision:
            raise PreparationError("agent_unlocked_revision_mismatch")

    def _accepted(self, item, draft):
        if draft["revision"] == item["request"]["revision"]:
            return False
        if item["phase"] == "proposal":
            completed = self.for_job(item["request"]["job_id"], "completed")
            if (completed is None or completed["state"] != "ready" or not completed["ownership_settled"]
                    or completed["proposal_input_digest"] != item["result_digest"]):
                raise PreparationError("agent_unlocked_revision_mismatch")
            return self._accepted(completed, draft)
        row = self.db.execute("SELECT data FROM preparation_selections WHERE import_id=?", (draft["draft_id"],)).fetchone()
        if row is None or item["phase"] != "completed":
            raise PreparationError("agent_unlocked_revision_mismatch")
        selected = json.loads(row[0]); request = item["request"]
        components = selected["descriptor"].get("components", [])
        if (selected["revision"] != draft["revision"] or selected["source_revision"] != request["source_revision"]
                or selected["candidate_id"] != item["proposal_input_digest"]
                or selected["resolution_digest"] != request["resolution_result_digest"]
                or selected["descriptor"]["recipe"] != request["recipe"] or len(components) != 1
                or components[0]["component"] != request["producer"]
                or components[0]["input"] != {"derivation_id": item["id"], "derivation_digest": item["result_digest"]}):
            raise PreparationError("agent_unlocked_revision_mismatch")
        return True

    def proposal_candidate(self, import_id, revision, candidate_id, *, full=True):
        with self.imports._lock:
            rows = self.db.execute("SELECT id FROM agent_unlocked_inputs WHERE json_extract(data,'$.phase')='proposal' "
                "AND json_extract(data,'$.result_digest')=? AND json_extract(data,'$.request.import_id')=?", (candidate_id, import_id)).fetchall()
            if len(rows) != 1:
                raise PreparationError("agent_unlocked_input_unavailable")
            item = self._get(rows[0][0])
            # Return a distinct copy; callers cannot update stored authority by
            # changing a candidate or draft dictionary.
            draft = json.loads(json.dumps(self.imports._get(import_id, revision)))
        if full:
            result, _ = self.resolve(item["id"], candidate_id, draft)
            snapshot, _ = original_snapshot(draft)
            project = canonical_dependency_project(result.specification, snapshot)
            tool_digest = contract_digest("agent-unlocked-tool-v1", result.specification.tool.model_dump(exclude_none=True))
            project_digest = hashlib.sha256(project).hexdigest()
        else:
            self.check_metadata(item["id"], candidate_id, draft)
            # These values bind the full successful worker proof and are
            # checked without reading proposal contents on admission.
            project_digest, tool_digest = item["project_digest"], item["tool_digest"]
        candidate = {"candidate_id": candidate_id, "kind": "python-callable", "root": ".", "module": MODULE,
            "function": "invoke", "_agent_proposal_id": item["id"], "_agent_project_digest": project_digest,
            "_agent_tool_digest": tool_digest, "_agent_request_digest": contract_digest("agent-unlocked-candidate-request-v1", item["request"])}
        if full and (candidate["_agent_project_digest"] != item["project_digest"] or candidate["_agent_tool_digest"] != item["tool_digest"]):
            raise PreparationError("agent_unlocked_input_changed")
        return draft, candidate

    def check_metadata(self, identity, digest, draft):
        with self.imports._lock:
            item = self._get(identity)
            if item["state"] != "ready" or not item["ownership_settled"] or item["result_digest"] != digest:
                raise PreparationError("agent_unlocked_input_unavailable")
            cls = UnlockedCompletionRequestV1 if item["phase"] == "completed" else AdapterRequestV1
            request = cls.model_validate(item["request"])
            self._current(request, draft, accepted=self._accepted(item, draft))
        if _metadata(draft) != item["source_metadata"] or _metadata(_derived_draft(self.root / identity, item["records"])) != item["derived_metadata"]:
            raise PreparationError("agent_unlocked_input_changed")
        if item["phase"] == "completed" and resolution_input_metadata(Path(item["resolution_path"]),
                ResolutionResultV1.model_validate(item["resolution"])) != item["resolution_metadata"]:
            raise PreparationError("agent_unlocked_resolution_mismatch")
        return item, request

    def resolve(self, identity, digest, draft, check=lambda: None):
        item, request = self.check_metadata(identity, digest, draft)
        check()
        if original_snapshot(draft, check)[1] != item["source_metadata"]:
            raise PreparationError("agent_unlocked_input_changed")
        snapshot, baseline = original_snapshot(_derived_draft(self.root / identity, item["records"]), check)
        if baseline != item["derived_metadata"]:
            raise PreparationError("agent_unlocked_input_changed")
        content = {entry.path: entry.data for entry in snapshot.files}
        specification = UnlockedAdapterSpecificationV1.model_validate(load_contract(content["adapter.json"]))
        resolved = ResolutionResultV1.model_validate(item["resolution"]) if item["phase"] == "completed" else None
        result = _result(request, specification, content, resolved)
        if result.digest != digest:
            raise PreparationError("agent_unlocked_input_changed")
        self.check_metadata(identity, digest, draft); check()
        return result, self.root / identity

    def stage(self, job, plan, proposal, producer, draft, check=lambda: None):
        request = AdapterRequestV1(schema_version=1, import_id=job["import_id"], revision=job["revision"],
            source_revision=job["source_revision"], source_identity_digest=plan["source_identity_digest"],
            job_id=job["job_id"], plan_digest=plan["plan_digest"], request_digest=plan["request_digest"],
            proposal_digest=proposal.digest, attempt_id=proposal.attempt_id, generation=proposal.generation,
            recipe=plan["recipe"], recipe_epoch=plan["recipe_epoch"], producer=producer.model_dump(),
            policy_digest=contract_digest("agent-unlocked-staging-policy-v1", {"limits": plan["limits"], "namespace": MODULE}),
            expires_at=plan["expires_at"])
        if (proposal.request_digest != request.request_digest or proposal.attempt_id != job["attempt_id"]
                or proposal.digest != job["proposal_digest"]):
            raise PreparationError("agent_adapter_attempt_mismatch")
        snapshot, baseline = original_snapshot(draft, check)
        specification, content = specification_from_proposal(proposal, snapshot)
        result = _result(request, specification, content)
        item = self._persist("proposal", result, content, baseline, draft, check)
        return self.resolve(item["id"], result.digest, draft, check)[0]

    def materialize(self, proposal_item, resolution_job, resolution, directory, draft, check):
        proposed, proposed_path = self.resolve(proposal_item["id"], proposal_item["result_digest"], draft, check)
        if (resolution.request.candidate_id != proposed.digest or resolution.request.source_revision != proposed.request.source_revision
                or resolution.request.import_id != proposed.request.import_id or resolution.request.revision != proposed.request.revision
                or resolution_job["request"] != resolution.request.model_dump(mode="json") or resolution_job["ownership"]
                or resolution_job["state"] in {"succeeded", "failed", "interrupted", "canceled", "cancel_requested"}):
            raise PreparationError("agent_unlocked_resolution_mismatch")
        from .preparation_resolution import result_from_files
        if result_from_files(resolution.request, directory, (), None, resolution.peak_memory_bytes) != resolution:
            raise PreparationError("agent_unlocked_resolution_mismatch")
        proposal_snapshot, _ = original_snapshot(_derived_draft(proposed_path, proposal_item["records"]), check)
        content = {entry.path: entry.data for entry in proposal_snapshot.files}
        resolved_content,resolution_metadata=read_resolution_inputs(directory,resolution,check,with_metadata=True)
        content.update(resolved_content)
        if content["pyproject.toml"] != canonical_dependency_project(proposed.specification, original_snapshot(draft, check)[0]):
            raise PreparationError("agent_unlocked_resolution_mismatch")
        request = UnlockedCompletionRequestV1(**proposed.request.model_dump(mode="json"),
            proposal_input_digest=proposed.digest, resolution_job_id=resolution_job["job_id"],
            resolution_plan_digest=resolution_job["plan_digest"], resolution_result_digest=resolution.digest,
            resolver_request_digest=resolution.request.digest, resolver_runtime=resolution.request.runtime.model_dump(),
            resolver=resolution.request.producer.model_dump(), resolution_policy=resolution.request.policy.model_dump(mode="json"))
        result = _result(request, proposed.specification, content, resolution)
        return self._persist("completed",result,content,proposal_item["source_metadata"],draft,check,
            resolution_path=directory,resolution_metadata=resolution_metadata)

    def _persist(self,phase,result,content,baseline,draft,external_check,*,resolution_path=None,resolution_metadata=None):
        request = result.request
        records = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in sorted(content.items())]
        expected = set(FILES.values()) if phase == "completed" else {FILES[name] for name in FILES if name.startswith("adapter-")}
        if set(content) != expected or sum(map(len, content.values())) > 16 * 1024**2:
            raise PreparationError("agent_unlocked_input_changed")
        proposal_metadata = {}
        if phase == "proposal":
            snapshot, observed = original_snapshot(draft, external_check)
            if observed != baseline:
                raise PreparationError("agent_unlocked_input_changed")
            proposal_metadata = {"tool_digest": contract_digest("agent-unlocked-tool-v1", result.specification.tool.model_dump(exclude_none=True)),
                "project_digest": hashlib.sha256(canonical_dependency_project(result.specification, snapshot)).hexdigest()}
        with self.imports._transaction():
            self._current(request, draft)
            old = self.for_job(request.job_id, phase)
            if old is not None and old["result_digest"] != result.digest:
                raise PreparationError("agent_unlocked_result_conflict")
        if old is not None:
            self.resolve(old["id"], result.digest, draft, external_check)
            return old
        with self.imports._transaction():
            self._current(request, draft)
            if self.db.execute("SELECT count(*) FROM agent_unlocked_inputs").fetchone()[0] >= MAX_INPUTS:
                raise PreparationError("agent_unlocked_input_limit")
            identity = str(uuid.uuid4())
            item = {"schema_version": 1, "id": identity, "phase": phase, "state": "writing", "ownership_settled": False,
                "request": request.model_dump(mode="json"), "result_digest": result.digest, "records": records,
                "source_metadata": baseline, "derived_metadata": None, "root_identity": None, "file_identities": {},
                "proposal_input_digest": request.proposal_input_digest if phase == "completed" else result.digest,
                "resolution": result.resolution.model_dump(mode="json") if result.resolution else None}
            if phase == "completed":
                if resolution_path is None or resolution_metadata is None:
                    raise PreparationError("agent_unlocked_resolution_mismatch")
                item.update(resolution_path=str(resolution_path),resolution_metadata=resolution_metadata)
            item.update(proposal_metadata)
            item["tool_digest"] = contract_digest("agent-unlocked-tool-v1", result.specification.tool.model_dump(exclude_none=True))
            self.db.execute("INSERT INTO agent_unlocked_inputs VALUES (?,?,?)", (identity, request.job_id+":"+phase, canonical_bytes(item).decode()))
            self.workers[identity] = threading.Event()
        def check():
            external_check()
            if self.workers[identity].is_set():
                raise PreparationError("agent_cancel_requested")
        root = None
        try:
            check()
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir(identity, mode=0o700, dir_fd=parent)
                root = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                details = os.fstat(root); item["root_identity"] = [details.st_dev, details.st_ino, details.st_mode]
                with self.imports._transaction():
                    self._save(item)
                os.fsync(parent)
            finally:
                os.close(parent)
            for name, data in content.items():
                check()
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
                with os.fdopen(fd, "wb") as stream:
                    details = os.fstat(stream.fileno()); item["file_identities"][name] = [details.st_dev, details.st_ino, details.st_mode]
                    with self.imports._transaction():
                        self._save(item)
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                check()
            os.fsync(root)
            if _metadata(draft) != baseline:
                raise PreparationError("agent_unlocked_input_changed")
            _, item["derived_metadata"] = original_snapshot(_derived_draft(self.root/identity, records), check)
            if original_snapshot(draft, check)[1] != baseline:
                raise PreparationError("agent_unlocked_input_changed")
            with self.imports._transaction():
                check(); self._current(request, draft)
                if (_metadata(draft) != baseline or _metadata(_derived_draft(self.root/identity, records))
                        != item["derived_metadata"]):
                    raise PreparationError("agent_unlocked_input_changed")
                if phase == "completed" and resolution_input_metadata(resolution_path,result.resolution) != resolution_metadata:
                    raise PreparationError("agent_unlocked_resolution_mismatch")
                item.update(state="ready", ownership_settled=True); self._save(item)
            return item
        except BaseException:
            with self.imports._transaction():
                item.update(state="interrupted", ownership_settled=False); self._save(item)
            raise
        finally:
            if root is not None:
                os.close(root)
            self.workers.pop(identity, None)

    def cancel_job(self, job_id):
        with self.imports._transaction():
            for phase in ("proposal", "completed"):
                item = self.for_job(job_id, phase)
                if item is None or item["state"] in {"canceled", "deleted"}:
                    continue
                active = self.workers.get(item["id"])
                if active is not None:
                    active.set(); item.update(state="interrupted", ownership_settled=False)
                elif item["state"] == "ready":
                    item.update(state="canceled", ownership_settled=True)
                self._save(item)

    def recover(self, identity):
        with self.imports._transaction():
            item = self._get(identity)
            if identity in self.workers or item["state"] != "interrupted":
                raise PreparationError("agent_unlocked_ownership_unsettled")
            if item["root_identity"] is not None:
                details = (self.root/identity).lstat()
                if [details.st_dev, details.st_ino, details.st_mode] != item["root_identity"]:
                    raise PreparationError("agent_unlocked_ownership_unsettled")
            elif (self.root/identity).exists():
                raise PreparationError("agent_unlocked_ownership_unsettled")
            item.update(state="canceled", ownership_settled=True); self._save(item)
            return item

    def delete(self, identity):
        with self.imports._transaction():
            item = self._get(identity)
            if identity in self.workers or item["state"] != "canceled" or not item["ownership_settled"]:
                raise PreparationError("agent_unlocked_ownership_unsettled")
            item.update(state="deleting", ownership_settled=False); self._save(item)
        parent = root = None
        try:
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if item["root_identity"] is not None:
                root = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                details = os.fstat(root)
                if [details.st_dev, details.st_ino, details.st_mode] != item["root_identity"]:
                    raise PreparationError("agent_unlocked_ownership_unsettled")
                visible = set(os.listdir(root))
                if not visible <= set(item["file_identities"]) or not visible <= set(FILES.values()):
                    raise PreparationError("agent_unlocked_ownership_unsettled")
                for name in visible:
                    details = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1
                            or stat.S_IMODE(details.st_mode) != 0o600
                            or [details.st_dev, details.st_ino, details.st_mode] != item["file_identities"][name]):
                        raise PreparationError("agent_unlocked_ownership_unsettled")
                    os.unlink(name, dir_fd=root)
                os.fsync(root)
                details = os.stat(identity, dir_fd=parent, follow_symlinks=False)
                if [details.st_dev, details.st_ino, details.st_mode] != item["root_identity"]:
                    raise PreparationError("agent_unlocked_ownership_unsettled")
                os.rmdir(identity, dir_fd=parent)
            elif os.path.lexists(self.root/identity):
                raise PreparationError("agent_unlocked_ownership_unsettled")
            os.fsync(parent)
            with self.imports._transaction():
                item.update(state="deleted", ownership_settled=True); self._save(item)
        except BaseException:
            with self.imports._transaction():
                item.update(state="interrupted", ownership_settled=False); self._save(item)
            raise
        finally:
            for descriptor in (root, parent):
                if descriptor is not None:
                    os.close(descriptor)
