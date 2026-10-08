"""Cold directories remain inspectable before an extension has a candidate."""
from test_preparation_agent_adapter import adapter_foundation


def test_cold_untyped_directory_with_agent_family_is_inspectable(adapter_foundation):
    imports, _, preparations, draft, derivations, _, _ = adapter_foundation
    before = imports.inspect(draft["draft_id"])
    result = preparations.inspect_source(draft)
    assert result["source_revision"] == draft["source"]["revision"]
    assert result["candidates"] == []
    assert isinstance(result["blockers"], list)
    assert result["execution_available"] is False and not result["verified_capabilities"]
    assert imports.inspect(draft["draft_id"]) == before
    assert not preparations.list() and not derivations.workers
    assert derivations.db.execute("SELECT count(*) FROM agent_adapter_derivations").fetchone()[0] == 0


def test_missing_optional_blockers_preserves_other_inspection_diagnostics(adapter_foundation, monkeypatch):
    _, _, preparations, draft, _, family, _ = adapter_foundation
    from meecp import preparation_inspection
    original = preparation_inspection.inspect_source

    def legacy(value):
        return {**original(value), "blockers": ["synthetic_legacy_inspection_blocker"]}

    monkeypatch.setattr(preparation_inspection, "inspect_source", legacy)
    monkeypatch.setattr(family, "inspect_source", lambda value: {
        "source_revision": value["source"]["revision"], "candidates": [], "execution_available": False})
    result = preparations.inspect_source(draft)
    assert "synthetic_legacy_inspection_blocker" in result["blockers"]
    assert result["candidates"] == [] and result["execution_available"] is False
