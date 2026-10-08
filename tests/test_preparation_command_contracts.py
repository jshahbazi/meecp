"""Command contracts bind schema, argv, helper, licenses and original closure."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from meecp.imports import ImportStore
from meecp.preparation_contracts import PreparationError, contract_digest
from meecp.preparation_inputs import OriginalSourceReference, candidate_contract_digest, parse_bound_inputs
from meecp.recipes import RecipeStore, builtin_components
from test_meecp_recipes import recipe
from test_preparation_inputs import bind, source_binding, view  # noqa: F401


def command_fields(view):
    return {"command": {"version": 1, "component": "macos26-arm64-command-v3", "helper_sha256": "a" * 64,
        "commands": {view.tools[0].name: {"executable": "source/worker", "args": []}},
        "deadline_ms": 1000, "input_bytes": 4096, "output_bytes": 4096},
        "licenses": ["source/LICENSE"], "source_artifacts": [
            {"path": "source/LICENSE", "sha256": "b" * 64, "size": 8, "executable": False,
             "source": "LICENSE", "provenance": "selected-source", "license": "source/LICENSE"},
            {"path": "source/worker", "sha256": "c" * 64, "size": 16, "executable": True,
             "source": "worker", "provenance": "selected-source", "license": "source/LICENSE"}]}


def test_command_contract_has_explicit_versioned_domain_and_complete_identity(view):
    view, draft = view
    fields = command_fields(view)
    value = view.expected_package_contract(draft["source"]["revision"], profile="host-command", **fields)
    assert value["schema_version"] == 2
    assert candidate_contract_digest(value) == contract_digest("preparation-host-command-candidate-v1", value)
    assert candidate_contract_digest(value) != contract_digest("preparation-candidate-v1", value)
    legacy = view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio")
    assert candidate_contract_digest(legacy) == contract_digest("preparation-candidate-v1", legacy)
    for mutate in (
        lambda raw: raw["command"].update(helper_sha256="d" * 64),
        lambda raw: raw["command"]["commands"][view.tools[0].name].update(args=["--changed"]),
        lambda raw: raw["source_artifacts"][1].update(sha256="d" * 64),
        lambda raw: raw["source_artifacts"][1].update(provenance="different-source"),
    ):
        changed = copy.deepcopy(fields)
        mutate(changed)
        normalized = view.expected_package_contract(draft["source"]["revision"], profile="host-command", **changed)
        assert candidate_contract_digest(normalized) != candidate_contract_digest(value)


@pytest.mark.parametrize("mutation", ["no_command", "other_tool", "nonexec", "unlisted_license", "duplicate", "unbound_path"])
def test_incomplete_or_inconsistent_command_contract_rejected(view, mutation):
    view, draft = view
    fields = command_fields(view)
    if mutation == "no_command":
        fields["command"] = None
    elif mutation == "other_tool":
        fields["command"]["commands"] = {"unknown": {"executable": "source/worker"}}
    elif mutation == "nonexec":
        fields["source_artifacts"][1]["executable"] = False
    elif mutation == "unlisted_license":
        fields["source_artifacts"][1]["license"] = "source/OTHER"
    elif mutation == "duplicate":
        fields["source_artifacts"].append(fields["source_artifacts"][1])
    else:
        fields["source_artifacts"][1]["path"] = "worker"
    with pytest.raises(PreparationError, match="expected_package_contract_mismatch"):
        view.expected_package_contract(draft["source"]["revision"], profile="host-command", **fields)


def test_legacy_contract_cannot_accept_command_extension(view):
    view, draft = view
    with pytest.raises(PreparationError, match="expected_package_contract_mismatch"):
        view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio", **command_fields(view))


@pytest.mark.parametrize("kind,version", [("application_directory", 1), ("local_folder", 2), ("document", 2)])
def test_original_source_versions_cannot_be_reinterpreted(kind, version):
    with pytest.raises(ValidationError, match="unsupported_preparation_source_version"):
        OriginalSourceReference(kind=kind, schema_version=version, revision="sha256:" + "a" * 64)


def test_application_source_reference_is_explicit_version_two():
    reference = OriginalSourceReference(kind="application_directory", schema_version=2, revision="sha256:" + "a" * 64)
    assert reference.model_dump()["schema_version"] == 2


def test_explicit_application_source_does_not_require_a_captured_collection(view):
    view, draft = view
    bound = bind(view, draft).model_dump(mode="json")
    bound["source"].update(kind="application_directory", schema_version=2)
    bound["captured_collection"] = None
    parsed = parse_bound_inputs(bound)
    assert parsed.source.schema_version == 2 and parsed.captured_collection is None


def test_schema_two_recipe_requires_explicit_host_catalog_capability(tmp_path):
    root = recipe(tmp_path, requirements={"package_schema": 2})
    catalog = builtin_components()
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports, component_catalog=catalog)
        added = store.add(root)
        assert "recipe_incompatible" in added["blockers"]
    # Static metadata compatibility confers no runner admission.
    for component in catalog.values():
        component["package_schemas"] = [1, 2]
    with ImportStore(tmp_path / "other-state") as imports:
        store = RecipeStore(imports, component_catalog=catalog)
        added = store.add(root)
        assert not added["blockers"]
        enabled = store.enable(added["recipe_id"], added["version"])
        assert enabled["evidence"]["runnable"] is False
