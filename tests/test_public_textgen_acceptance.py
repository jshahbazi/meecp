"""Ordinary local text acceptance cannot substitute a private execution gate."""
import copy
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("public_textgen_acceptance", SCRIPTS / "verify_phase3_public_textgen.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
ENGINE = ("llamacpp-cpu-v1", "1.0.25", "b" * 64)
TASK = ("text-generate-v1", "1.0.25", "c" * 64)


@pytest.mark.parametrize("requested", [False, True])
async def test_public_textgen_start_and_restart_use_ordinary_launcher(monkeypatch, requested):
    calls = []
    async def ordinary(self, state, environment, home, *, enabled):
        calls.append(enabled); return "owned-child"
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    harness = object.__new__(driver.PublicTextgenHarness)
    assert await harness.start("state", {}, "home", enabled=requested) == "owned-child"
    assert calls == [False]


@pytest.mark.parametrize("previous_version", [None, "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.22", "1.0.23"])
async def test_public_defaults_bind_actual_current_producer_before_start(monkeypatch, previous_version):
    from meecp import preparation_textgen
    version = preparation_textgen.VERSION
    recipe_version = preparation_textgen.RECIPE_VERSION
    refs = {item.id: item.key for item in preparation_textgen.references()}
    assert version == "1.0.25" and preparation_textgen.ENGINE_ID == "llamacpp-cpu-v1"
    started = []
    harness = object.__new__(driver.PublicTextgenHarness)
    harness.new_case = lambda label: ("base", "state", "home", {})

    async def start(state, environment, home, *, enabled):
        started.append(enabled)
        return "owned-child"

    async def cli(state, environment, home, *arguments):
        if arguments[0] == "preparations":
            return {"preparations": []}
        return {"automatic_preparation": True, "recipe_execution": True,
            "preparation_resolution": {"execution": True, "candidate_kinds": ["python-callable", "python-mcp"],
                "producer_versions": ["1.0.1"]},
            "preparation_execution": {"admission_digest": "a" * 64,
                "families": [list(driver.public.RESOLVED), list(driver.public.HOTDOG), list(refs[preparation_textgen.TASK_ID])],
                "recipe_component_sets": [[list(driver.public.RESOLVED)],
                    [list(driver.public.ONNX), list(driver.public.HOTDOG)],
                    [list(refs[preparation_textgen.ENGINE_ID]), list(refs[preparation_textgen.TASK_ID])]]}}

    async def recipe(*args):
        return {"state": "enabled", "version": recipe_version}

    async def shutdown(*args):
        return None

    harness.start, harness.cli, harness.recipe, harness.shutdown = start, cli, recipe, shutdown
    if previous_version:
        monkeypatch.setattr(preparation_textgen, "VERSION", previous_version)
        with pytest.raises(driver.common.AcceptanceError, match="public_textgen_producer_version_changed"):
            await harness.public_defaults()
        assert not started
    else:
        result = await harness.public_defaults()
        assert result["passed"] and result["recipe_version"] == recipe_version and started == [False]


@pytest.mark.parametrize("damage", [None, "engine", "task", "closure", "disabled"])
def test_exact_public_engine_task_closure_is_required(damage):
    value = {"automatic_preparation": True, "recipe_execution": True,
        "preparation_resolution": {"execution": True, "candidate_kinds": ["python-callable", "python-mcp"],
            "producer_versions": ["1.0.1"]},
        "preparation_execution": {"admission_digest": "a" * 64,
            "families": [list(driver.public.RESOLVED), list(driver.public.HOTDOG), list(TASK)],
            "recipe_component_sets": [[list(driver.public.RESOLVED)],
                [list(driver.public.ONNX), list(driver.public.HOTDOG)], [list(ENGINE), list(TASK)]]}}
    value = copy.deepcopy(value)
    if damage == "engine": value["preparation_execution"]["recipe_component_sets"][2][0][2] = "d" * 64
    elif damage == "task": value["preparation_execution"]["families"][2][2] = "d" * 64
    elif damage == "closure": value["preparation_execution"]["recipe_component_sets"][2].pop(0)
    elif damage == "disabled": value["automatic_preparation"] = False
    if damage is None:
        assert driver.public_textgen_capabilities(value, ENGINE, TASK) == "a" * 64
    else:
        with pytest.raises(driver.common.AcceptanceError):
            driver.public_textgen_capabilities(value, ENGINE, TASK)
