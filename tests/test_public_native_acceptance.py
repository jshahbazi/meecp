"""Public acceptance must exercise source authority rather than an injected gate."""
import copy
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("public_native_acceptance", SCRIPTS / "verify_phase3_public_native.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def capabilities():
    return {"automatic_preparation": True, "recipe_execution": True,
        "preparation_resolution": {"execution": True, "candidate_kinds": ["python-callable", "python-mcp"],
            "producer_versions": ["1.0.1"]},
        "preparation_execution": {"admission_digest": "a" * 64,
            "families": [list(driver.RESOLVED), list(driver.HOTDOG)],
            "recipe_component_sets": [[list(driver.RESOLVED)], [list(driver.ONNX), list(driver.HOTDOG)]]}}


@pytest.mark.parametrize("kind", ["python", "onnx"])
@pytest.mark.parametrize("requested", [False, True])
async def test_every_start_uses_ordinary_launcher_even_for_execution_and_restart(monkeypatch, kind, requested):
    seen = []
    async def ordinary(self, state, environment, home, *, enabled):
        seen.append((state, environment, home, enabled)); return "owned-child"
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    cls = driver.PublicPythonHarness if kind == "python" else driver.PublicOnnxHarness
    obj = object.__new__(cls)
    assert await obj.start("state", {"PATH": "/usr/bin:/bin"}, "home", enabled=requested) == "owned-child"
    assert seen == [("state", {"PATH": "/usr/bin:/bin"}, "home", False)]


@pytest.mark.parametrize("damage", ["preparation", "resolution", "private_producer", "changed_python", "missing_engine", "mixed_closure"])
def test_unavailable_or_different_authority_cannot_be_counted_as_public_acceptance(damage):
    value = copy.deepcopy(capabilities())
    if damage == "preparation": value["automatic_preparation"] = False
    elif damage == "resolution": value["preparation_resolution"]["execution"] = False
    elif damage == "private_producer": value["preparation_resolution"]["producer_versions"] = ["1.0.0"]
    elif damage == "changed_python": value["preparation_execution"]["families"][0][2] = "b" * 64
    elif damage == "missing_engine": value["preparation_execution"]["recipe_component_sets"][1].pop(0)
    elif damage == "mixed_closure": value["preparation_execution"]["recipe_component_sets"] = [value["preparation_execution"]["families"]]
    with pytest.raises(driver.common.AcceptanceError):
        driver.public_capabilities(value)


def test_supported_policy_and_mixed_content_preserve_exact_asset_closure(tmp_path):
    assert driver.public_capabilities(capabilities()) == "a" * 64
    obj = object.__new__(driver.PublicMixedHarness)
    expected = obj.source_fixture(tmp_path, "callable")
    assert set(expected) == {"worker.py", "LICENSE", "sample.png", "unknown.bin"}
    assert (tmp_path / "sample.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert expected == {p.name: driver.common.digest(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("damage", [None, "import", "revision", "source"])
async def test_native_folder_intake_binds_original_revision_without_cli_creating_a_draft(tmp_path, damage):
    base = tmp_path.resolve(); source = base / "source"; state = base / "state"
    source.mkdir(mode=0o700); state.mkdir(mode=0o700)
    driver.common.write_private_json(state / "manager-connection.json", {"synthetic": True})
    obj = object.__new__(driver.PublicPythonHarness)
    obj.native = base / "unused-native"
    observed = {"draft_id": "synthetic-import", "revision": 3, "source": {"revision": "sha256:" + "b" * 64}}
    request = {"import_id": observed["draft_id"], "revision": 2, "source_revision": observed["source"]["revision"]}
    if damage == "import": request["import_id"] = "different-import"
    elif damage == "revision": request["revision"] = 1
    elif damage == "source": request["source_revision"] = "sha256:" + "c" * 64
    calls = []
    async def native(argv, environment, home, **kwargs):
        assert argv[1] == "--public-directory-native-acceptance"
        case = driver.common.private_json(Path(argv[2]))
        assert case["source_directory"] == str(source) and "draft_id" not in case
        assert (state / ("native-public-directory-" + case["fixture_nonce"] + ".json")).is_file()
        driver.common.write_private_json(Path(case["output_path"]), {"passed": True,
            "native_directory_selection": True, "draft_id": observed["draft_id"],
            "source_selection_revision": 2, "initial_revision": 1,
            "resolution_job_id": "synthetic-resolution", "candidate_id": "a" * 64})
        return 0, b""
    async def cli(_state, _environment, _home, *args):
        calls.append(args[:2])
        if args[:2] == ("imports", "inspect"): return observed
        if args[:2] == ("resolutions", "get"): return {"request": request}
        assert args[:2] == ("preparations", "inspect-source")
        return {"candidates": [{"kind": "python-callable", "candidate_id": "a" * 64}]}
    obj.run = native; obj.cli = cli
    if damage is not None:
        with pytest.raises(driver.common.AcceptanceError, match="public_resolution_origin_mismatch"):
            await obj.native_preparation(base, state, {}, base, source, "callable", None)
    else:
        origin, candidates, receipt, path = await obj.native_preparation(base, state, {}, base, source, "callable", None)
        assert origin == {**observed, "revision": 2} and candidates[0]["candidate_id"] == receipt["candidate_id"]
    assert ("imports", "create") not in calls
