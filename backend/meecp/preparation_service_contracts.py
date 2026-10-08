"""Closed service source and probe authority; no I/O, secrets, or execution."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .preparation_components import ComponentReference
from .preparation_contracts import Contract, Digest, ExecutionPolicy, PreparationError, SourceIdentity, UUID, contract_digest


class LoopbackEndpointV1(Contract):
    scheme: Literal["http"]
    host: Literal["127.0.0.1"]
    port: int = Field(ge=1, le=65535)
    base_path: Literal["/v1"]

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"


class ServiceLimitsV1(Contract):
    timeout_seconds: int = Field(ge=1, le=60)
    max_input_bytes: int = Field(ge=1, le=16384)
    max_output_tokens: int = Field(ge=1, le=512)
    max_response_bytes: int = Field(ge=256, le=1048576)


class ServiceSecretReferencesV1(Contract):
    api_key: UUID


class ServiceConnectionV1(Contract):
    schema_version: Literal[1]
    protocol: ComponentReference
    transport_auth: ComponentReference
    task: ComponentReference
    endpoint: LoopbackEndpointV1
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]
    secret_references: ServiceSecretReferencesV1
    limits: ServiceLimitsV1

    @property
    def digest(self):
        return contract_digest("mcpme-service-connection-source-v1", self.model_dump())

    @property
    def revision(self):
        return "sha256:" + self.digest

    def portable(self):
        """A sealed package has logical roles, never host secret UUIDs."""
        return {**self.model_dump(exclude={"secret_references"}), "auth_role": "api_key"}


class ServiceSourceV1(Contract):
    kind: Literal["service_connection"]
    contract_version: Literal[1]
    connection: ServiceConnectionV1
    revision: SourceIdentity
    files: list = Field(max_length=0)

    @model_validator(mode="after")
    def bound(self):
        if self.revision != self.connection.revision:
            raise PreparationError("service_source_identity_mismatch")
        return self


class ServiceProbeGrantV1(Contract):
    schema_version: Literal[1]
    source_revision: SourceIdentity
    descriptor_digest: Digest
    protocol: ComponentReference
    transport_auth: ComponentReference
    task: ComponentReference
    endpoint: LoopbackEndpointV1
    method: Literal["POST"]
    path: Literal["/v1/chat/completions"]
    auth_role: Literal["api_key"]
    secret_reference: UUID
    fixture_digest: Digest
    max_calls: Literal[1]
    request_bytes: int = Field(ge=1, le=32768)
    response_bytes: int = Field(ge=256, le=1048576)
    timeout_seconds: int = Field(ge=1, le=60)

    @property
    def digest(self):
        return contract_digest("mcpme-service-probe-grant-v1", self.model_dump())


class ServiceExecutionPolicyV1(ExecutionPolicy):
    """Distinct wire version/domain: legacy ExecutionPolicy bytes are untouched."""
    schema_version: Literal[2]
    service_probe: ServiceProbeGrantV1

    @property
    def digest(self):
        return contract_digest("mcpme-service-execution-policy-v1", self.model_dump())


def parse_policy(value):
    if isinstance(value, ServiceExecutionPolicyV1):
        return value
    if isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 2:
        return ServiceExecutionPolicyV1.model_validate(value)
    return ExecutionPolicy.model_validate(value)
