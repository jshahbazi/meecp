"""Immutable host-source execution admission; never parsed from client data."""
from __future__ import annotations

from dataclasses import dataclass

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

    def __post_init__(self):
        if (type(self.families) is not frozenset or type(self.recipe_component_sets) is not frozenset
                or type(self.resolution_candidate_kinds) is not frozenset
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
        return contract_digest("public-preparation-admission-v1", {
            "families": [list(key) for key in sorted(self.families)],
            "recipe_component_sets": sorted(sorted([list(key) for key in keys]) for keys in self.recipe_component_sets),
            "resolution_candidate_kinds": sorted(self.resolution_candidate_kinds)})

    def require_candidate(self, candidate):
        if (candidate.get("kind") not in self.resolution_candidate_kinds
                or any(key.startswith("_agent") for key in candidate)):
            raise PreparationError("resolution_candidate_not_admitted")

    def require_request(self, request):
        if (request.candidate_kind not in self.resolution_candidate_kinds
                or request.producer.id != "python-resolution-v1" or request.producer.version != "1.0.1"):
            raise PreparationError("resolution_candidate_not_admitted")


def public_preparation_admission() -> PublicPreparationAdmission | None:
    """Release source hook. Activation follows exact artifact acceptance."""
    return None
