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
ENGINE = ("textgen-engine-v1", "1.0.9", "b" * 64)
TASK = ("text-generate-v1", "1.0.9", "c" * 64)


@pytest.mark.parametrize("requested", [False, True])
async def test_public_textgen_start_and_restart_use_ordinary_launcher(monkeypatch, requested):
    calls = []
    async def ordinary(self, state, environment, home, *, enabled):
        calls.append(enabled); return "owned-child"
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    harness = object.__new__(driver.PublicTextgenHarness)
    assert await harness.start("state", {}, "home", enabled=requested) == "owned-child"
    assert calls == [False]


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
