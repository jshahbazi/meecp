"""A separately connected, scoped, tools-only management MCP facade.

This transport never retrieves slot invocation credentials, opens an app, or
starts a slot during initialization. The manager checks authority on each call.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Callable

import anyio
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult, JSONRPCMessage, ServerCapabilities, TextContent, Tool, ToolsCapability
from mcp.types import InitializeRequestParams

from .gateway import MAX_MESSAGE_BYTES, MAX_PENDING, SESSION_INSTRUCTIONS, encode
from .grants import GrantError
from .policy import load_json
from .authorization_client import AuthorizationError, SessionAuthorization

UUID_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
PATH_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 4096}
REVISION_SCHEMA = {"type": "integer", "minimum": 1}
NAME_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 256}
INTENT_SCHEMA = {"type": "string", "maxLength": 16384}
DIGEST_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
RECIPE_ID_SCHEMA = {"type": "string", "pattern": "^[a-z0-9]+(?:[.-][a-z0-9]+)+$", "maxLength": 128}
VERSION_SCHEMA = {"type": "string", "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?$", "maxLength": 64}
START_POLICY_SCHEMA = {"type": "string", "enum": ["manual", "host"]}
DOCUMENT_ROLE_SCHEMA = {"type": "string", "enum": ["instructions", "reference"]}
LABELS_SCHEMA = {"type": "array", "maxItems": 128, "uniqueItems": True,
                 "items": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"}}
REQUIREMENTS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "tools": LABELS_SCHEMA, "secret_references": LABELS_SCHEMA,
    "profile": {"type": "string", "enum": ["mcp-stdio", "model-service"]},
    "memory_mb": {"type": "integer", "minimum": 1, "maximum": 262144}}}
SETTING_NAME_SCHEMA = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}
SETTINGS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["configuration", "secret_references"],
    "properties": {"schema_version": {"type": "integer", "const": 1},
        "configuration": {"type": "object", "maxProperties": 32, "propertyNames": SETTING_NAME_SCHEMA,
            "additionalProperties": {"anyOf": [{"type": "boolean"},
                {"type": "integer", "minimum": -(2 ** 53), "maximum": 2 ** 53},
                {"type": "string", "maxLength": 4096}]}},
        "secret_references": {"type": "object", "maxProperties": 16, "propertyNames": SETTING_NAME_SCHEMA,
            "additionalProperties": UUID_SCHEMA}}}

# Dependencies and validators can include request content in their exceptions.
# Only these documented metadata codes can cross the error boundary.
SAFE_ERROR_CODES = frozenset({
    "unauthorized", "forbidden", "grant_expired", "grant_revoked", "grant_scope_denied",
    "grant_scope", "grant_plan_changed", "grant_limit", "grant_credential_unavailable", "grants_unavailable",
    "grant_not_found", "grant_unavailable", "grant_operation_mismatch", "grant_plan_mismatch",
    "invalid_grant", "invalid_credential", "unsafe_private_state", "plan_not_found", "plan_stale",
    "invalid_plan", "invalid_install_plan", "invalid_import_plan", "import_not_found", "stale_import_revision",
    "invalid_import_identity", "invalid_import_revision", "invalid_import_request", "invalid_import_state",
    "import_submitted", "import_expired", "import_canceled", "imports_unavailable", "import_not_ready",
    "source_unresolved", "source_changed", "source_credentials_detected", "source_size_exceeded",
    "source_file_limit", "unsafe_source_path", "unsafe_source_file", "unsupported_source_file",
    "invalid_import_intent", "invalid_document_role", "import_credentials_detected", "invalid_slot_name",
    "invalid_import_requirements", "tool_contract_required", "tool_contract_mismatch",
    "engine_incompatible", "secret_reference_unavailable", "unsupported_process_topology",
    "unsupported_profile", "unsupported_capability", "package_changed", "package_digest_mismatch",
    "invalid_package", "slot_not_found", "operation_not_found", "invalid_control", "resource_blocked",
    "port_conflict", "ownership_lost", "incompatible", "host_unavailable", "body_limit", "response_limit",
    "invalid_request", "service_error", "plan_expired", "import_revision_mismatch", "import_plan_invalidated",
    "draft_inactive", "draft_finalized", "operation_in_progress", "tool_schema_mismatch",
    "installation_recovery_required", "draft_revision_mismatch", "import_metadata_limit", "source_file_excluded",
    "invalid_source_selection", "intent_required", "prepared_package_required", "unsupported_import_source",
    "artifact_hash_mismatch", "artifact_mode_mismatch", "artifact_size_mismatch", "artifact_tree_mismatch",
    "duplicate_artifact", "duplicate_tool", "entrypoint_not_executable", "external_schema_reference",
    "invalid_artifact_path", "invalid_entrypoint", "invalid_model_profile", "invalid_package_json",
    "invalid_package_schema", "invalid_process_graph", "invalid_resource_declaration", "invalid_tool_schema",
    "license_required", "manifest_hash_mismatch", "model_identity_mismatch", "package_lock_mismatch",
    "package_size_exceeded", "unsafe_package_file", "unsafe_package_path", "unsupported_execution_profile",
    "unsupported_package_schema", "import_binding_mismatch", "import_cleanup_failed", "import_limit_exceeded",
    "import_operation_mismatch", "import_revision_required", "invalid_import_update", "invalid_start_policy",
    "source_identity_mismatch", "supported_source_required", "unsupported_import_schema", "unsupported_source",
    "generation_required", "invalid_generation", "invalid_identity", "operation_conflict", "operation_finalized",
    "ownership_reconciliation_required", "registry_unavailable", "slot_conflict", "slot_not_stopped", "stale_generation",
    "unsupported_registry_schema", "candidate_validation_failed", "capacity_exceeded", "gateway_failed",
    "invalid_resource_policy", "invalid_resource_reservation", "invalid_slot_identity", "migration_recovery_required",
    "package_required", "resource_policy_required", "runtime_authentication_failed", "runtime_config_limit",
    "runtime_identity_mismatch", "runtime_start_failed", "session_limit", "session_termination_failed",
    "session_termination_unverified", "slot_stopped", "unsupported_mcp_capabilities", "unsupported_platform",
    "unsupported_start_policy",
    "invalid_recipe_request", "invalid_recipe_identity", "recipe_not_found", "recipe_disabled",
    "recipe_revoked", "recipe_removed", "recipe_changed", "recipe_digest_mismatch", "recipe_evidence_stale",
    "recipe_in_use", "recipe_limit", "recipe_conflict", "recipe_size_limit", "unsafe_recipe_path",
    "unsafe_recipe_file", "invalid_recipe_schema", "invalid_recipe_lock", "recipe_metadata_required",
    "recipe_manifest_hash_mismatch", "recipe_artifact_tree_mismatch", "recipe_artifact_hash_mismatch",
    "recipe_license_required", "recipe_component_unavailable", "recipe_component_digest_mismatch",
    "recipe_dependency_cycle", "recipe_incompatible", "invalid_preparation_request", "preparation_not_found",
    "invalid_preparation_identity", "preparation_runner_unavailable", "preparation_plan_not_found",
    "preparation_plan_mismatch", "preparation_plan_consumed", "preparation_authority_expired",
    "preparation_canceled", "preparation_in_progress", "preparation_key_conflict", "preparation_limit",
    "invalid_preparation_policy", "preparation_policy_mismatch", "preparation_descriptor_required",
    "preparation_form_stale", "preparation_selection_required", "invalid_resolution_request",
    "invalid_resolution_policy", "resolution_runner_unavailable", "resolution_plan_not_found",
    "resolution_plan_mismatch", "resolution_authority_expired", "resolution_canceled",
    "resolution_idempotency_conflict", "resolution_not_found", "invalid_resolution_identity",
    "grant_scope_changed", "management_execution_authority_invalid", "management_execution_revoked",
    "management_execution_expired", "recipe_unavailable", "recipe_version_collision", "invalid_preparation_form",
    "preparation_form_mismatch", "preparation_answer_invalid", "preparation_candidate_unavailable",
    "authorization_required", "authorization_pending", "authorization_expired", "authorization_revoked",
    "authorization_denied", "authorization_unavailable",
    "invalid_upgrade_request", "incompatible_upgrade", "invalid_upgrade_identity", "invalid_upgrade_update",
    "invalid_upgrade_transition", "invalid_generation_reference", "generation_data_missing", "upgrade_not_found",
    "upgrade_limit", "upgrade_canceled", "upgrade_already_exposed", "upgrade_data_unsafe", "upgrade_data_changed",
    "upgrade_data_limit", "upgrade_snapshot_unavailable", "upgrade_snapshot_exists", "upgrade_disk_unavailable",
    "upgrade_identity_conflict", "upgrade_generation_mismatch", "upgrade_recovery_required", "upgrades_unavailable",
    "operation_cancel_unavailable", "operation_recovery_unavailable",
    "invalid_settings", "configuration_required", "invalid_secret_reference", "secrets_unavailable",
})


def schema(properties=None, required=(), **extra):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False, **extra}


@dataclass(frozen=True)
class Operation:
    tool: Tool
    method: str
    route: str
    body: Callable[[dict], dict | None]


def operations() -> dict[str, Operation]:
    result = {}

    def add(name, description, method, route, input_schema=None, body=lambda _: None):
        result[name] = Operation(Tool(name=name, description=description,
            inputSchema=input_schema or schema()), method, route, body)

    add("mcpme_capabilities", "Read supported host operations and preparation templates.", "GET", "/v1/capabilities")
    add("mcpme_list_recipes", "Read registered immutable recipe versions and state.", "GET", "/v1/recipes")
    add("mcpme_get_recipe_schema", "Read the versioned recipe manifest and lock schemas.", "GET", "/v1/recipes/schema")
    recipe_identity = {"recipe_id": RECIPE_ID_SCHEMA, "version": VERSION_SCHEMA}
    add("mcpme_get_recipe", "Inspect a registered recipe version and its static compatibility evidence.",
        "POST", "/v1/recipes/inspect", schema(recipe_identity, ("recipe_id", "version")), lambda args: args)
    add("mcpme_register_recipe", "Register the exact bundle allowed by an owner-issued recipe management grant; never execute bundle code.",
        "POST", "/v1/recipes", schema({"path": PATH_SCHEMA}, ("path",)), lambda args: args)
    for action in ("enable", "disable", "deprecate", "revoke", "remove"):
        add("mcpme_" + action + "_recipe", f"{action.capitalize()} the exact recipe version allowed by a recipe management grant.",
            "POST", "/v1/recipes/" + action, schema(recipe_identity, ("recipe_id", "version")), lambda args: args)
    draft_revision = {"import_id": UUID_SCHEMA, "revision": REVISION_SCHEMA}
    recipe_reference = schema({"id": RECIPE_ID_SCHEMA, "version": VERSION_SCHEMA, "digest": DIGEST_SCHEMA},
                              ("id", "version", "digest"))
    add("mcpme_inspect_preparation_source", "Inspect the exact draft source without executing it.",
        "POST", "/v1/preparations/inspect-source", schema(draft_revision, ("import_id", "revision")), lambda args: args)
    add("mcpme_get_preparation_form", "Read focused questions for the exact current candidate and recipe.",
        "POST", "/v1/preparations/form", schema({**draft_revision, "candidate_id": DIGEST_SCHEMA,
            "recipe": recipe_reference, "resolution_digest": DIGEST_SCHEMA}, ("import_id", "revision")), lambda args: args)
    add("mcpme_plan_preparation", "Review an exact descriptor and optional bounded execution policy without starting a job.",
        "POST", "/v1/preparations/plan", schema({**draft_revision, "descriptor": {"type": "object"},
            "execution_policy": {"type": "object"}}, ("import_id", "revision", "descriptor")), lambda args: args)
    add("mcpme_plan_preparation_form", "Bind exact form answers to a draft revision and review its preparation plan; no execution.",
        "POST", "/v1/preparations/plan-form", schema({**draft_revision, "candidate_id": DIGEST_SCHEMA,
            "recipe": recipe_reference, "form_digest": DIGEST_SCHEMA, "answers": {"type": "object"},
            "execution_policy": {"type": "object"}, "resolution_digest": DIGEST_SCHEMA},
            ("import_id", "revision", "candidate_id", "recipe", "form_digest", "answers")), lambda args: args)
    add("mcpme_plan_resolution", "Review locked dependency/interface resolution for an exact candidate without starting work.",
        "POST", "/v1/resolutions/plan", schema({**draft_revision, "candidate_id": DIGEST_SCHEMA,
            "policy": {"type": "object"}}, ("import_id", "revision", "candidate_id", "policy")), lambda args: args)
    for noun, collection in (("preparation", "preparations"), ("resolution", "resolutions")):
        add("mcpme_start_" + noun, f"Start only the exact {noun} plan and attempt bound to an owner-issued execution grant.",
            "POST", "/v1/" + collection + "/start", schema({"plan_id": UUID_SCHEMA, "plan_digest": DIGEST_SCHEMA,
                "idempotency_key": UUID_SCHEMA}, ("plan_id", "plan_digest", "idempotency_key")), lambda args: args)
        add("mcpme_list_" + collection, f"Read visible {noun} jobs without adopting or restarting work.", "GET", "/v1/" + collection)
        add("mcpme_get_" + noun, f"Read one visible {noun} job and bounded progress.", "GET", "/v1/" + collection + "/{job_id}",
            schema({"job_id": UUID_SCHEMA}, ("job_id",)))
        add("mcpme_cancel_" + noun, f"Cancel only the exact {noun} job bound to this execution grant; owned work must settle.",
            "POST", "/v1/" + collection + "/{job_id}/cancel", schema({"job_id": UUID_SCHEMA}, ("job_id",)), lambda _: {})
    add("mcpme_list_imports", "List resumable import draft summaries.", "GET", "/v1/imports")
    add("mcpme_create_import", "Snapshot selected local source into a resumable draft without executing it.",
        "POST", "/v1/imports", schema({"source": PATH_SCHEMA, "name": NAME_SCHEMA, "intent": INTENT_SCHEMA,
            "document_role": DOCUMENT_ROLE_SCHEMA, "start": {"type": "boolean"}, "start_policy": START_POLICY_SCHEMA,
            "files": {"type": "array", "maxItems": 2048, "uniqueItems": True, "items": PATH_SCHEMA}},
            ("source", "name")), lambda args: {**{"intent": "", "start": False, "start_policy": "manual"}, **args})
    add("mcpme_inspect_import", "Inspect the current draft revision and blockers.", "GET", "/v1/imports/{draft_id}",
        schema({"draft_id": UUID_SCHEMA}, ("draft_id",)))
    revision = {"draft_id": UUID_SCHEMA, "revision": REVISION_SCHEMA}
    without_id = lambda args: {key: value for key, value in args.items() if key != "draft_id"}
    add("mcpme_revise_import", "Revise a draft using its exact current revision; attached artifacts become stale.",
        "POST", "/v1/imports/{draft_id}/revise", schema({**revision, "name": NAME_SCHEMA, "intent": INTENT_SCHEMA,
            "document_role": DOCUMENT_ROLE_SCHEMA, "start": {"type": "boolean"}, "start_policy": START_POLICY_SCHEMA,
            "requirements": REQUIREMENTS_SCHEMA}, ("draft_id", "revision"), minProperties=3), without_id)
    for action in ("export", "cancel", "expire"):
        add("mcpme_" + action + "_import", f"{action.capitalize()} the exact current draft revision.",
            "POST", "/v1/imports/{draft_id}/" + action, schema(revision, ("draft_id", "revision")), without_id)
    add("mcpme_attach_import_package", "Verify a prepared package against the draft source and exact revision.",
        "POST", "/v1/imports/{draft_id}/attach", schema({**revision, "package": PATH_SCHEMA},
            ("draft_id", "revision", "package")), without_id)
    add("mcpme_list_slots", "List managed slots visible to this grant.", "GET", "/v1/slots")
    add("mcpme_inspect_slot", "Inspect one slot visible to this grant.", "GET", "/v1/slots/{slot_id}",
        schema({"slot_id": UUID_SCHEMA}, ("slot_id",)))
    plan_schema = schema({"package": PATH_SCHEMA, "import_id": UUID_SCHEMA, "revision": REVISION_SCHEMA,
            "name": NAME_SCHEMA, "start_policy": START_POLICY_SCHEMA, "start": {"type": "boolean"}}, oneOf=[
        {"required": ["package"], "not": {"anyOf": [{"required": ["import_id"]}, {"required": ["revision"]}]}},
        {"required": ["import_id", "revision"], "not": {"anyOf": [{"required": [key]} for key in
            ("package", "name", "start_policy", "start")]}}])
    add("mcpme_plan_install", "Create a reviewable immutable plan. Planning does not execute package code.",
        "POST", "/v1/install/plan", plan_schema, lambda args: args if "import_id" in args else
        {**{"start_policy": "manual", "start": False}, **args})
    add("mcpme_inspect_install_plan", "Read the saved review summary of an exact installation plan.",
        "GET", "/v1/plans/{plan_id}", schema({"plan_id": UUID_SCHEMA}, ("plan_id",)))
    add("mcpme_apply_install", "Apply the exact granted plan; retries return its one bound operation.",
        "POST", "/v1/install/apply", schema({"plan_id": UUID_SCHEMA}, ("plan_id",)), lambda args: args)
    add("mcpme_plan_upgrade", "Create an immutable upgrade plan for an existing app entry without executing package code. Omit settings to preserve current settings; supplied settings contain configuration values and existing opaque secret references. Applying preserves the entry identity and validates a private app-data clone.",
        "POST", "/v1/upgrade/plan", schema({"slot_id": UUID_SCHEMA, "package": PATH_SCHEMA,
            "settings": SETTINGS_SCHEMA}, ("slot_id", "package")), lambda args: args)
    add("mcpme_inspect_upgrade_plan", "Read the saved review summary of an exact upgrade plan for an existing app entry.",
        "GET", "/v1/upgrade/plans/{plan_id}", schema({"plan_id": UUID_SCHEMA}, ("plan_id",)))
    add("mcpme_apply_upgrade", "Apply only the exact separately approved upgrade plan; retries return its one bound operation. Upgrade validation runs against a private data clone before the new generation becomes available.",
        "POST", "/v1/upgrade/apply", schema({"plan_id": UUID_SCHEMA}, ("plan_id",)), lambda args: args)
    add("mcpme_get_operation", "Read a visible operation's bounded progress and result.", "GET", "/v1/operations/{operation_id}",
        schema({"operation_id": UUID_SCHEMA}, ("operation_id",)))
    for action in ("start", "stop", "restart"):
        add("mcpme_" + action + "_slot", f"{action.capitalize()} one slot allowed by this lifecycle grant.",
            "POST", "/v1/slots/{slot_id}/" + action, schema({"slot_id": UUID_SCHEMA}, ("slot_id",)), lambda _: {})
    return result


def authorization_tools():
    action = schema({"kind": {"type": "string", "enum": ["preparation", "resolution", "install", "upgrade"]},
        "plan_id": UUID_SCHEMA, "plan_digest": DIGEST_SCHEMA, "idempotency_key": UUID_SCHEMA},
        ("kind", "plan_id", "plan_digest"), allOf=[{"if": {"properties": {"kind": {
            "enum": ["preparation", "resolution"]}}}, "then": {"required": ["idempotency_key"]}},
            {"if": {"properties": {"kind": {"enum": ["install", "upgrade"]}}},
             "then": {"not": {"required": ["idempotency_key"]}}}])
    return {
        "mcpme_request_access": Tool(name="mcpme_request_access",
            description="Request native owner consent for scoped management access or an exact saved plan. Returns promptly; poll authorization status, then explicitly call the intended operation.",
            inputSchema=schema({"scopes": {"type": "array", "minItems": 1, "maxItems": 2,
                "uniqueItems": True, "contains": {"const": "inspect"},
                "items": {"type": "string", "enum": ["inspect", "plan"]}},
                "action": action}, **{"not": {"required": ["scopes", "action"]}})),
        "mcpme_get_authorization_status": Tool(name="mcpme_get_authorization_status",
            description="Read only this live connection's authorization status. No manager data or credentials are returned.",
            inputSchema=schema()),
    }


def tool_result(value: dict, *, error=False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, allow_nan=False, separators=(",", ":")))],
                          structuredContent=value, isError=error)


def failure(code: str) -> CallToolResult:
    return tool_result({"api_version": 1, "error": {"code": code}}, error=True)


class Management:
    def __init__(self, state: Path, credential_file: Path | None = None, *, caller=None, profile=None, authorization=None):
        if caller is None:
            from .__main__ import call
            caller = call
        self.state, self.credential_file, self.caller = state, credential_file, caller
        self.authorization = authorization if authorization is not None else (
            SessionAuthorization(state, profile) if credential_file is None else None)
        self.operations = operations()
        self.local_tools = authorization_tools()
        self.validators = {name: Draft202012Validator(operation.tool.inputSchema)
                           for name, operation in self.operations.items()}
        self.validators.update({name: Draft202012Validator(tool.inputSchema) for name, tool in self.local_tools.items()})

    def initialized(self, client):
        if self.authorization is not None:
            self.authorization.on_initialized(client)

    async def close(self):
        if self.authorization is not None:
            await self.authorization.close()

    async def invoke(self, name: str, arguments: dict | None) -> CallToolResult:
        operation = self.operations.get(name)
        if operation is None and name not in self.local_tools:
            return failure("unknown_tool")
        try:
            if not isinstance(arguments, dict) or not self.validators[name].is_valid(arguments):
                return failure("invalid_tool_arguments")
            if name in self.local_tools:
                if self.authorization is None:
                    return tool_result({"api_version": 1, "status": "approved", "mode": "credential_file"}) if name == "mcpme_get_authorization_status" else failure("authorization_unavailable")
                if name == "mcpme_get_authorization_status":
                    return tool_result(self.authorization.status())
                return tool_result(await self.authorization.request_access(arguments))
            async with asyncio.timeout(330):
                if self.authorization is not None:
                    value = await self.authorization.invoke(name, arguments, operation.method,
                        operation.route.format(**arguments), operation.body(arguments))
                else:
                    value = await self.caller(self.state, operation.method, operation.route.format(**arguments),
                        operation.body(arguments), credential_file=self.credential_file)
            if not isinstance(value, dict):
                return failure("management_request_failed")
            if "error" in value:
                code = value["error"].get("code") if isinstance(value["error"], dict) else None
                return failure(code if isinstance(code, str) and code in SAFE_ERROR_CODES else "management_request_failed")
            result = tool_result(value)
            if len(encode(result.model_dump(by_alias=True, exclude_none=True))) > MAX_MESSAGE_BYTES - 8192:
                # Include both the structured result and its escaped text copy.
                return failure("response_limit")
            return result
        except asyncio.CancelledError:
            raise
        except AuthorizationError as error:
            return tool_result({"api_version": 1, "error": {"code": error.code}, **error.status}, error=True)
        except GrantError as error:
            return failure(error.code if error.code in SAFE_ERROR_CODES else "management_request_failed")
        except Exception:
            return failure("host_unavailable")

    def server(self) -> Server:
        server = Server("mcpme-management", version="1")

        @server.list_tools()
        async def list_tools():
            return [operation.tool for operation in self.operations.values()] + list(self.local_tools.values())

        # The SDK's default validator includes caller arguments in error text.
        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            return await self.invoke(name, arguments)

        return server


async def serve_streams(management: Management, reader, writer):
    """Bound frames, queued messages, active requests, output and write deadlines."""
    pending = set()
    read_send, read_receive = anyio.create_memory_object_stream(1)
    write_send, write_receive = anyio.create_memory_object_stream(1)
    write_lock = asyncio.Lock()
    initialize_id = None
    initialize_client = None
    initialize_accepted = False

    async def emit(value):
        data = encode(value) + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("response_limit")
        async with write_lock, asyncio.timeout(30):
            await writer(data)

    async def input_task():
        nonlocal initialize_id, initialize_client
        async with read_send:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    return
                if not line:
                    return
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    return
                try:
                    value = load_json(line)
                    if "id" in value and type(value["id"]) not in {int, str}:
                        return
                    message = JSONRPCMessage.model_validate(value)
                except Exception:
                    return
                if value.get("method") == "initialize" and initialize_id is None:
                    try:
                        params = InitializeRequestParams.model_validate(value.get("params"))
                        initialize_id = value.get("id")
                        initialize_client = params.clientInfo.model_dump(include={"name", "version"})
                    except Exception:
                        pass
                if value.get("method") == "notifications/initialized" and initialize_accepted:
                    management.initialized(initialize_client or {})
                if "id" in value and "method" in value:
                    key = (type(value["id"]), value["id"])
                    if key in pending:
                        return
                    if len(pending) >= MAX_PENDING:
                        await emit({"jsonrpc": "2.0", "id": value["id"], "error": {
                            "code": -32000, "message": "The management request limit was reached."}})
                        continue
                    pending.add(key)
                await read_send.send(SessionMessage(message))

    async def output_task():
        nonlocal initialize_accepted
        async with write_receive:
            async for item in write_receive:
                value = item.message.model_dump(by_alias=True, exclude_none=True)
                if "error" in value:
                    # The SDK can stringify exceptions outside our tool handler.
                    value["error"] = {"code": -32000, "message": "The management request could not be completed."}
                if value.get("id") == initialize_id and "result" in value and initialize_client is not None:
                    initialize_accepted = True
                await emit(value)
                if "id" in value and "method" not in value:
                    pending.discard((type(value["id"]), value["id"]))

    server = management.server()
    options = InitializationOptions(server_name="mcpme-management", server_version="1",
        capabilities=ServerCapabilities(tools=ToolsCapability(listChanged=False)), instructions=SESSION_INSTRUCTIONS)

    async def run_server():
        try:
            async with read_receive, write_send:
                await server.run(read_receive, write_send, options)
        finally:
            with anyio.CancelScope(shield=True):
                await management.close()
            # A closed SDK session must also stop a blocked stdout writer.
            # Durable manager operations live in the supervisor independently.
            group.cancel_scope.cancel()

    async with anyio.create_task_group() as group:
        group.start_soon(input_task)
        group.start_soon(output_task)
        group.start_soon(run_server)


async def run_management(state: Path, credential_file: Path | None = None, *, profile=None):
    """Use the SDK with a private local API and terminate cleanly on EOF/signals."""
    from .grants import read_credential
    if credential_file is not None:
        read_credential(credential_file)
    logging.disable(logging.CRITICAL)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    stdout_fd = sys.stdout.fileno()
    os.set_blocking(stdout_fd, False)

    async def write(data):
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(stdout_fd, view):]
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_writer(stdout_fd, lambda: None if ready.done() else ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_writer(stdout_fd)

    task = asyncio.current_task()
    handlers = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
            handlers.append(sig)
    try:
        await serve_streams(Management(state, credential_file, profile=profile), reader, write)
    except asyncio.CancelledError:
        pass
    finally:
        transport.close()
        for sig in handlers:
            loop.remove_signal_handler(sig)
