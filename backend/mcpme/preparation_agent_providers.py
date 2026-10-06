"""Owner-entered provider configuration; no discovery, dispatch or gate changes.

The Supervisor owns the state lock. SecretStore keeps immutable values in this
dedicated private namespace. Registry identities and digests contain only opaque
generations, references and nonsecret configuration. Adapters are trusted code.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import threading
from typing import Annotated, Literal, Protocol
import uuid

from pydantic import Field, SecretStr, model_validator

from .contracts import StrictModel
from .preparation_agent_backend import ENDPOINT, OpenAIResponsesBackend
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .private import ensure_private_directory, read_private_json, write_private_json
from .settings import SecretStore

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
MAX_PROVIDERS = 16
MAX_RECORDS = 128
MAX_REGISTRY_BYTES = 512 * 1024
ERRORS = frozenset({"agent_provider_request_rejected", "agent_provider_configuration_stale",
    "agent_provider_not_found", "agent_provider_limit", "agent_provider_unavailable",
    "agent_provider_adapter_unavailable", "agent_provider_owner_required"})


class AgentProviderError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "agent_provider_request_rejected"
        super().__init__(self.code)


class VersionedProviderInput(StrictModel):
    schema_version: Literal[1]

    @model_validator(mode="before")
    @classmethod
    def integer_version(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("agent_provider_request_rejected")
        return value


class ProviderChoices(VersionedProviderInput):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    models: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    configuration: dict
    credentials: Annotated[dict[str, SecretStr], Field(max_length=8)]

    @model_validator(mode="after")
    def closed(self):
        if (type(self.schema_version) is not int or self.name != self.name.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.name)
                or len(self.models) != len(set(self.models))):
            raise ValueError("agent_provider_request_rejected")
        # Detect accidental credentials in nonsecret choices, never hash keys.
        canonical_bytes({"name": self.name, "models": self.models, "configuration": self.configuration})
        return self


class CreateProvider(ProviderChoices):
    adapter_id: Identifier


class UpdateProvider(ProviderChoices):
    expected_generation: UUID


class RemoveProvider(VersionedProviderInput):
    expected_generation: UUID


class ProviderRecord(StrictModel):
    provider_id: UUID
    adapter_id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=80)]
    models: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    configuration: dict
    generation: UUID
    credential_generation: UUID
    credential_references: Annotated[dict[str, UUID], Field(min_length=1, max_length=8)]
    status: Literal["active", "removed"]


class ProviderJournal(VersionedProviderInput):
    providers: Annotated[list[ProviderRecord], Field(max_length=MAX_RECORDS)]

    @model_validator(mode="after")
    def identities(self):
        if (type(self.schema_version) is not int
                or len({record.provider_id for record in self.providers}) != len(self.providers)
                or sum(record.status == "active" for record in self.providers) > MAX_PROVIDERS):
            raise ValueError("agent_provider_unavailable")
        return self


class TrustedProviderAdapter(Protocol):
    """Only host registration supplies destinations, schemas and backend code."""
    adapter_id: str
    name: str
    destination: str
    configuration_schema: dict
    credential_fields: list[dict]
    digest: str

    def validate_configuration(self, value: dict) -> None: ...
    def validate_credentials(self, value: dict[str, str]) -> None: ...
    def backend(self, resolve, configuration: dict): ...


class OpenAIResponsesAdapter:
    adapter_id = "openai-responses-v1"
    name = "OpenAI Responses"
    destination = ENDPOINT
    configuration_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    credential_fields = [{"id": "api_key", "label": "API key", "required": True, "max_bytes": 4096}]

    @property
    def digest(self):
        return contract_digest("agent-provider-adapter-v1", {
            "id": self.adapter_id, "destination": self.destination,
            "backend_sha256": hashlib.sha256(Path(__file__).with_name("preparation_agent_backend.py").read_bytes()).hexdigest(),
            "registration_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})

    def validate_configuration(self, value):
        if value != {}:
            raise AgentProviderError("agent_provider_request_rejected")

    def validate_credentials(self, value):
        if (set(value) != {"api_key"} or not isinstance(value["api_key"], str)
                or not 1 <= len(value["api_key"]) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in value["api_key"])):
            raise AgentProviderError("agent_provider_request_rejected")

    def backend(self, resolve, configuration):
        self.validate_configuration(configuration)
        return OpenAIResponsesBackend(lambda generation: resolve(generation)["api_key"])


class AgentProviderRegistry:
    def __init__(self, root: Path, *, adapters=None, execution_admission=None):
        from .preparation_release_policy import AgentExecutionAdmission
        if execution_admission is not None and type(execution_admission) is not AgentExecutionAdmission:
            raise AgentProviderError("agent_provider_unavailable")
        self.execution_admission = execution_admission
        try:
            registered = (OpenAIResponsesAdapter(),) if adapters is None else tuple(adapters)
            self.adapters = {item.adapter_id: item for item in registered}
            if len(self.adapters) != len(registered) or not 1 <= len(registered) <= 16:
                raise ValueError()
            for item in registered:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", item.adapter_id):
                    raise ValueError()
                canonical_bytes(self._adapter_metadata(item))
            self.root = ensure_private_directory(root)
            self.secrets = SecretStore(self.root)
            self.path = self.root / "providers.json"
            self._lock = threading.RLock()
            if not self.path.exists() and not self.path.is_symlink():
                write_private_json(self.path, {"schema_version": 1, "providers": []})
            self._read()
        except Exception:
            raise AgentProviderError("agent_provider_unavailable") from None

    @staticmethod
    def _adapter_metadata(adapter):
        return {"adapter_id": adapter.adapter_id, "name": adapter.name, "destination": adapter.destination,
            "configuration_schema": adapter.configuration_schema, "credential_fields": adapter.credential_fields,
            "model_selection": "explicit_ids"}

    def _read(self):
        try:
            ensure_private_directory(self.root)
            journal = ProviderJournal.model_validate(read_private_json(self.path, max_bytes=MAX_REGISTRY_BYTES))
            canonical_bytes(journal.model_dump())
            return journal
        except Exception:
            raise AgentProviderError("agent_provider_unavailable") from None

    def _write(self, journal):
        value = ProviderJournal.model_validate(journal.model_dump()).model_dump()
        if len(canonical_bytes(value)) > MAX_REGISTRY_BYTES:
            raise AgentProviderError("agent_provider_limit")
        try:
            write_private_json(self.path, value)
        except Exception:
            raise AgentProviderError("agent_provider_unavailable") from None

    def _adapter(self, adapter_id):
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise AgentProviderError("agent_provider_adapter_unavailable")
        return adapter

    @staticmethod
    def _record(journal, provider_id):
        record = next((item for item in journal.providers if item.provider_id == provider_id), None)
        if record is None:
            raise AgentProviderError("agent_provider_not_found")
        return record

    def _credential_status(self, record):
        if record.status != "active":
            return "unavailable"
        try:
            return "configured" if all(self.secrets.get(ref)["status"] == "active"
                for ref in record.credential_references.values()) else "unavailable"
        except Exception:
            return "unavailable"

    def _metadata(self, record):
        adapter = self.adapters.get(record.adapter_id)
        credential_status = self._credential_status(record)
        available = (self._execution_current() and adapter is not None and credential_status == "configured"
            and any(self.execution_admission.permits(adapter.adapter_id, adapter.digest, model) for model in record.models))
        return record.model_dump(exclude={"credential_references"}) | {
            "credential_status": credential_status,
            "destination": adapter.destination if adapter else None, "execution_available": available}

    def list(self):
        with self._lock:
            journal = self._read()
            return {"schema_version": 1, "adapters": [self._adapter_metadata(item) for item in self.adapters.values()],
                "providers": [self._metadata(item) for item in journal.providers],
                "live_execution_enabled": self._execution_current() and bool(self.execution_admission.providers)}

    def _execution_current(self):
        if self.execution_admission is None:
            return False
        try:
            self.execution_admission.check_current()
            return True
        except PreparationError:
            return False

    def _new_credentials(self, adapter, credentials):
        values = {key: value.get_secret_value() for key, value in credentials.items()}
        adapter.validate_credentials(values)
        references = {}
        try:
            for field, value in values.items():
                references[field] = self.secrets.create("Agent provider " + field, value)["secret_id"]
            return references
        except Exception:
            # Unpublished values confer no provider authority. Revoke created
            # records best-effort; an interrupted write never reuses them.
            for reference in references.values():
                try:
                    self.secrets.revoke(reference)
                except Exception:
                    pass
            raise AgentProviderError("agent_provider_unavailable") from None

    def _revoke(self, references):
        try:
            for reference in references.values():
                self.secrets.revoke(reference)
        except Exception:
            raise AgentProviderError("agent_provider_unavailable") from None

    def create(self, payload):
        try:
            choices = CreateProvider.model_validate(payload)
        except Exception:
            raise AgentProviderError("agent_provider_request_rejected") from None
        with self._lock:
            journal = self._read()
            if len(journal.providers) >= MAX_RECORDS or sum(item.status == "active" for item in journal.providers) >= MAX_PROVIDERS:
                raise AgentProviderError("agent_provider_limit")
            adapter = self._adapter(choices.adapter_id)
            adapter.validate_configuration(choices.configuration)
            references = self._new_credentials(adapter, choices.credentials)
            record = ProviderRecord(provider_id=str(uuid.uuid4()), adapter_id=adapter.adapter_id,
                name=choices.name, models=choices.models, configuration=choices.configuration,
                generation=str(uuid.uuid4()), credential_generation=str(uuid.uuid4()),
                credential_references=references, status="active")
            journal.providers.append(record)
            self._write(journal)
            return {"schema_version": 1, "provider": self._metadata(record)}

    def update(self, provider_id, payload):
        try:
            choices = UpdateProvider.model_validate(payload)
        except Exception:
            raise AgentProviderError("agent_provider_request_rejected") from None
        with self._lock:
            journal = self._read(); record = self._record(journal, provider_id)
            if record.status != "active" or record.generation != choices.expected_generation:
                raise AgentProviderError("agent_provider_configuration_stale")
            adapter = self._adapter(record.adapter_id)
            adapter.validate_configuration(choices.configuration)
            previous = record.credential_references
            changes = {"name": choices.name, "models": choices.models, "configuration": choices.configuration,
                "generation": str(uuid.uuid4())}
            if choices.credentials:
                changes.update(credential_references=self._new_credentials(adapter, choices.credentials),
                    credential_generation=str(uuid.uuid4()))
            replacement = ProviderRecord.model_validate(record.model_dump() | changes)
            journal.providers[journal.providers.index(record)] = replacement
            self._write(journal)
            if choices.credentials:
                self._revoke(previous)
            return {"schema_version": 1, "provider": self._metadata(replacement)}

    def remove(self, provider_id, payload):
        try:
            choices = RemoveProvider.model_validate(payload)
            if type(choices.schema_version) is not int:
                raise ValueError()
        except Exception:
            raise AgentProviderError("agent_provider_request_rejected") from None
        with self._lock:
            journal = self._read(); record = self._record(journal, provider_id)
            if record.status != "active" or record.generation != choices.expected_generation:
                raise AgentProviderError("agent_provider_configuration_stale")
            replacement = ProviderRecord.model_validate(record.model_dump() | {"status": "removed", "generation": str(uuid.uuid4())})
            journal.providers[journal.providers.index(record)] = replacement
            self._write(journal)
            self._revoke(record.credential_references)
            return {"schema_version": 1, "provider": self._metadata(replacement)}

    def _resolve(self, provider_id, generation, credential_generation):
        with self._lock:
            record = self._record(self._read(), provider_id)
            if record.status != "active" or record.generation != generation or record.credential_generation != credential_generation:
                raise AgentProviderError("agent_provider_configuration_stale")
            try:
                values = self.secrets.resolve(record.credential_references)
                self._adapter(record.adapter_id).validate_credentials(values)
                return values
            except Exception:
                raise AgentProviderError("agent_provider_unavailable") from None

    def configuration(self, provider_id):
        from .preparation_agent_host import AgentProviderConfiguration
        with self._lock:
            record = self._record(self._read(), provider_id)
            if record.status != "active" or self._credential_status(record) != "configured":
                raise AgentProviderError("agent_provider_unavailable")
            adapter = self._adapter(record.adapter_id)
            adapter.validate_configuration(record.configuration)
            return AgentProviderConfiguration(record.provider_id,
                adapter.backend(lambda credential_generation: self._resolve(record.provider_id, record.generation, credential_generation),
                    record.configuration), tuple(record.models), record.credential_generation,
                generation=record.generation, destination=adapter.destination, adapter_digest=adapter.digest, name=record.name)

    def configurations(self):
        with self._lock:
            # Removed/unavailable entries remain visible in owner setup, but
            # cannot be selected for source transfer or gain dispatch authority.
            result = []
            for record in self._read().providers:
                if record.status == "active" and self._credential_status(record) == "configured" and record.adapter_id in self.adapters:
                    result.append(self.configuration(record.provider_id))
            return tuple(result)

    def dispatch(self, method, path, payload=None, *, owner_authorized=False):
        if owner_authorized is not True:
            raise AgentProviderError("agent_provider_owner_required")
        if path == "/v1/agent-providers":
            if method == "GET" and payload is None:
                return self.list()
            if method == "POST":
                return self.create(payload)
        match = re.fullmatch(r"/v1/agent-providers/([0-9a-f-]{36})(/remove)?", path)
        if method == "POST" and match:
            return self.remove(match[1], payload) if match[2] else self.update(match[1], payload)
        raise AgentProviderError("agent_provider_request_rejected")


def agent_provider_routes(local_policy, registry):
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from .policy import LocalPolicy, load_json
    if not isinstance(local_policy, LocalPolicy):
        raise AgentProviderError("agent_provider_owner_required")

    async def dispatch(request):
        principal = request.scope.get("mcpme.principal", {})
        if principal.get("owner") is not True or principal.get("grant_id") is not None:
            return JSONResponse({"api_version": 1, "error": {"code": "grant_scope"}}, status_code=403)
        try:
            data = None if request.method == "GET" else await request.body()
            if data is not None and len(data) > 64 * 1024:
                raise AgentProviderError("agent_provider_request_rejected")
            payload = None if data is None else load_json(data)
            result = registry.dispatch(request.method, request.url.path, payload, owner_authorized=True)
            return JSONResponse(result | {"api_version": 1})
        except AgentProviderError as error:
            status = {"agent_provider_configuration_stale": 409, "agent_provider_not_found": 404}.get(error.code, 400)
            return JSONResponse({"api_version": 1, "error": {"code": error.code}}, status_code=status)
        except Exception:
            return JSONResponse({"api_version": 1, "error": {"code": "agent_provider_request_rejected"}}, status_code=400)

    local_policy.app.routes.extend([
        Route("/v1/agent-providers", dispatch, methods=["GET", "POST"]),
        Route("/v1/agent-providers/{provider_id}", dispatch, methods=["POST"]),
        Route("/v1/agent-providers/{provider_id}/remove", dispatch, methods=["POST"]),
    ])
    return local_policy
