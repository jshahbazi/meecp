"""One public resolution owner with separate ordinary/proposal authority.

The published unlocked family modules remain immutable. This trusted adapter
adds public policy and producer fences without granting arbitrary agent markers
ordinary resolution authority or running two recovery loops over one journal.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .preparation_agent_unlocked_resolution import (
    AgentUnlockedResolutionStore, OwnedAgentUnlockedResolutionRunner, _AGENT_RESOLUTION)
from .preparation_builder import load_private_toolchain
from .preparation_components import ComponentReference
from .preparation_contracts import PreparationError, contract_digest
from .preparation_release_policy import AgentExecutionAdmission
from .preparation_resolution import ResolutionStore, ResolutionRequestV1, ResolutionPolicyV1
from .preparation_resolution_runner import OwnedResolutionRunner


class PublicAgentResolutionStore(AgentUnlockedResolutionStore):
    def __init__(self, imports, inputs, *, public_admission, agent_admission, current_producer):
        if (type(agent_admission) is not AgentExecutionAdmission or not agent_admission.unlocked_resolution
                or public_admission is None):
            raise PreparationError("invalid_agent_execution_admission")
        ResolutionStore.__init__(self, imports, public_admission=public_admission, current_producer=current_producer)
        self.inputs, self.agent_admission = inputs, agent_admission

    def _proposal(self, import_id, candidate_id):
        return self._agent(import_id, candidate_id) is not None

    def _require_proposal_component(self, candidate):
        item = self.inputs.get(candidate["_agent_proposal_id"])
        reference = ComponentReference.model_validate(item["request"]["producer"])
        if (not self.agent_admission.unlocked_resolution or reference.key not in self.agent_admission.components
                or reference.id != "agent-unlocked-python-adapter-v1"):
            raise PreparationError("resolution_candidate_not_admitted")

    def candidate(self, import_id, revision, candidate_id):
        draft, candidate = super().candidate(import_id, revision, candidate_id)
        if self._proposal(import_id, candidate_id):
            self._require_proposal_component(candidate)
        else:
            self.public_admission.require_candidate(candidate)
        return draft, candidate

    def require_public_kind(self, request):
        self.agent_admission.check_current()
        if not self._proposal(request.import_id, request.candidate_id):
            return super().require_public_kind(request)
        if (not self.agent_admission.unlocked_resolution or request.candidate_kind != "python-callable"
                or request.producer.id != "python-agent-resolution-v1" or request.producer.version != "1.0.1"
                or tuple(request.policy.operations) != ("resolve_dependencies",)
                or set(request.policy.network) != {"pypi.org", "files.pythonhosted.org"}):
            raise PreparationError("resolution_candidate_not_admitted")

    def current_producer_for(self, request):
        return self._current_producer(request)

    def require_public_request(self, request, candidate=None):
        if not self._proposal(request.import_id, request.candidate_id):
            return super().require_public_request(request, candidate)
        with self.imports._lock:
            if self.db.in_transaction:
                raise PreparationError("resolution_prevalidation_transaction")
        self.require_public_kind(request)
        draft, original = self.candidate(request.import_id, request.revision, request.candidate_id)
        if (draft["source"]["revision"] != request.source_revision
                or contract_digest("preparation-resolution-candidate-v1", original) != request.candidate_digest
                or candidate is not None and candidate != original):
            raise PreparationError("resolution_candidate_not_admitted")
        self.project(draft, original)
        self.require_public_producer(request)

    def publish(self, job_id, result):
        # No derived collection may materialize under a stale public producer.
        self.require_public_request(result.request)
        return super().publish(job_id, result)


class PublicAgentResolutionRunner(OwnedAgentUnlockedResolutionRunner):
    def __init__(self, *args, agent_admission, **kwargs):
        if (type(agent_admission) is not AgentExecutionAdmission
                or type(args[0]) is not PublicAgentResolutionStore
                or args[0].agent_admission is not agent_admission):
            raise PreparationError("invalid_agent_execution_admission")
        self.agent_admission = agent_admission
        super().__init__(*args, **kwargs)

    def producer_for(self, request):
        token = _AGENT_RESOLUTION.set(request.producer.id == "python-agent-resolution-v1")
        try:
            return self._producer_sync()
        finally:
            _AGENT_RESOLUTION.reset(token)

    def _producer_sync(self):
        self.agent_admission.check_current(full=True)
        original = super()._producer_sync()
        if _AGENT_RESOLUTION.get() is not True:
            return original
        return ComponentReference(id="python-agent-resolution-v1", version="1.0.1", digest=contract_digest(
            "public-agent-resolution-producer-v1", {"implementation": original.model_dump(),
                "dispatcher": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "admission": self.agent_admission.digest}))

    async def _resolution_plan(self, import_id, revision, candidate_id, policy, expires_in):
        draft, candidate = await self._static(self.store.candidate, import_id, revision, candidate_id)
        if "_agent_proposal_id" not in candidate:
            return await OwnedResolutionRunner._resolution_plan(self, import_id, revision, candidate_id, policy, expires_in)
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
        return await self._static(lambda: self.store.plan(request, candidate, expires_in=expires_in))
