"""Trusted loopback service family; original input is a connection, not a tree."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import Tool
from .preparation_components import ComponentReference, PreparationDescriptorV2, SourceBinding
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .preparation_service_contracts import ServiceExecutionPolicyV1, ServiceProbeGrantV1
from .preparation_service_source import checked_source

VERSION = "1.0.0"
IDS = ("openai-chat-completions-v1", "loopback-http-bearer-v1", "service-text-generate-v1")
EMPTY = {"type": "object", "additionalProperties": False}
FILES = ("preparation_service.py", "preparation_service_contracts.py", "preparation_service_source.py",
    "preparation_service_receipts.py", "preparation_service_builder.py", "preparation_service_verifier.py", "preparation_service_routes.py")


def component_catalog():
    root = Path(__file__).parent
    records = [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in FILES]
    records += [{"path": "mcpme_service/" + path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted((root.parent / "mcpme_service").glob("*.py"))]
    records += [{"path": "templates/service-connection-v1/" + name,
                 "sha256": hashlib.sha256((root.parent.parent / "templates/service-connection-v1" / name).read_bytes()).hexdigest()}
                for name in ("pyproject.toml", "uv.lock", "LICENSE")]
    return {name + "@" + VERSION: {"digest": contract_digest("service-component-v1", {"id": name, "files": records}),
        "input_schema": EMPTY, "dependencies": []} for name in IDS}


def references():
    catalog = component_catalog()
    return tuple(ComponentReference(id=name, version=VERSION, digest=catalog[name + "@" + VERSION]["digest"]) for name in IDS)


def validate_components(connection, *, selected=None):
    if (connection.protocol, connection.transport_auth, connection.task) != (references() if selected is None else selected):
        raise PreparationError("service_component_not_admitted")


def tools(connection):
    limits = connection.limits
    return [Tool.model_validate(item) for item in [
        {"name": "generate", "description": "Generate text using the selected local service.",
         "input_schema": {"type": "object", "additionalProperties": False, "required": ["prompt"], "properties": {
             "prompt": {"type": "string", "minLength": 1, "maxLength": limits.max_input_bytes},
             "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": limits.max_output_tokens}}},
         "output_schema": {"type": "object", "additionalProperties": False, "required": ["text", "finish_reason"],
             "properties": {"text": {"type": "string", "minLength": 1}, "finish_reason": {"enum": ["stop", "length"]}}}},
        {"name": "check_connection", "description": "Make one short synthetic request and check protocol compatibility; no quality assessment.",
         "input_schema": EMPTY, "output_schema": {"type": "object", "additionalProperties": False,
             "required": ["compatible"], "properties": {"compatible": {"const": True}}}},
    ]]


def probe_arguments(connection):
    # Even the minimum admitted one-byte input budget can run this exact probe.
    return {"prompt": "Hi"[:connection.limits.max_input_bytes], "max_output_tokens": min(16, connection.limits.max_output_tokens)}


def descriptor(draft, recipe, *, name, memory_mb, selected=None):
    connection = checked_source(draft).connection
    selected = references() if selected is None else selected
    validate_components(connection, selected=selected)
    return PreparationDescriptorV2.model_validate({"schema_version": 2,
        "recipe": {"id": recipe["manifest"]["id"], "version": recipe["manifest"]["version"], "digest": recipe["digest"]},
        "package": {"id": "service-" + draft["draft_id"], "version": "1.0.0", "name": name},
        "components": [{"component": ref.model_dump(), "input": {}} for ref in selected],
        "tools": [tool.model_dump(exclude_none=True) for tool in tools(connection)], "artifacts": [], "inputs": {},
        "access": {"network": [connection.endpoint.origin], "secrets": ["api_key"], "writes": False},
        "resources": {"memory_mb": memory_mb},
        "lifecycle": {"max_sessions": 1, "operation_timeout_seconds": connection.limits.timeout_seconds + 5},
        "validation": [{"synthetic": True, "id": "service-protocol", "tool": "check_connection", "arguments": {}, "expected": {"compatible": True}}]})


def validate_descriptor(value, draft, *, selected=None):
    selected = references() if selected is None else selected
    source = checked_source(draft); validate_components(source.connection, selected=selected)
    if not isinstance(value, PreparationDescriptorV2):
        raise PreparationError("service_descriptor_required")
    expected = descriptor(draft, {"manifest": {"id": value.recipe.id, "version": value.recipe.version}, "digest": value.recipe.digest},
                          name=value.package.name, memory_mb=value.resources.memory_mb, selected=selected)
    if value != expected or not 128 <= value.resources.memory_mb <= 4096:
        raise PreparationError("service_descriptor_mismatch")
    return source.connection


class ServiceComponent:
    input_schema = EMPTY
    legacy_interface_kinds = frozenset()

    def __init__(self, reference):
        self.reference = reference

    def validate_source(self, descriptor, draft, recipe):
        connection = validate_descriptor(descriptor, draft)
        return SourceBinding(*self.reference.key, connection.revision, (),
            contract_digest("service-interface-v1", [item.model_dump(exclude_none=True) for item in tools(connection)]), None,
            canonical_bytes({"connection_digest": connection.digest}))


class ServicePreparationFamily:
    source_kind = "service_connection"
    target_profile = "mcp-stdio"

    def __init__(self, secrets):
        self.secrets = secrets
        self.components = tuple(ServiceComponent(ref) for ref in references())
        self.component = self.components[-1]
        self.form_provider = ServiceFormProvider()

    def validate_inputs(self, value, draft):
        connection = validate_descriptor(value, draft, selected=tuple(c.reference for c in self.components))
        if self.secrets.get(connection.secret_references.api_key)["status"] != "active":
            raise PreparationError("service_secret_unavailable")
        return connection

    def check_current(self, value, draft):
        self.validate_inputs(value, draft)
        return True

    def execution_policy(self, value, draft, supplied=None):
        connection = self.validate_inputs(value, draft)
        from .preparation_runner import WHEEL_HOSTS
        grant = ServiceProbeGrantV1.model_validate({"schema_version": 1, "source_revision": connection.revision,
            "descriptor_digest": value.digest, **{key: getattr(connection, key).model_dump() for key in ("protocol", "transport_auth", "task")},
            "endpoint": connection.endpoint.model_dump(), "method": "POST", "path": "/v1/chat/completions", "auth_role": "api_key",
            "secret_reference": connection.secret_references.api_key,
            "fixture_digest": contract_digest("service-probe-fixture-v1", probe_arguments(connection)), "max_calls": 1,
            "request_bytes": 32768, "response_bytes": connection.limits.max_response_bytes, "timeout_seconds": connection.limits.timeout_seconds})
        if supplied is not None:
            from .preparation_service_contracts import parse_policy
            policy = parse_policy(supplied)
            if type(policy) is not ServiceExecutionPolicyV1 or policy.service_probe != grant:
                raise PreparationError("service_probe_authority_mismatch")
            return policy
        return ServiceExecutionPolicyV1.model_validate({"schema_version": 2, "operations": ["build", "probe"],
            "network": sorted(WHEEL_HOSTS), "wall_seconds": 600, "disk_bytes": 4 * 1024**3,
            "memory_mb": max(2048, value.resources.memory_mb), "service_probe": grant.model_dump()})

    def bound_inputs(self, value, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import SourceValidationReference, candidate_contract_digest
        from .preparation_service_receipts import ServiceBoundInputsV1
        from .preparations import PreparationStore
        connection = self.validate_inputs(value, draft)
        return ServiceBoundInputsV1.model_validate({"schema_version": 2,
            "source": {"kind": "service_connection", "schema_version": 1, "revision": connection.revision},
            "descriptor_digest": value.digest, "recipe": value.recipe.model_dump(), "package": value.package.model_dump(),
            "components": [ref.model_dump() for ref in references()],
            "source_validations": [SourceValidationReference.from_binding(binding).model_dump(mode="json")
                for binding in registry.validate_source(value, draft, recipe)],
            "runtimes": [{"name": "private-python-toolchain", "digest": runtime_digest}],
            "portable_configuration_digest": contract_digest("service-portable-configuration-v1", connection.portable()),
            "derivation_digest": contract_digest("service-generated-adapter-v1", {"source_revision": connection.revision,
                "components": [ref.model_dump() for ref in references()], "configuration": connection.portable()}),
            "expected_package_contract_digest": candidate_contract_digest(PreparationStore._candidate_contract(value, connection.revision))})

    def inspect_source(self, draft):
        connection = checked_source(draft).connection; validate_components(connection)
        return {"schema_version": 1, "source_revision": connection.revision, "blockers": [], "candidates": [{
            "candidate_id": contract_digest("service-candidate-v1", {"source_revision": connection.revision}),
            "kind": "service-connection", "module": "Local text service", "unresolved": [],
            "tools": [item.model_dump(exclude_none=True) for item in tools(connection)]}]}

    def prepare(self, value, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        from .preparation_service_builder import prepare_service
        self.validate_inputs(value, draft)
        return prepare_service(value, draft, toolchain, output, run, check, acquire_wheels=acquire_wheels)

    async def verify(self, package_root, value, reaper, workspace, *, timeout_seconds=60, owner=None):
        from .preparation_service_verifier import verify_service
        return await verify_service(self, package_root, value, reaper, workspace, timeout_seconds=timeout_seconds, owner=owner)

    def release(self, job_id, descriptor=None):
        pass


class ServiceFormProvider:
    def accepts(self, candidate, recipe):
        return candidate["kind"] == "service-connection" and {
            (step["component"], step["version"], step["digest"]) for step in recipe["manifest"]["steps"]} == {ref.key for ref in references()}

    def fields(self, draft, candidate, recipe):
        from .preparation_forms import _field
        field = _field("service-name", label="App name", kind="string", value=draft["name"],
                       constraints={"min_length": 1, "max_length": 256})
        return [field], {field.id: ("name",)}, []

    def resolve(self, context, values):
        field = next(iter(context.bindings))
        return descriptor(context.draft, context.recipe_record, name=values[field], memory_mb=256).model_dump(exclude_none=True)

    def restore_fields(self, fields, bindings, value):
        parsed = PreparationDescriptorV2.model_validate(value)
        return [field.model_copy(update={"value": parsed.package.name}) for field in fields]
