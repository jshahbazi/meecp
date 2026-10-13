"""Closed distribution-owned release policy; never owner configuration.

The fixed policy is outside TextGen's backend closure to avoid self-reference.
Its exact bytes are bound by the signed distribution's strict host manifest.
No endpoint, code, runtime path or factory is supplied by this artifact.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import asyncio
import hashlib
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import StrictModel
from .preparation_admission import ComponentKey, PublicPreparationAdmission
from .preparation_components import ComponentReference
from .preparation_contracts import PreparationError, contract_digest, load_contract

POLICY_PATH = "templates/release-policies/phase3-v1.json"
MAX_POLICY_BYTES = 64 * 1024
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


@dataclass(frozen=True)
class AgentExecutionAdmission:
    """Exact trusted guide components and accepted adapter/model combinations."""
    components: frozenset[ComponentKey]
    providers: frozenset[tuple[str, str, str]]  # adapter ID, implementation digest, model
    unlocked_resolution: bool = False
    release_policy_digest: str | None = None
    artifact: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.artifact is not None:
            from .preparation_release_artifact import ReleaseArtifactProof
            if type(self.artifact) is not ReleaseArtifactProof:
                raise PreparationError("invalid_agent_execution_admission")
        try:
            if (type(self.components) is not frozenset or not 1 <= len(self.components) <= 16
                    or type(self.providers) is not frozenset or len(self.providers) > 256
                    or type(self.unlocked_resolution) is not bool
                    or self.release_policy_digest is not None and (type(self.release_policy_digest) is not str
                        or not re.fullmatch(r"[0-9a-f]{64}", self.release_policy_digest))):
                raise ValueError()
            for key in self.components:
                if type(key) is not tuple or len(key) != 3:
                    raise ValueError()
                ComponentReference(id=key[0], version=key[1], digest=key[2])
            for key in self.providers:
                if (type(key) is not tuple or len(key) != 3
                        or any(type(value) is not str for value in key)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key[0])
                        or not re.fullmatch(r"[0-9a-f]{64}", key[1])
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key[2])):
                    raise ValueError()
            if self.unlocked_resolution and not any(key[0] == "agent-unlocked-python-adapter-v1" for key in self.components):
                raise ValueError()
        except (TypeError, ValueError):
            raise PreparationError("invalid_agent_execution_admission") from None

    @property
    def digest(self):
        return contract_digest("public-agent-execution-admission-v1", {
            "components": [list(key) for key in sorted(self.components)], "providers": [list(key) for key in sorted(self.providers)],
            "unlocked_resolution": self.unlocked_resolution, "release_policy_digest": self.release_policy_digest,
            "host_manifest_digest": self.artifact.host_manifest_digest if self.artifact is not None else None})

    def permits(self, adapter_id, adapter_digest, model):
        return (adapter_id, adapter_digest, model) in self.providers

    def check_current(self, *, full=False):
        if self.artifact is not None:
            self.artifact.check_current(full=full)


class PreparationPolicy(StrictModel):
    families: Annotated[list[ComponentReference], Field(max_length=32)]
    recipe_component_sets: Annotated[list[Annotated[list[ComponentReference], Field(min_length=1, max_length=32)]], Field(max_length=32)]
    resolution_candidate_kinds: Annotated[list[Literal["python-callable", "python-mcp"]], Field(max_length=2)]


class ProviderPolicy(StrictModel):
    adapter_id: Identifier
    adapter_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    models: Annotated[list[Identifier], Field(min_length=1, max_length=16)]


class AgentPolicy(StrictModel):
    components: Annotated[list[ComponentReference], Field(max_length=16)]
    providers: Annotated[list[ProviderPolicy], Field(max_length=16)]
    unlocked_resolution: bool


class CommandPolicy(StrictModel):
    component: Annotated[list[str], Field(min_length=3, max_length=3)]
    installed_platform_policy: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    resource_platform_policy: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReleasePolicy(StrictModel):
    schema_version: Literal[1]
    policy_id: Identifier
    preparation: PreparationPolicy
    agent: AgentPolicy
    command: CommandPolicy | None

    @model_validator(mode="before")
    @classmethod
    def strict_flags(cls, value):
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or not isinstance(value.get("agent"), dict)
                or type(value["agent"].get("unlocked_resolution")) is not bool):
            raise ValueError("invalid_release_policy")
        return value

    @model_validator(mode="after")
    def unique(self):
        groups = [self.preparation.families, self.agent.components, *self.preparation.recipe_component_sets]
        if any(len({item.key for item in group}) != len(group) for group in groups):
            raise ValueError("invalid_release_policy")
        closures = [frozenset(item.key for item in group) for group in self.preparation.recipe_component_sets]
        if (len(set(closures)) != len(closures)
                or len(set(self.preparation.resolution_candidate_kinds)) != len(self.preparation.resolution_candidate_kinds)
                or len({item.adapter_id for item in self.agent.providers}) != len(self.agent.providers)
                or any(len(set(item.models)) != len(item.models) for item in self.agent.providers)
                or not self.agent.components and (self.agent.providers or self.agent.unlocked_resolution)):
            raise ValueError("invalid_release_policy")
        return self

    def admissions(self):
        policy_digest = contract_digest("release-policy-authority-v1", self.model_dump(mode="json"))
        preparation = PublicPreparationAdmission(frozenset(item.key for item in self.preparation.families),
            frozenset(frozenset(item.key for item in group) for group in self.preparation.recipe_component_sets),
            frozenset(self.preparation.resolution_candidate_kinds), policy_digest)
        agent = (AgentExecutionAdmission(frozenset(item.key for item in self.agent.components),
            frozenset((item.adapter_id, item.adapter_digest, model) for item in self.agent.providers for model in item.models),
            self.agent.unlocked_resolution, policy_digest) if self.agent.components else None)
        if not preparation.families and not preparation.recipe_component_sets and not preparation.resolution_candidate_kinds:
            preparation = None
        if agent is not None and (preparation is None or not agent.components <= preparation.families
                or any(frozenset({key}) not in preparation.recipe_component_sets for key in agent.components)):
            raise PreparationError("invalid_release_policy")
        command_keys = set() if preparation is None else {key for key in preparation.families if key[0] == "host-command-json-v1"}
        if self.command is not None:
            from .preparation_release_command_admission import CommandReleasePolicyV1
            command = CommandReleasePolicyV1(tuple(self.command.component), self.command.installed_platform_policy,
                self.command.resource_platform_policy)
            if (preparation is None or command_keys != {command.component}
                    or frozenset({command.component}) not in preparation.recipe_component_sets):
                raise PreparationError("invalid_release_policy")
        else:
            command = None
            if command_keys:
                raise PreparationError("invalid_release_policy")
        return preparation, agent, command


def validate_policy_implementations(policy: ReleasePolicy):
    """Check static implementation pins without granting execution authority.

    Producers use this check before signing. Runtime admission still requires
    the separate signed artifact proof and its held identity fences.
    """
    from .recipes import builtin_components
    try:
        preparation, agent, command = policy.admissions()
        if preparation is None and agent is None and command is None:
            return preparation, agent, command
        catalog = builtin_components()
        selected = preparation.families | frozenset(key for group in preparation.recipe_component_sets for key in group)
        for identifier, version, digest in selected:
            record = catalog.get(identifier + "@" + version)
            if record is None or record["digest"] != digest:
                raise ValueError()
        for group in preparation.recipe_component_sets:
            for identifier, version, _ in group:
                if any((item["component"], item["version"], item["digest"]) not in group
                        for item in catalog[identifier + "@" + version].get("dependencies", [])):
                    raise ValueError()
        if agent is not None:
            from .preparation_agent_providers import OpenAIResponsesAdapter
            from .preparation_agent_guides import builtin_analysis_guides
            if not agent.components <= frozenset((item.component.id, item.component.version, item.component.digest)
                    for item in builtin_analysis_guides()):
                raise ValueError()
            adapters = {item.adapter_id: item for item in (OpenAIResponsesAdapter(),)}
            for adapter_id, digest, _ in agent.providers:
                if adapter_id not in adapters or adapters[adapter_id].digest != digest:
                    raise ValueError()
        return preparation, agent, command
    except (OSError, ValueError, TypeError, KeyError):
        raise PreparationError("invalid_release_policy") from None


def _load_policy(host_root):
    """Private test seam; production caller has one fixed distribution root."""
    from .recipes import _read
    try:
        path = host_root / POLICY_PATH
        data = _read(path, MAX_POLICY_BYTES)
        policy = ReleasePolicy.model_validate(load_contract(data))
        preparation, agent, command = policy.admissions()
        if preparation is None and agent is None and command is None:
            return None, None, (None, None)
        from .preparation_release_artifact import verify_release_artifact, load_artifact_json, ReleaseArtifactProof
        artifact = verify_release_artifact(host_root, POLICY_PATH, data)
        if type(artifact) is not ReleaseArtifactProof:
            raise ValueError()
        # This is an artifact identity check within the signed-distribution /
        # same-user boundary, not a publisher signature inferred from JSON.
        build = load_artifact_json(_read(host_root / "build.json", 8 * 1024**2))
        if (build.get("source_tree_dirty") is not False
                or not isinstance(build.get("source_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", build["source_commit"])
                or not isinstance(build.get("artifact_sha256"), dict)
                or len(build["artifact_sha256"]) > 20000
                or build["artifact_sha256"].get(POLICY_PATH) != hashlib.sha256(data).hexdigest()):
            raise ValueError()
        preparation, agent, command = validate_policy_implementations(policy)
        if command is not None:
            from .preparation_release_command_admission import command_release_admission
            try:
                command_admissions = command_release_admission(command, host_root, artifact=artifact)
            except PreparationError as error:
                if error.code != "command_release_platform_unavailable":
                    raise
                # The command gate validates the full artifact before reporting
                # an unaccepted platform. Close only this implementation and
                # dependent closures; retain the complete reviewed policy and
                # host identity for the remaining admitted families.
                preparation = replace(preparation,
                    families=preparation.families - {command.component},
                    recipe_component_sets=frozenset(group for group in preparation.recipe_component_sets
                        if command.component not in group))
                command_admissions = (None, None)
        else:
            command_admissions = (None, None)
        if _read(path, MAX_POLICY_BYTES) != data:
            raise ValueError()
        artifact.check_current(full=True)
        preparation = replace(preparation, artifact=artifact)
        if agent is not None: agent = replace(agent, artifact=artifact)
        return preparation, agent, command_admissions
    except (OSError, ValueError, TypeError, KeyError):
        raise PreparationError("invalid_release_policy") from None


def release_admissions():
    # Source checkouts and standalone development hosts retain manual package
    # operation even after the tracked distribution policy becomes nonempty.
    # Only this fixed module location selects the context; neither policy bytes
    # nor an owner setting can opt a source host into automatic execution.
    module = Path(__file__).absolute()
    resolved = module.resolve()
    if not any(part.endswith(".app") for path in (module, resolved) for part in path.parts):
        return None, None, (None, None)
    # Preserve strict rejection for every enclosed app, including malformed
    # layouts and links which point outside their original app context.
    return _load_policy(resolved.parents[2])


async def load_release_admissions():
    """Join bounded signature/manifest inspection before canceled startup ends."""
    from .processes import _await_ownership
    task = asyncio.create_task(asyncio.to_thread(release_admissions))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _await_ownership(task)
        except Exception:
            pass
        raise
