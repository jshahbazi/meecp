"""Opt-in real hotdog build/probe with a simulated model and an adapter repair.

This checks execution and asset closure, not live model quality. No provider
credentials or private responses are read. The source and model are user-selected
through explicit environment paths; the ordinary confined runner owns execution.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import traceback
from types import SimpleNamespace
import uuid
import zlib

import pytest

from mcpme.app_setup import AppSetupHost
from mcpme.contracts import validate_package
from mcpme.imports import ImportStore
from mcpme.preparation_agent_adapter import AgentPythonAdapterFamily
from mcpme.preparation_agent_adapter_store import AgentAdapterDerivationStore
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_forms import PreparationForms
from mcpme.preparation_runner import PreparationRunner
from mcpme.preparations import PreparationStore
from mcpme.recipes import RecipeStore
from test_preparation_agent import output, response, role_of
from test_preparation_agent_host import provider


def black_image():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * 96) * 32)) + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


@pytest.mark.skipif(not os.environ.get("MCPME_APP_SETUP_HOST") or not os.environ.get("MCPME_APP_SETUP_SOURCE"),
    reason="requires an exact built host and the selected hotdog source with its bundled model")
async def test_hotdog_assets_and_actual_confined_test_drive_adapter_repair(tmp_path, monkeypatch):
    host_root = Path(os.environ["MCPME_APP_SETUP_HOST"]).resolve()
    source = Path(os.environ["MCPME_APP_SETUP_SOURCE"]).resolve()
    model = source / "models/yolo11n.onnx"
    provenance = json.loads((source / "model-provenance.json").read_bytes())
    assert hashlib.sha256(model.read_bytes()).hexdigest() == provenance["onnx"]["sha256"]
    tool = {"name": "detect_hotdog", "description": "Detect hot dogs in a client-supplied image.",
        "input_schema": {"type": "object", "properties": {"image_base64": {"type": "string"},
            "confidence": {"type": "number", "default": 0.35}}, "required": ["image_base64"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"hotdog": {"type": "boolean"},
            "verdict": {"type": "string"}}, "required": ["hotdog", "verdict"], "additionalProperties": False}}
    fixture = {"arguments": {"image_base64": black_image(), "confidence": 0.95},
        "expected": {"hotdog": False, "verdict": "not_hotdog"}}
    wrapper = ("from detector import detect_hotdog\n"
        "def invoke(image_base64: str, confidence: float = 0.35) -> dict:\n"
        "    result = detect_hotdog(image_base64=image_base64, confidence=confidence)\n"
        "    return {'hotdog': result['hotdog'], 'verdict': result['verdict']}\n")
    calls = []
    def transport(request):
        role = role_of(request)
        calls.append(role)
        if role == "interface":
            files = [{"path": "proposed/interface/interface.json", "content": json.dumps({"tool": tool})}]
        elif role == "dependencies":
            files = [{"path": "proposed/dependencies/dependencies.json", "content": json.dumps({
                "project": "pyproject.toml", "lock": "uv.lock", "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest()})}]
        elif role == "fixtures":
            files = [{"path": "proposed/fixtures/fixtures.json", "content": json.dumps(fixture)}]
        else:
            # A valid static wrapper with a real runtime error must fail the
            # actual confined fixture. Only the adapter changes on repair.
            code = wrapper if calls.count("adapter") > 1 else wrapper.replace("detect_hotdog(image_base64=", "detect_hotdog(wrong_argument=")
            files = [{"path": "proposed/adapter/adapter.json", "content": json.dumps({"schema_version": 1,
                "module": "mcpme_generated_agent_adapter", "function": "invoke", "source_license": "LICENSE"})},
                {"path": "proposed/adapter/wrapper.py", "content": code},
                {"path": "proposed/adapter/LICENSE", "content": "Permission to use, copy, modify and distribute this generated test wrapper is granted.\n"}]
        return response(role, output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text",
            "text": json.dumps(output(role, files=files))}]}])

    with ImportStore(tmp_path.resolve() / "state") as imports:
        recipes = RecipeStore(imports)
        record = recipes.add(Path(__file__).resolve().parents[1] / "templates/builtin-recipes/agent-python-adapter-v1-0-1")
        recipes.enable(record["recipe_id"], record["version"])
        draft = imports.create(source, name="Hotdog repair check", intent="Detect hot dogs from an image", source_mode="automatic")
        assert "models/yolo11n.onnx" in {item["path"] for item in draft["source"]["files"]}
        family = AgentPythonAdapterFamily(AgentAdapterDerivationStore(imports, recipes))
        preparations = PreparationStore(imports, recipes, families=(family,))
        builder = PreparationRunner(preparations, host_root, host_root / "bin/mcpme-reap", enabled=True,
            enabled_families=frozenset({family.component.reference.key}))
        agent = AgentPreparationHost(preparations, tmp_path.resolve() / "agent",
            providers=(provider(transport),), simulated_enabled=True)
        async def static_work(function, *args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)
        host = SimpleNamespace(agent_preparations=agent, imports=imports, preparations=preparations,
            preparation_forms=PreparationForms(preparations), preparation_runner=builder,
            resolution_runner=SimpleNamespace(enabled=False, tasks={}), static_work=static_work)
        setup = AppSetupHost(host)
        failures = []
        build = setup._build
        async def diagnostic_build(*args):
            try:
                return await build(*args)
            except Exception as error:
                # Repository frame locations and fixed codes only. Never retain
                # exception messages, provider bodies or process output here.
                failures.append({"type": type(error).__name__, "code": getattr(error, "code", None),
                    "frames": [(Path(frame.filename).name, frame.lineno, frame.name)
                        for frame in traceback.extract_tb(error.__traceback__) if "/backend/mcpme/" in frame.filename]})
                raise
        monkeypatch.setattr(setup, "_build", diagnostic_build)
        try:
            plan = await setup.plan({"import_id": draft["draft_id"], "revision": draft["revision"],
                "provider_id": "configured-openai", "model": "explicit-test-model"})
            assert plan["execution_available"]
            job = await setup.start({"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
                "idempotency_key": str(uuid.uuid4())})
            await setup.tasks[job["job_id"]]
            result = setup.get(job["job_id"])
            assert result["state"] == "ready", (result.get("error"), failures)
            assert result["local_io_settled"] and result["installed"] is False
            assert calls == ["interface", "dependencies", "fixtures", "adapter", "adapter"]
            assert result["calls_used"] == 5
            builds = preparations.list()
            assert len(builds) == 2 and {item["state"] for item in builds} == {"failed", "succeeded"}
            saved = imports.inspect(draft["draft_id"])
            package = validate_package(Path(saved["attachment"]["path"]))
            assert (package.root / "source/models/yolo11n.onnx").read_bytes() == model.read_bytes()
            assert (package.root / "source/mcpme_generated_agent_adapter.py").read_text() == wrapper
            assert hashlib.sha256(model.read_bytes()).hexdigest() == provenance["onnx"]["sha256"]
        finally:
            await setup.close()
            await agent.close()
            await builder.close()
