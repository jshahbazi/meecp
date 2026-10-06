"""Reviewed derivation family for a separately frozen untrusted Python wrapper."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from pathlib import Path

from pydantic import Field

from .contracts import Digest
from .limits import DEFAULT_REQUEST_BYTES, MAX_REQUEST_BYTES
from .preparation_components import ComponentReference, PreparationDescriptorV2, SourceBinding, SourceFileBinding
from .preparation_contracts import Contract, PreparationError, canonical_bytes, contract_digest
from .preparation_agent_adapter_store import AgentAdapterDerivationStore

COMPONENT_ID, VERSION = "agent-python-adapter-v1", "1.0.2"
INPUT_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["derivation_id", "derivation_digest"], "properties": {
        "derivation_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "derivation_digest": {"type": "string", "minLength": 64, "maxLength": 64}}}


def component_catalog():
    root = Path(__file__).parent
    files = ["preparation_agent_adapter.py", "preparation_agent_adapter_store.py",
        "preparation_agent_adapter_builder.py", "preparation_agent_adapter_forms.py",
        "preparation_agent_runtime.py",
        "preparation_builder.py", "preparation_application_builder.py", "preparation_application_source.py",
        "preparation_selection_snapshot.py", "limits.py"]
    digest = contract_digest("agent-python-adapter-component-v1", [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in files])
    return {COMPONENT_ID + "@1.0.0": {"digest": "28c1d862b947f294dcd0a7f038c32f3d9cf4e31ea20f81d91ab800b13d0b8274",
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []},
        COMPONENT_ID + "@1.0.1": {"digest": "d7eb6da84e83c3c3204fb92e17c467766c1c4d17b930417634f2c8d71338712d",
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []},
        COMPONENT_ID + "@" + VERSION: {"digest": digest,
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}}


class AgentAdapterInput(Contract):
    derivation_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    derivation_digest: Digest


def selected_input(descriptor):
    if not isinstance(descriptor, PreparationDescriptorV2) or len(descriptor.components) != 1:
        raise PreparationError("unsupported_component_descriptor")
    if descriptor.components[0].component.id != COMPONENT_ID:
        raise PreparationError("unsupported_component_descriptor")
    return AgentAdapterInput.model_validate(descriptor.components[0].input)


def request_limit_for(tool):
    # Primitive string parameters can carry inline images or documents. A tiny
    # preparation fixture does not establish their real-world request size.
    # Reserve the host's bounded inline-data capacity without guessing from
    # parameter names or making the owner configure transport limits.
    return (MAX_REQUEST_BYTES if any(value.get("type") == "string"
        for value in tool.input_schema.get("properties", {}).values()) else DEFAULT_REQUEST_BYTES)


def descriptor_for(result, identity, draft):
    tool = result.specification.tool
    return PreparationDescriptorV2.model_validate({"schema_version": 2,
        "recipe": result.request.recipe.model_dump(),
        "package": {"id": "agent-adapter-" + draft["draft_id"], "version": "1.0.0", "name": draft["name"]},
        "components": [{"component": result.request.producer.model_dump(),
            "input": {"derivation_id": identity, "derivation_digest": result.digest}}],
        "tools": [tool.model_dump(exclude_none=True)], "artifacts": [], "inputs": {},
        "access": {}, "resources": {"memory_mb": 128, "cpu_only": True},
        "lifecycle": {"max_request_bytes": request_limit_for(tool), "max_sessions": 1,
            "idle_timeout_seconds": 300, "operation_timeout_seconds": 30},
        "validation": [{"id": "agent-adapter-fixture", "tool": tool.name, "arguments": result.specification.arguments,
            "expected": result.specification.expected, "synthetic": True}]})


class AgentPythonAdapterComponent:
    input_schema, legacy_interface_kinds = INPUT_SCHEMA, frozenset()

    def __init__(self, derivations):
        self.derivations = derivations
        self.reference = ComponentReference(id=COMPONENT_ID, version=VERSION,
            digest=component_catalog()[COMPONENT_ID + "@" + VERSION]["digest"])

    def result(self, descriptor, draft, *, full=True, check=lambda: None):
        value = selected_input(descriptor)
        result, path = self.derivations.resolve(value.derivation_id, value.derivation_digest, draft, full=full, check=check)
        if (descriptor.components[0].component != self.reference or result.request.producer != self.reference
                or descriptor.recipe != result.request.recipe or descriptor.tools != [result.specification.tool]
                or descriptor.inputs or descriptor.artifacts or descriptor.access.network or descriptor.access.filesystem
                or descriptor.access.secrets or descriptor.access.writes
                or not 64 <= descriptor.resources.memory_mb <= 4096 or not descriptor.resources.cpu_only):
            raise PreparationError("agent_adapter_descriptor_mismatch")
        return result, path

    def validate_source(self, descriptor, draft, recipe):
        result, _ = self.result(descriptor, draft)
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(item["path"], item["sha256"], item["size"]) for item in draft["source"]["files"]),
            result.specification.digest, result.specification.dependency_lock_sha256,
            canonical_bytes({"family": "agent-python-adapter", "derivation_digest": result.digest,
                "proposal_digest": result.request.proposal_digest, "attempt_id": result.request.attempt_id}))


class AgentPythonAdapterFamily:
    source_kind, target_profile = None, "mcp-stdio"

    def __init__(self, derivations):
        self.derivations = derivations
        self.component = AgentPythonAdapterComponent(derivations)
        from .preparation_agent_adapter_forms import AgentPythonAdapterFormProvider
        self.form_provider = AgentPythonAdapterFormProvider(self)

    def supports_source(self, draft):
        return draft["source"]["kind"] in {"local_folder", "application_directory"}

    def inspect_source(self, draft):
        candidates = []
        with self.derivations.imports._lock:
            rows = self.derivations.db.execute("SELECT data FROM agent_adapter_derivations WHERE json_extract(data,'$.request.import_id')=?",
                (draft["draft_id"],)).fetchall()
        import json
        for row in rows:
            item = json.loads(row[0])
            if item["state"] != "ready":
                continue
            try:
                result, _ = self.derivations.resolve(item["id"], item["result_digest"], draft)
                if result.request.producer != self.component.reference:
                    continue
                candidates.append({"candidate_id": result.digest, "kind": "agent-python-adapter",
                    "module": result.specification.module, "function": result.specification.function,
                    "unresolved": [], "tools": [result.specification.tool.model_dump(exclude_none=True)],
                    "_agent_derivation_id": item["id"], "_agent_derivation_digest": result.digest,
                    "verified": False})
            except (ValueError, KeyError):
                continue
        return {"schema_version": 1, "source_revision": draft["source"]["revision"], "roots": [],
            "observations": [], "execution_available": False, "verified_capabilities": [], "candidates": candidates}

    def freeze_agent_proposal(self, job, plan, proposal, draft, check=lambda: None):
        result = self.derivations.freeze(job, plan, proposal, self.component.reference, draft, check)
        identity = self.derivations._identity_for(result)
        return descriptor_for(result, identity, draft)

    def cancel_agent_proposal(self, job):
        with self.derivations.imports._lock:
            row = self.derivations.db.execute("SELECT id FROM agent_adapter_derivations WHERE job=?", (job["job_id"],)).fetchone()
        if row is not None:
            self.derivations.cancel(row[0])

    def selection_binding(self, descriptor, draft):
        result, _ = self.component.result(descriptor, draft)
        return result.digest, None

    def validate_inputs(self, descriptor, draft):
        return self.component.result(descriptor, draft)

    def check_current(self, descriptor, draft):
        # No byte hashing, provider I/O, writes or awaits inside the ordinary
        # authority transaction. Compare the same durably pinned baseline.
        self.component.result(descriptor, draft, full=False)
        return True

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import adapt_descriptor, bind_preparation_inputs, RuntimeReference
        result, _ = self.validate_inputs(descriptor, draft)
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        return bind_preparation_inputs(view, draft=draft, captured_collection=None,
            derived_collection=result.collection, derived_inputs=result.derived_inputs,
            runtimes=(RuntimeReference(name="private-python-runtime", digest=runtime_digest),), capabilities=("tools",),
            expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio"))

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        from .preparation_agent_adapter_builder import prepare_agent_adapter
        result, path = self.component.result(descriptor, draft, check=check)
        package = prepare_agent_adapter(descriptor, draft, result, path, toolchain, output, run, check,
            acquire_wheels=acquire_wheels)
        self.component.result(descriptor, draft, check=check)
        return package

    def release(self, job_id, descriptor):
        pass  # Immutable derived inputs remain durable until explicit deletion.

    async def verify(self, package_root, descriptor, reaper, workspace, *, timeout_seconds, owner):
        from .preparation_verifier import verify_prepared_package
        if "confine_private_python" not in inspect.signature(verify_prepared_package).parameters:
            raise PreparationError("preparation_probe_confinement_unavailable")
        value = selected_input(descriptor)
        item = self.derivations.get(value.derivation_id)
        request = item["request"]
        draft = self.derivations.imports.inspect(request["import_id"])
        async def validate():
            task = asyncio.create_task(asyncio.to_thread(self.component.result, descriptor, draft))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve the joined exception without publishing it.
                if not task.cancelled():
                    task.exception()
                raise
        await validate()
        report = await verify_prepared_package(package_root, descriptor, reaper, workspace,
            timeout_seconds=timeout_seconds, owner=owner, confine_private_python=True)
        await validate()
        return report
