"""Bounded analysis contracts. These values confer no execution authority."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import StrictModel
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest, load_contract

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Role = Literal["interface", "dependencies", "fixtures", "adapter"]


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or len(value) > 256 or path.is_absolute() or str(path) != value
            or any(part in {".", ".."} for part in path.parts)
            or "\\" in value or any(ord(char) < 32 for char in value)):
        raise ValueError("agent_path_invalid")
    return value


class SourceFileV1(StrictModel):
    path: str
    sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=16384)]

    @model_validator(mode="after")
    def path_valid(self):
        relative_path(self.path)
        return self


class ProviderV1(StrictModel):
    backend: Literal["openai-responses-v1"]
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    credential_ref: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class AgentLimitsV1(StrictModel):
    max_calls: Annotated[int, Field(ge=4, le=4)] = 4
    max_parallel: Annotated[int, Field(ge=1, le=3)] = 3
    # Sum of serialized provider-request bodies, including JSON schemas.
    max_transfer_bytes: Annotated[int, Field(ge=1, le=2 * 1024 * 1024)] = 512 * 1024
    max_output_tokens: Annotated[int, Field(ge=64, le=16384)] = 4096
    wall_seconds: Annotated[int, Field(ge=1, le=600)] = 180
    max_artifact_bytes: Annotated[int, Field(ge=1, le=256 * 1024)] = 128 * 1024


class AnalysisRequestV1(StrictModel):
    schema_version: Literal[1] = 1
    import_id: Identity
    revision: Annotated[int, Field(ge=1, le=2**31 - 1)]
    source_revision: Annotated[str, Field(pattern=r"^(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})$")]
    authority_id: Identity
    expires_at: Annotated[int, Field(ge=1)]
    provider: ProviderV1
    selected_files: Annotated[list[SourceFileV1], Field(min_length=1, max_length=32)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    permitted_tools: list[Literal["read-selected-source", "propose-files"]]
    limits: AgentLimitsV1

    @model_validator(mode="before")
    @classmethod
    def version_type(cls, value):
        if isinstance(value, dict) and "schema_version" in value and type(value["schema_version"]) is not int:
            raise ValueError("agent_contract_invalid")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if type(self.schema_version) is not int:
            raise ValueError("agent_contract_invalid")
        if self.permitted_tools != ["read-selected-source", "propose-files"]:
            raise ValueError("agent_scope_invalid")
        paths = [item.path for item in self.selected_files]
        if paths != sorted(set(paths)) or sum(item.bytes for item in self.selected_files) > 128 * 1024:
            raise ValueError("agent_source_scope_invalid")
        canonical_bytes(self.model_dump())
        return self

    @property
    def digest(self):
        return contract_digest("analysis-request-v1", self.model_dump())

    def selected_source(self, source: dict[str, bytes]) -> dict[str, str]:
        """Accept only host-captured bytes; never traverse an ambient directory."""
        if set(source) != {item.path for item in self.selected_files}:
            raise PreparationError("agent_source_scope_mismatch")
        selected = {}
        for item in self.selected_files:
            data = source[item.path]
            if (not isinstance(data, bytes) or len(data) != item.bytes
                    or hashlib.sha256(data).hexdigest() != item.sha256):
                raise PreparationError("agent_source_identity_mismatch")
            try:
                selected[item.path] = data.decode("utf-8")
            except UnicodeError:
                raise PreparationError("agent_source_encoding_invalid") from None
        canonical_bytes(selected)
        return selected


class ProposedFileV1(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=256)]
    content: Annotated[str, Field(max_length=16384)]

    @model_validator(mode="after")
    def path_valid(self):
        relative_path(self.path)
        if not self.path.startswith("proposed/"):
            raise ValueError("agent_proposal_path_invalid")
        return self


class TaskProposalV1(StrictModel):
    role: Role
    files: Annotated[list[ProposedFileV1], Field(min_length=1, max_length=8)]
    unresolved: Annotated[list[Annotated[str, Field(max_length=512)]], Field(max_length=16)]

    @model_validator(mode="after")
    def bounded(self):
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("agent_proposal_conflict")
        canonical_bytes(self.model_dump())
        return self


class AgentUsageV1(StrictModel):
    input_tokens: Annotated[int, Field(ge=0, le=10**9)]
    output_tokens: Annotated[int, Field(ge=0, le=10**9)]


class AgentProposalV1(StrictModel):
    schema_version: Literal[1] = 1
    request_digest: Digest
    attempt_id: Identity
    generation: Identity
    tasks: Annotated[list[TaskProposalV1], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def integrated(self):
        if [task.role for task in self.tasks] != ["interface", "dependencies", "fixtures", "adapter"]:
            raise ValueError("agent_proposal_roles_invalid")
        paths = [file.path for task in self.tasks for file in task.files]
        if len(set(paths)) != len(paths):
            raise ValueError("agent_proposal_conflict")
        canonical_bytes(self.model_dump())
        return self

    @property
    def digest(self):
        return contract_digest("agent-proposal-v1", self.model_dump())


def parse_task(data: bytes, role: Role) -> TaskProposalV1:
    try:
        proposal = TaskProposalV1.model_validate(load_contract(data))
        if proposal.role != role:
            raise ValueError()
        return proposal
    except (ValueError, TypeError):
        raise PreparationError("agent_proposal_invalid") from None
