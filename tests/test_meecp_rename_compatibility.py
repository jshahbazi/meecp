"""Renaming the host must preserve sealed packages and existing client contracts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from meecp.contracts import PackageError, validate_package
from meecp.imports import ImportStore
from meecp.management import Management
from meecp.preparation_contracts import PreparationError
from meecp.preparations import receipt_name
from meecp.private import snapshot_package
from meecp.recipe_sdk import relock_recipe
from meecp.recipes import RecipeError, validate_bundle
from test_meecp_packages import make_package
from test_meecp_recipes import recipe


def legacy_package(root: Path) -> Path:
    make_package(root)
    for current, old in (("meecp.package.json", "mcpme.package.json"),
                         ("meecp.lock.json", "mcpme.lock.json")):
        (root / current).rename(root / old)
    return root


def test_legacy_package_snapshot_keeps_original_seal_and_metadata(tmp_path):
    original = validate_package(legacy_package(tmp_path / "package"))
    snapshot = snapshot_package(original, tmp_path / "private" / "snapshot")
    assert snapshot.digest == original.digest
    assert snapshot.manifest_name == "mcpme.package.json"
    assert snapshot.lock_name == "mcpme.lock.json"
    for source, copied in zip(original.metadata_paths, snapshot.metadata_paths):
        assert source.read_bytes() == copied.read_bytes()
    assert not (snapshot.root / "meecp.package.json").exists()


@pytest.mark.parametrize("source_mode", ["legacy", "automatic"])
def test_legacy_package_import_is_recognized_as_prepared(tmp_path, source_mode):
    source = legacy_package(tmp_path / "package")
    imports = ImportStore(tmp_path / "state")
    try:
        draft = imports.create(source, name="Legacy package", source_mode=source_mode)
        assert draft["source"]["kind"] == "prepared_package"
        assert draft["source"]["file_inventory"] == "mcpme.lock.json"
        assert draft["attachment"] is not None
    finally:
        imports.close()


@pytest.mark.parametrize("duplicate", [False, True])
def test_mixed_or_duplicate_package_descriptors_are_rejected(tmp_path, duplicate):
    root = make_package(tmp_path / "package")
    if duplicate:
        (root / "mcpme.package.json").write_bytes((root / "meecp.package.json").read_bytes())
        (root / "mcpme.lock.json").write_bytes((root / "meecp.lock.json").read_bytes())
    else:
        (root / "meecp.lock.json").rename(root / "mcpme.lock.json")
    with pytest.raises(PackageError):
        validate_package(root)


def test_legacy_recipe_relock_preserves_descriptor_names(tmp_path):
    root = recipe(tmp_path)
    for current, old in (("meecp.recipe.json", "mcpme.recipe.json"),
                         ("meecp.recipe.lock.json", "mcpme.recipe.lock.json")):
        (root / current).rename(root / old)
    for path in root.iterdir():
        path.chmod(0o600)
    root.chmod(0o700)
    original = validate_bundle(root)
    (root / "README.md").write_text("Updated synthetic documentation.\n")
    result = relock_recipe(root)
    assert result["recipe_id"] == original["manifest"]["id"]
    assert result["digest"] != original["digest"]
    assert (root / "mcpme.recipe.lock.json").is_file()
    assert not (root / "meecp.recipe.lock.json").exists()
    assert validate_bundle(root)["digest"] == result["digest"]


def test_duplicate_recipe_descriptors_are_rejected(tmp_path):
    root = recipe(tmp_path)
    (root / "mcpme.recipe.json").write_bytes((root / "meecp.recipe.json").read_bytes())
    (root / "mcpme.recipe.lock.json").write_bytes((root / "meecp.recipe.lock.json").read_bytes())
    with pytest.raises(RecipeError, match="^recipe_metadata_required$"):
        validate_bundle(root)


@pytest.mark.parametrize("name", ["meecp.preparation.json", "mcpme.preparation.json"])
def test_sealed_receipt_name_accepts_current_and_legacy(name):
    package = SimpleNamespace(lock=SimpleNamespace(artifacts=[SimpleNamespace(path=name)]))
    assert receipt_name(package) == name


def test_ambiguous_sealed_receipts_are_rejected():
    package = SimpleNamespace(lock=SimpleNamespace(artifacts=[
        SimpleNamespace(path="meecp.preparation.json"), SimpleNamespace(path="mcpme.preparation.json")]))
    with pytest.raises(PreparationError, match="^preparation_receipt_required$"):
        receipt_name(package)


@pytest.mark.asyncio
async def test_legacy_management_tool_calls_use_current_operation(tmp_path):
    calls = []

    async def caller(state, method, route, body, **kwargs):
        calls.append((method, route, body))
        return {"api_version": 1}

    manager = Management(tmp_path, credential_file=tmp_path / "credential", caller=caller)
    assert "meecp_capabilities" in manager.operations
    assert "mcpme_capabilities" not in manager.operations
    result = await manager.invoke("mcpme_capabilities", {})
    assert not result.isError
    assert calls == [("GET", "/v1/capabilities", None)]


@pytest.mark.parametrize("module", ["meecp", "meecp_textgen", "mcpme_textgen", "meecp_jevk5"])
def test_renamed_module_entrypoints_launch_from_an_unrelated_directory(tmp_path, module):
    backend = Path(__file__).resolve().parents[1] / "backend"
    import_path = os.pathsep.join((str(backend), str(backend.parent / "vendor")))
    result = subprocess.run([sys.executable, "-P", "-m", module, "--help"], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": import_path},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "usage:" in result.stdout
    if module != "meecp":
        assert "--meecp-config-fd" in result.stdout
        assert "--mcpme-config-fd" in result.stdout
