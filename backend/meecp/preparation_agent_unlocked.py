"""Trusted family consuming an untrusted proposal and explicit resolver proof."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path

from .preparation_agent_adapter_store import MODULE
from .preparation_agent_unlocked_store import UnlockedCompletionRequestV1
from .preparation_components import ComponentReference, PreparationDescriptorV2, SourceBinding, SourceFileBinding
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .preparation_agent_adapter import AgentAdapterInput

COMPONENT_ID, VERSION = "agent-unlocked-python-adapter-v1", "1.0.4"


def component_catalog():
    root = Path(__file__).parent
    # The resolver is a distinct producer, bound by its own exact receipt.
    # Registry/route registration is not this component's implementation.
    names = sorted({"preparation_agent_unlocked.py", "preparation_agent_unlocked_contracts.py",
        "preparation_agent_unlocked_store.py", "preparation_agent_unlocked_resolution.py", "preparation_agent_unlocked_builder.py",
        "preparation_agent_unlocked_forms.py", "preparation_agent_adapter.py", "preparation_agent_adapter_store.py",
        "preparation_selection_snapshot.py", "preparation_application_builder.py", "preparation_application_source.py",
        "preparation_builder.py", "preparation_components.py", "preparation_inputs.py", "preparation_contracts.py",
        "contracts.py", "private.py"})
    digest = contract_digest("agent-unlocked-component-v1", [
        {"path": name, "sha256": hashlib.sha256((root/name).read_bytes()).hexdigest()} for name in names])
    return {COMPONENT_ID+"@"+VERSION: {"digest": digest, "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}}


def selected_input(descriptor):
    if (not isinstance(descriptor, PreparationDescriptorV2) or len(descriptor.components) != 1
            or descriptor.components[0].component.id != COMPONENT_ID):
        raise PreparationError("unsupported_component_descriptor")
    return AgentAdapterInput.model_validate(descriptor.components[0].input)


def descriptor_for(result, identity, draft):
    return PreparationDescriptorV2.model_validate({"schema_version": 2, "recipe": result.request.recipe.model_dump(),
        "package": {"id": "agent-unlocked-"+draft["draft_id"], "version": "1.0.0", "name": draft["name"]},
        "components": [{"component": result.request.producer.model_dump(), "input": {"derivation_id": identity, "derivation_digest": result.digest}}],
        "tools": [result.specification.tool.model_dump(exclude_none=True)], "inputs": {}, "artifacts": [], "access": {},
        "resources": {"memory_mb": 128, "cpu_only": True},
        "lifecycle": {"max_sessions": 1, "idle_timeout_seconds": 300, "operation_timeout_seconds": 30},
        "validation": [{"id": "agent-unlocked-fixture", "tool": result.specification.tool.name,
            "arguments": result.specification.arguments, "expected": result.specification.expected, "synthetic": True}]})


class AgentUnlockedPythonComponent:
    from .preparation_agent_adapter import INPUT_SCHEMA as input_schema
    legacy_interface_kinds = frozenset()

    def __init__(self, inputs, resolutions):
        self.inputs, self.resolutions = inputs, resolutions
        self.reference = ComponentReference(id=COMPONENT_ID, version=VERSION, digest=component_catalog()[COMPONENT_ID+"@"+VERSION]["digest"])

    def metadata(self, descriptor, draft):
        value = selected_input(descriptor)
        item, request = self.inputs.check_metadata(value.derivation_id, value.derivation_digest, draft)
        if not isinstance(request, UnlockedCompletionRequestV1):
            raise PreparationError("agent_dependency_resolution_required")
        job = self.resolutions.get(request.resolution_job_id)
        if (job["state"] != "succeeded" or job["ownership_settled"] is not True or job["reservation_retained"] is not False
                or job["ownership"] or job["result_digest"] != request.resolution_result_digest
                or job["plan_digest"] != request.resolution_plan_digest or job["result"] != item["resolution"]
                or job["request"] != item["resolution"]["request"]
                or str(Path(job["workspace"])/"derived") != item["resolution_path"]
                or descriptor.components[0].component != self.reference or request.producer != self.reference
                or descriptor.recipe != request.recipe or descriptor.inputs or descriptor.artifacts
                or descriptor.access.network or descriptor.access.filesystem or descriptor.access.secrets or descriptor.access.writes
                or not descriptor.resources.cpu_only or not 64 <= descriptor.resources.memory_mb <= 4096
                or contract_digest("agent-unlocked-tool-v1", descriptor.tools[0].model_dump(exclude_none=True)) != item["tool_digest"]
                or len(descriptor.tools) != 1):
            raise PreparationError("agent_unlocked_descriptor_mismatch")
        return item, request

    def result(self, descriptor, draft, check=lambda: None):
        item, _ = self.metadata(descriptor, draft)
        result, path = self.inputs.resolve(item["id"], item["result_digest"], draft, check)
        from .preparation_agent_unlocked_store import read_resolution_inputs
        job = self.resolutions.get(result.request.resolution_job_id)
        actual = read_resolution_inputs(Path(job["workspace"])/"derived", result.resolution, check)
        for name, data in actual.items():
            reference = next(ref for ref in result.derived_inputs if ref.name == "dependency-"+("project" if name == "pyproject.toml" else "lock"))
            if hashlib.sha256(data).hexdigest() != reference.sha256:
                raise PreparationError("agent_unlocked_resolution_mismatch")
        self.metadata(descriptor, draft); check()
        return result, path

    def validate_source(self, descriptor, draft, recipe):
        result, _ = self.result(descriptor, draft)
        # The original source has no generated lock declaration. All generated
        # project/lock bytes are separate, typed derived inputs.
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(item["path"], item["sha256"], item["size"]) for item in draft["source"]["files"]),
            result.specification.digest, None, canonical_bytes({"family": "agent-unlocked-python-adapter",
                "proposal_digest": result.request.proposal_digest, "attempt_id": result.request.attempt_id,
                "derivation_digest": result.digest, "resolution_result_digest": result.request.resolution_result_digest}))


class AgentUnlockedPythonFamily:
    source_kind, target_profile = None, "mcp-stdio"

    def __init__(self, inputs, resolutions):
        self.inputs, self.resolutions = inputs, resolutions
        self.component = AgentUnlockedPythonComponent(inputs, resolutions)
        from .preparation_agent_unlocked_forms import AgentUnlockedPythonFormProvider
        self.form_provider = AgentUnlockedPythonFormProvider(self)

    def supports_source(self, draft):
        return draft["source"]["kind"] in {"local_folder", "application_directory"}

    def inspect_source(self, draft):
        with self.inputs.imports._lock:
            rows = self.inputs.db.execute("SELECT id FROM agent_unlocked_inputs WHERE json_extract(data,'$.phase')='proposal' "
                "AND json_extract(data,'$.request.import_id')=?", (draft["draft_id"],)).fetchall()
        candidates = []
        for row in rows:
            item = self.inputs.get(row[0])
            if item["state"] != "ready":
                continue
            try:
                proposed, _ = self.inputs.resolve(item["id"], item["result_digest"], draft)
                if proposed.request.producer != self.component.reference:
                    continue
                completed = self.inputs.for_job(proposed.request.job_id, "completed")
                ready = False
                if completed is not None and completed["state"] == "ready":
                    result, _ = self.inputs.resolve(completed["id"], completed["result_digest"], draft)
                    self.component.result(descriptor_for(result, completed["id"], draft), draft)
                    ready = True
                candidates.append({"candidate_id": proposed.digest, "kind": "agent-unlocked-python-adapter",
                    "module": MODULE, "function": "invoke", "tools": [proposed.specification.tool.model_dump(exclude_none=True)],
                    # The exact result still needs explicit selection in the
                    # ordinary form, even when its resolution job has ended.
                    "unresolved": ["dependency_resolution_required"], "verified": False,
                    "_agent_job_id": proposed.request.job_id, "_agent_proposal_id": item["id"],
                    "_agent_completed_id": completed["id"] if ready else None})
            except (ValueError, KeyError):
                continue
        return {"schema_version": 1, "source_revision": draft["source"]["revision"], "roots": [], "observations": [],
            "execution_available": False, "verified_capabilities": [], "candidates": candidates,
            "blockers": [] if candidates else ["agent_adapter_proposal_required"]}

    def freeze_agent_proposal(self, job, plan, proposal, draft, check=lambda: None):
        self.inputs.stage(job, plan, proposal, self.component.reference, draft, check)
        completed = self.inputs.for_job(job["job_id"], "completed")
        if completed is None:
            raise PreparationError("agent_dependency_resolution_required")
        result, _ = self.inputs.resolve(completed["id"], completed["result_digest"], draft, check)
        descriptor = descriptor_for(result, completed["id"], draft)
        self.component.result(descriptor, draft, check)
        return descriptor

    def cancel_agent_proposal(self, job):
        self.inputs.cancel_job(job["job_id"])

    def selection_binding(self, descriptor, draft):
        result, _ = self.component.result(descriptor, draft)
        return result.request.proposal_input_digest, result.request.resolution_result_digest

    def resolution_binding(self, descriptor):
        value = selected_input(descriptor)
        item = self.inputs.get(value.derivation_id)
        return item["proposal_input_digest"], item["request"]["resolution_result_digest"]

    def validate_inputs(self, descriptor, draft):
        return self.component.result(descriptor, draft)

    def check_current(self, descriptor, draft):
        self.component.metadata(descriptor, draft)
        return True

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import adapt_descriptor, bind_preparation_inputs
        result, _ = self.validate_inputs(descriptor, draft)
        if runtime_digest != result.request.resolver_runtime.digest:
            raise PreparationError("resolution_runtime_mismatch")
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        return bind_preparation_inputs(view, draft=draft, captured_collection=None, derived_collection=result.collection,
            derived_inputs=result.derived_inputs, runtimes=(result.request.resolver_runtime,), capabilities=("tools",),
            expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio"))

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        from .preparation_agent_unlocked_builder import prepare_agent_unlocked_adapter
        result, path = self.component.result(descriptor, draft, check)
        package = prepare_agent_unlocked_adapter(descriptor, draft, result, path, toolchain, output, run, check,
            acquire_wheels=acquire_wheels)
        self.component.result(descriptor, draft, check)
        return package

    def release(self, job_id, descriptor):
        pass  # Immutable separately owned inputs require explicit deletion.

    async def verify(self, package_root, descriptor, reaper, workspace, *, timeout_seconds, owner):
        from .preparation_verifier import verify_prepared_package
        if "confine_private_python" not in inspect.signature(verify_prepared_package).parameters:
            raise PreparationError("preparation_probe_confinement_unavailable")
        value = selected_input(descriptor)
        item = self.inputs.get(value.derivation_id)
        async def validate():
            from .processes import _await_ownership
            draft = self.inputs.imports.inspect(item["request"]["import_id"])
            task = asyncio.create_task(asyncio.to_thread(self.component.result, descriptor, draft))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await _await_ownership(task)
                except BaseException:
                    pass
                raise
        await validate()
        report = await verify_prepared_package(package_root, descriptor, reaper, workspace,
            timeout_seconds=timeout_seconds, owner=owner, confine_private_python=True)
        await validate()
        return report
