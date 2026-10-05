"""Explicit resolution for a durably staged untrusted adapter proposal.

This trusted dispatcher preserves ordinary resolver behavior for original
candidates. Only its separately reviewed plan/start path can launch uv.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from pathlib import Path

from .preparation_agent_adapter_store import original_snapshot
from .preparation_agent_unlocked_contracts import canonical_dependency_project
from .preparation_components import ComponentReference
from .preparation_contracts import PreparationError, contract_digest
from .preparation_builder import load_private_toolchain
from .preparation_resolution import ResolutionStore, ResolutionRequestV1, ResolutionPolicyV1
from .preparation_resolution_network import ResolutionIndexBroker
from .preparation_resolution_runner import OwnedResolutionRunner
from .processes import _await_ownership

_AGENT_RESOLUTION = contextvars.ContextVar("mcpme_agent_resolution_producer", default=False)


class AgentUnlockedResolutionStore(ResolutionStore):
    def __init__(self, imports, inputs):
        super().__init__(imports)
        self.inputs = inputs

    def _agent(self, import_id, candidate_id):
        with self.imports._lock:
            return self.db.execute("SELECT id FROM agent_unlocked_inputs WHERE json_extract(data,'$.phase')='proposal' "
                "AND json_extract(data,'$.request.import_id')=? AND json_extract(data,'$.result_digest')=?", (import_id, candidate_id)).fetchone()

    def candidate(self, import_id, revision, candidate_id):
        if self._agent(import_id, candidate_id) is None:
            return super().candidate(import_id, revision, candidate_id)
        return self.inputs.proposal_candidate(import_id, revision, candidate_id)

    def _current(self, request):
        draft = super()._current(request)
        if self._agent(request.import_id, request.candidate_id) is not None:
            _, candidate = self.inputs.proposal_candidate(request.import_id, request.revision, request.candidate_id, full=False)
            if (request.producer.id != "python-agent-resolution-v1" or request.candidate_kind != "python-callable"
                    or tuple(request.policy.operations) != ("resolve_dependencies",)
                    or set(request.policy.network) != {"pypi.org", "files.pythonhosted.org"}
                    or contract_digest("preparation-resolution-candidate-v1", candidate) != request.candidate_digest):
                raise PreparationError("agent_unlocked_resolution_mismatch")
        return draft

    def project(self, draft, candidate, check=lambda: None):
        result, _ = self.inputs.resolve(candidate["_agent_proposal_id"], candidate["candidate_id"], draft, check)
        data = canonical_dependency_project(result.specification, original_snapshot(draft, check)[0])
        if hashlib.sha256(data).hexdigest() != candidate["_agent_project_digest"]:
            raise PreparationError("agent_unlocked_input_changed")
        check()
        return data

    def publish(self, job_id, result):
        if self._agent(result.request.import_id, result.request.candidate_id) is None:
            return super().publish(job_id, result)
        draft, candidate = self.candidate(result.request.import_id, result.request.revision, result.request.candidate_id)
        job = self.get(job_id)
        check = lambda: self.check_authority(job_id)
        check()
        if contract_digest("preparation-resolution-candidate-v1", candidate) != result.request.candidate_digest:
            raise PreparationError("agent_unlocked_resolution_mismatch")
        item = self.inputs.get(candidate["_agent_proposal_id"])
        self.inputs.materialize(item, job, result, Path(job["workspace"])/"derived", draft, check)
        check()
        return super().publish(job_id, result)


class OwnedAgentUnlockedResolutionRunner(OwnedResolutionRunner):
    async def _static(self, function, *args):
        if self._closed:
            raise PreparationError("resolution_runner_unavailable")
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _await_ownership(task)
            except BaseException:
                pass
            raise
        if self._closed:
            raise PreparationError("resolution_runner_unavailable")
        return result

    async def _resolution_plan(self, import_id, revision, candidate_id, policy, expires_in):
        draft, candidate = await self._static(self.store.candidate, import_id, revision, candidate_id)
        if "_agent_proposal_id" not in candidate:
            return await super()._resolution_plan(import_id, revision, candidate_id, policy, expires_in)
        try:
            policy = ResolutionPolicyV1.model_validate(policy)
        except ValueError:
            raise PreparationError("invalid_resolution_policy") from None
        if tuple(policy.operations) != ("resolve_dependencies",) or set(policy.network) != {"pypi.org", "files.pythonhosted.org"}:
            raise PreparationError("resolution_policy_mismatch")
        await self._static(self.store.project, draft, candidate)
        toolchain = await self._static(load_private_toolchain, self.host_root)
        token = _AGENT_RESOLUTION.set(True)
        try:
            producer = await self._producer()
        finally:
            _AGENT_RESOLUTION.reset(token)
        request = ResolutionRequestV1(schema_version=1, import_id=import_id, revision=revision,
            source_revision=draft["source"]["revision"], candidate_id=candidate_id, candidate_kind="python-callable",
            candidate_digest=contract_digest("preparation-resolution-candidate-v1", candidate), policy=policy.model_dump(mode="json"),
            runtime={"name": "private-python-toolchain", "digest": toolchain.identity}, producer=producer.model_dump())
        if self._closed:
            raise PreparationError("resolution_runner_unavailable")
        return self.store.plan(request, candidate, expires_in=expires_in)

    def _producer_sync(self):
        original = super()._producer_sync()
        if _AGENT_RESOLUTION.get() is not True:
            return original
        root = Path(__file__).parent
        names = ("preparation_agent_unlocked_resolution.py", "preparation_agent_unlocked_store.py",
            "preparation_agent_unlocked_contracts.py", "preparation_agent_adapter_store.py", "preparation_selection_snapshot.py")
        identity = contract_digest("agent-unlocked-resolution-producer-v1", {"ordinary": original.model_dump(),
            "implementation": [{"path": name, "sha256": hashlib.sha256((root/name).read_bytes()).hexdigest()} for name in names]})
        return ComponentReference(id="python-agent-resolution-v1", version="1.0.0", digest=identity)

    async def _work(self, attempt):
        candidate = self.store.get(attempt.job_id)["candidate"]
        token = _AGENT_RESOLUTION.set("_agent_proposal_id" in candidate)
        try:
            return await super()._work(attempt)
        finally:
            _AGENT_RESOLUTION.reset(token)

    async def _resolve(self, attempt, toolchain, draft, candidate, executor):
        if "_agent_proposal_id" not in candidate:
            return await super()._resolve(attempt, toolchain, draft, candidate, executor)
        directory = attempt.workspace/"derived"; directory.mkdir(mode=0o700)
        project = await self._thread(attempt, self.store.project, draft, candidate, attempt.check)
        (directory/"pyproject.toml").write_bytes(project)
        self.store.stage(attempt.job_id, "resolving")
        async with ResolutionIndexBroker(attempt.workspace/"index", byte_limit=attempt.policy.disk_bytes,
                check=attempt.check) as broker:
            config = attempt.workspace/"uv.toml"
            config.write_text('[[index]]\nname = "mcpme-resolution"\nurl = '+json.dumps(broker.base+"/simple")+
                '\ndefault = true\nauthenticate = "always"\n')
            try:
                await self._execute(attempt, executor, toolchain, [toolchain.uv, "lock", "--quiet", "--project", directory,
                    "--config-file", config, "--no-sources", "--no-build", "--no-python-downloads", "--keyring-provider", "disabled"],
                    broker=broker, env_extra={"UV_INDEX_MCPME_RESOLUTION_USERNAME": "mcpme", "UV_INDEX_MCPME_RESOLUTION_PASSWORD": broker.password})
            except PreparationError:
                if broker.error_code:
                    raise PreparationError(broker.error_code) from None
                raise
            lock = directory/"uv.lock"
            if lock.stat().st_size > 16*1024**2:
                raise PreparationError("dependency_lock_limit")
            lock.write_bytes(broker.canonical_lock(lock.read_bytes()))
        return directory
