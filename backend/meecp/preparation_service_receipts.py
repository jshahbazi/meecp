"""Service receipt variant; published directory receipt models stay unchanged."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .preparation_components import ComponentReference
from .preparation_contracts import Digest, JobBinding, PreparationError, PreparationPackage, RecipeReference, contract_digest, load_contract
from .preparation_inputs import InputContract, OriginalSourceReference, RuntimeReference, SourceValidationReference


class ServiceBoundInputsV1(InputContract):
    schema_version: Literal[2]
    source: OriginalSourceReference
    descriptor_digest: Digest
    recipe: RecipeReference
    package: PreparationPackage
    components: tuple[ComponentReference, ...] = Field(min_length=3, max_length=3)
    source_validations: tuple[SourceValidationReference, ...] = Field(min_length=3, max_length=3)
    runtimes: tuple[RuntimeReference, ...] = Field(min_length=1, max_length=1)
    portable_configuration_digest: Digest
    derivation_digest: Digest
    expected_package_contract_digest: Digest

    @model_validator(mode="after")
    def bound(self):
        keys = [item.key for item in self.components]
        if (self.source.kind != "service_connection" or self.source.schema_version != 1
                or len(set(keys)) != 3 or {item.component.key for item in self.source_validations} != set(keys)
                or any(item.source_revision != self.source.revision or item.files or item.dependency_lock_digest is not None
                       for item in self.source_validations)):
            raise PreparationError("service_input_binding_mismatch")
        return self

    @property
    def captured_collection(self):
        return None

    @property
    def digest(self):
        return contract_digest("mcpme-service-bound-inputs-v1", self.model_dump(mode="json"))


class ServicePreparationReceiptV1(InputContract):
    schema_version: Literal[3]
    binding: JobBinding
    inputs: ServiceBoundInputsV1
    fixture_digest: Digest
    service_probe_grant_digest: Digest

    @model_validator(mode="after")
    def bound(self):
        if (self.binding.source_revision != self.inputs.source.revision
                or self.binding.descriptor_digest != self.inputs.descriptor_digest
                or self.binding.recipe != self.inputs.recipe):
            raise PreparationError("service_receipt_mismatch")
        return self

    @property
    def digest(self):
        return contract_digest("mcpme-service-preparation-receipt-v1", self.model_dump(mode="json"))


def parse_receipt(value):
    from .preparation_inputs import parse_preparation_receipt
    raw = load_contract(value) if isinstance(value, bytes) else value
    if isinstance(raw, dict) and type(raw.get("schema_version")) is int and raw["schema_version"] == 3:
        return ServicePreparationReceiptV1.model_validate(raw)
    return parse_preparation_receipt(value)


def validate_job_receipt(receipt, job):
    """Discriminator, complete original derivation, and reviewed grant agree."""
    raw = job.get("bound_inputs")
    service = isinstance(raw, dict) and raw.get("schema_version") == 2
    if (receipt.schema_version == 3) != service:
        raise PreparationError("service_receipt_mismatch")
    if service:
        expected = ServiceBoundInputsV1.model_validate(raw)
        if (receipt.inputs != expected or receipt.inputs.digest != job.get("bound_inputs_digest")
                or receipt.service_probe_grant_digest != job.get("service_probe_grant_digest")):
            raise PreparationError("service_receipt_mismatch")
