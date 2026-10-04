"""Trusted metadata rules for choosing an agent preparation path.

Rules are registered by host code against an exact guide and component. They
inspect captured names, sizes and text classifications only. A compatible choice
is a useful review default, not evidence of successful analysis or execution.
Imported recipes cannot register rules or inspect source through this seam.
"""
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import StrictModel
from .preparation_agent_contracts import AnalysisGuideV1
from .preparation_agent_guides import builtin_analysis_guides
from .preparation_contracts import PreparationError, RecipeReference

MAX_AGENT_RECIPE_CHOICES = 128
MAX_AGENT_FILE_CHOICES = 256


def source_choice_inventory(source):
    """Advertise text for content transfer and all eligible identities separately."""
    kinds = ({item["path"]: item["kind"] for item in source["file_metadata"]}
             if source["kind"] == "application_directory" else {item["path"]: "text" for item in source["files"]})
    content = [item for item in source["files"] if kinds.get(item["path"]) == "text" and 1 <= item["size"] <= 16384]
    metadata = [item for item in source["files"] if 1 <= item["size"] <= 16 * 1024 * 1024]
    return {"files": content[:MAX_AGENT_FILE_CHOICES], "files_truncated": len(content) > MAX_AGENT_FILE_CHOICES,
            "metadata_files": metadata[:MAX_AGENT_FILE_CHOICES], "metadata_files_truncated": len(metadata) > MAX_AGENT_FILE_CHOICES}


class AgentRecipeChoiceRuleV1(StrictModel):
    guide_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,127}$")]
    guide_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    component: RecipeReference
    label: Annotated[str, Field(min_length=1, max_length=96)]
    preferred_recipe_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,127}$")]
    source_suffixes: Annotated[list[Annotated[str, Field(pattern=r"^\.[a-z0-9]{1,16}$")]],
                               Field(min_length=1, max_length=8)]
    identity_pair: Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]],
                            Field(min_length=2, max_length=2)]
    pair_requirement: Literal["absent", "single"]

    @model_validator(mode="after")
    def unambiguous(self):
        if (len(set(self.identity_pair)) != 2 or len(set(self.source_suffixes)) != len(self.source_suffixes)
                or any(ord(char) < 32 for char in self.label)):
            raise ValueError("agent_recipe_choice_rule_invalid")
        return self


def builtin_recipe_choice_rules():
    locked, unlocked = builtin_analysis_guides()
    return tuple(AgentRecipeChoiceRuleV1(guide_id=guide.id, guide_digest=guide.digest,
        component=guide.component, label=label, preferred_recipe_id=recipe_id,
        source_suffixes=[".py"], identity_pair=["pyproject.toml", "uv.lock"],
        pair_requirement=requirement) for guide, label, recipe_id, requirement in (
            (locked, "Python app with an existing dependency lock", "mcpme.agent-python-adapter", "single"),
            (unlocked, "Python app needing dependency resolution", "mcpme.agent-unlocked-python-adapter", "absent")))


class AgentRecipeChoiceRegistry:
    def __init__(self, rules):
        self.rules = tuple(AgentRecipeChoiceRuleV1.model_validate(item.model_dump()) for item in rules)
        keys = [(rule.guide_id, rule.guide_digest, rule.component.id,
                 rule.component.version, rule.component.digest) for rule in self.rules]
        if len(keys) > 64 or len(keys) != len(set(keys)):
            raise PreparationError("agent_recipe_choice_registration_invalid")

    @staticmethod
    def _matches_source(rule, source, inventory):
        if source["kind"] not in {"local_folder", "application_directory"}:
            return False
        records = {item["path"]: item for item in source["files"]}
        kinds = ({item["path"]: item["kind"] for item in source["file_metadata"]}
                 if source["kind"] == "application_directory" else {path: "text" for path in records})
        visible_content = {item["path"] for item in inventory["files"]}
        visible_identities = {item["path"] for item in inventory["metadata_files"]}
        selected = [PurePosixPath(path) for path, item in records.items()
                    if kinds.get(path) == "text" and 1 <= item["size"] <= 16384
                    and path in visible_content and PurePosixPath(path).suffix in rule.source_suffixes]
        if not selected:
            return False
        first, second = rule.identity_pair
        # Count all named pairs, including unsupported binary/oversized ones.
        # An unusable existing lock must not turn into an unlocked default.
        parents = {PurePosixPath(path).parent for path in records if PurePosixPath(path).name == first}
        pairs = [parent for parent in parents if str(parent / second) in records]
        if rule.pair_requirement == "absent":
            return not pairs
        if len(pairs) != 1:
            return False
        parent = pairs[0]
        identities = [str(parent / name) for name in rule.identity_pair]
        return (all(path in visible_identities and kinds.get(path) == "text"
                    and 1 <= records[path]["size"] <= 16 * 1024 * 1024
                    for path in identities)
                and any(parent == path.parent or parent in path.parents for path in selected))

    def choices(self, source, recipe_store, guides, records, *, source_inventory):
        """Bound output to the exact displayed inventory; perform metadata checks."""
        if len(records) > MAX_AGENT_RECIPE_CHOICES:
            raise PreparationError("agent_recipe_choice_inventory_invalid")
        result, matched = [], {}
        for record in records:
            ref = {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]}
            choice = {"recipe": ref, "label": record["recipe_id"][:96],
                      "compatibility": "unresolved", "recommended": False}
            try:
                admitted = recipe_store.assert_admissible(ref["id"], ref["version"], ref["digest"])
                guide = guides.resolve(admitted)
            except PreparationError:
                guide = None
            if guide is not None:
                value = AnalysisGuideV1.model_validate(guide)
                key = (value.id, value.digest, value.component.id, value.component.version, value.component.digest)
                rule = next((item for item in self.rules if (item.guide_id, item.guide_digest,
                    item.component.id, item.component.version, item.component.digest) == key), None)
                if rule is not None:
                    choice["label"] = rule.label
                    if self._matches_source(rule, source, source_inventory):
                        choice["compatibility"] = "compatible"
                        matched[len(result)] = (key, rule)
            result.append(choice)
        # Overlapping guide families and multiple versions remain explicit.
        if len({key for key, _ in matched.values()}) == 1:
            preferred = [index for index, (_, rule) in matched.items()
                         if result[index]["recipe"]["id"] == rule.preferred_recipe_id]
            candidate = preferred if preferred else list(matched)
            if len(candidate) == 1:
                result[candidate[0]]["recommended"] = True
        return result
