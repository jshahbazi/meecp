"""Immutable host-source execution admission; never parsed from client data."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from .preparation_components import ComponentReference
from .preparation_contracts import PreparationError, contract_digest

ComponentKey = tuple[str, str, str]


@dataclass(frozen=True)
class PublicPreparationAdmission:
    """Exact reviewed implementations and ordinary inspected Python sources.

    Recipe IDs are deliberately absent: an independent declarative recipe can
    use the same complete reviewed component closure without a dispatcher edit.
    Construction belongs to trusted host source, never config or an API route.
    """

    families: frozenset[ComponentKey]
    recipe_component_sets: frozenset[frozenset[ComponentKey]]
    resolution_candidate_kinds: frozenset[str] = frozenset()
    release_policy_digest: str | None = None
    artifact: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.artifact is not None:
            from .preparation_release_artifact import ReleaseArtifactProof
            if type(self.artifact) is not ReleaseArtifactProof:
                raise PreparationError("invalid_public_preparation_admission")
        if (type(self.families) is not frozenset or type(self.recipe_component_sets) is not frozenset
                or type(self.resolution_candidate_kinds) is not frozenset
                or self.release_policy_digest is not None and (type(self.release_policy_digest) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", self.release_policy_digest))
                or not self.resolution_candidate_kinds <= {"python-callable", "python-mcp"}):
            raise PreparationError("invalid_public_preparation_admission")
        try:
            if any(type(keys) is not frozenset or not keys for keys in self.recipe_component_sets):
                raise ValueError()
            for keys in (self.families, *self.recipe_component_sets):
                if type(keys) is not frozenset:
                    raise ValueError()
                for key in keys:
                    if type(key) is not tuple or len(key) != 3:
                        raise ValueError()
                    ComponentReference(id=key[0], version=key[1], digest=key[2])
        except (ValueError, TypeError):
            raise PreparationError("invalid_public_preparation_admission") from None

    @property
    def digest(self):
        value = {
            "families": [list(key) for key in sorted(self.families)],
            "recipe_component_sets": sorted(sorted([list(key) for key in keys]) for keys in self.recipe_component_sets),
            "resolution_candidate_kinds": sorted(self.resolution_candidate_kinds)}
        if self.release_policy_digest is not None:
            value["release_policy_digest"] = self.release_policy_digest
        if self.artifact is not None:
            value["host_manifest_digest"] = self.artifact.host_manifest_digest
        return contract_digest("public-preparation-admission-v1", value)

    def require_candidate(self, candidate):
        if (candidate.get("kind") not in self.resolution_candidate_kinds
                or any(key.startswith("_agent") for key in candidate)):
            raise PreparationError("resolution_candidate_not_admitted")

    def check_current(self, *, full=False):
        if self.artifact is not None:
            self.artifact.check_current(full=full)

    def require_request(self, request):
        if (request.candidate_kind not in self.resolution_candidate_kinds
                or request.producer.id != "python-resolution-v1" or request.producer.version != "1.0.1"):
            raise PreparationError("resolution_candidate_not_admitted")


def public_preparation_admission() -> PublicPreparationAdmission | None:
    """Release source hook. Activation follows exact artifact acceptance."""
    from .preparation_release_policy import release_admissions
    return release_admissions()[0]
