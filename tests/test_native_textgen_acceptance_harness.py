"""Native textgen evidence must bind admission and survive cleanup failures."""
import asyncio
import importlib
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_native_textgen")


def test_factory_admits_only_exact_textgen_task(module, monkeypatch, tmp_path):
    from meecp import supervisor, preparation_runner
    engine, task, onnx = ("engine", "1", "a" * 64), ("task", "1", "b" * 64), ("onnx", "1", "c" * 64)
    selected = ModuleType("meecp.preparation_textgen")
    selected.references = lambda: (SimpleNamespace(key=engine), SimpleNamespace(key=task))
    monkeypatch.setitem(sys.modules, selected.__name__, selected)
    calls = []

    class Runner:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"enabled": True}
            self.enabled_families = frozenset({onnx})

    async def serve(*args, **kwargs):
        assert set(kwargs) == {"preparation_runner_factory"}
        runner = kwargs["preparation_runner_factory"]()
        assert runner.enabled_families == frozenset({task})
        calls.append(runner)

    monkeypatch.setattr(preparation_runner, "PreparationRunner", Runner)
    monkeypatch.setattr(supervisor, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["harness", str(tmp_path), str(tmp_path), str(tmp_path), str(tmp_path), "1"])
    original_path = sys.path[:]
    try:
        exec(module.ENABLED_HOST, {})
    finally:
        sys.path[:] = original_path
    assert len(calls) == 1


def test_unlisted_host_runtime_file_rejected_before_artifact_reader(module, monkeypatch, tmp_path):
    (tmp_path / "build.json").write_text(json.dumps({"artifact_sha256": {"runtime.py": "a" * 64}}))
    (tmp_path / "runtime.py").write_bytes(b"synthetic runtime")
    (tmp_path / "unlisted.py").write_bytes(b"unlisted synthetic code")
    calls = []
    monkeypatch.setattr(module.primitives, "host_hashes", lambda host: calls.append(host))
    with pytest.raises(module.common.AcceptanceError, match="textgen_host_payload_membership_mismatch"):
        module.sealed_host_identity(tmp_path)
    assert calls == []


def test_source_binding_uses_captured_paths_and_preserves_original_folder_namespace(module):
    captured = {"name": "selected-model", "path": "artifacts/selected-model", "sha256": "a" * 64, "size": 17}
    assert module.expected_source_files({"source": {"kind": "captured_artifact_collection",
        "collection": {"entries": [captured]}}}) == [
            {"path": "artifacts/selected-model", "sha256": "a" * 64, "size": 17}]
    original = [{"path": "README.md", "sha256": "b" * 64, "size": 11}]
    assert module.expected_source_files({"source": {"kind": "directory", "files": original},
        "captured_artifacts": {"entries": [captured]}}) == original


def test_model_residents_are_distinguished_from_owned_host_gateway(module):
    details = {"processes": [{"process_id": name, "instance_id": name, "ownership_lost": False}
        for name in ("engine", "adapter")]}
    ledger = {name: {"state": "owned"} for name in ("engine", "adapter", "gateway")}
    assert module.resident_graph(details, ledger, "engine") == {"resident_processes": 2, "host_owned_processes": 3}
    for altered in ({key: value for key, value in ledger.items() if key != "engine"},
                    {**ledger, "unbound": {"state": "owned"}}):
        with pytest.raises(module.common.AcceptanceError, match="installed_textgen_resident_graph_mismatch"):
            module.resident_graph(details, altered, "engine")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1.0.2", "7.2.1"])
async def test_recipe_lifecycle_uses_selected_host_version(module, monkeypatch, tmp_path, version):
    selected = ModuleType("meecp.preparation_textgen")
    selected.VERSION = "1.0.21"
    selected.RECIPE_VERSION = version
    monkeypatch.setitem(sys.modules, selected.__name__, selected)
    harness = module.Harness(tmp_path, tmp_path, tmp_path, {"safe_to_remove": True}, tmp_path)
    calls = []

    async def cli(state, environment, home, *arguments):
        calls.append(arguments)
        return {"api_version": 1}

    monkeypatch.setattr(harness, "cli", cli)
    await harness.recipe(tmp_path, {}, tmp_path)
    await harness.recipe(tmp_path, {}, tmp_path, "disable")
    assert calls == [("recipes", action, "--id", module.RECIPE_ID, "--version", version, "--json")
                     for action in ("inspect", "disable")]


@pytest.mark.asyncio
async def test_sdk_observation_keeps_only_forwarded_request_identity(module):
    delivered = []

    class Stream:
        async def send(self, message):
            delivered.append(message)

    observed = module.ObservedWrite(Stream())
    message = SimpleNamespace(message=SimpleNamespace(root=SimpleNamespace(
        method="tools/call", id=7, params={"name": "text_generate", "arguments": {"text": "private fixture"}})))
    await observed.send(message)
    assert delivered == [message]
    assert observed.request_ids == [7] and observed.sent.is_set()
    assert set(vars(observed)) == {"stream", "request_ids", "sent"}


@pytest.mark.asyncio
async def test_failed_sdk_send_does_not_claim_cancellation_identity(module):
    class Stream:
        async def send(self, message):
            raise OSError("private transport details")

    observed = module.ObservedWrite(Stream())
    message = SimpleNamespace(message=SimpleNamespace(root=SimpleNamespace(method="tools/call", id=7,
        params={"name": "text_generate"})))
    with pytest.raises(OSError):
        await observed.send(message)
    assert observed.request_ids == [] and not observed.sent.is_set()


@pytest.mark.asyncio
async def test_official_sdk_cancellation_maps_observed_request_to_error(module):
    import anyio
    from mcp import ClientSession
    from mcp.server import Server
    from mcp.server.models import InitializationOptions
    from mcp.shared.exceptions import McpError
    from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams, ServerCapabilities, ToolsCapability
    server = Server("Synthetic cancellation mapping")
    started = asyncio.Event()
    incoming_send, incoming = anyio.create_memory_object_stream(1)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(1)

    @server.list_tools()
    async def listing():
        return []

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        started.set()
        await asyncio.Event().wait()

    async def serve():
        await server.run(outgoing_receive, incoming_send, InitializationOptions(
            server_name="synthetic", server_version="1", capabilities=ServerCapabilities(tools=ToolsCapability())))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(serve)
        async with incoming, outgoing:
            observed = module.ObservedWrite(outgoing)
            async with ClientSession(incoming, observed) as client:
                await client.initialize()
                active = asyncio.create_task(client.call_tool("text_generate", {}))
                await started.wait()
                await client.send_notification(ClientNotification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=observed.request_ids[-1]))))
                with pytest.raises(McpError):
                    await active
                assert active.done() and len(observed.request_ids) == 1
        tasks.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_relocation_unjoined_owner_preserves_receipt(module, monkeypatch, tmp_path):
    from meecp import processes
    verifier = ModuleType("meecp.preparation_textgen_verifier")
    workspace = tmp_path / "probe"
    workspace.mkdir()
    closed = []

    class Owner:
        instances = {"unjoined": object()}

        def __init__(self, reaper):
            pass

        async def close(self):
            closed.append(True)

    async def verify(*args, **kwargs):
        assert isinstance(kwargs["owner"], Owner)
        return object()

    verifier.verify_textgen_package = verify
    monkeypatch.setitem(sys.modules, verifier.__name__, verifier)
    monkeypatch.setattr(processes, "ProcessOwner", Owner)
    with pytest.raises(module.common.AcceptanceError, match="textgen_owned_work_remaining"):
        await module.relocation_probe(tmp_path / "package", object(), tmp_path / "reaper", workspace)
    assert closed == [True] and (tmp_path / "ownership-unsettled").is_file()


@pytest.mark.asyncio
async def test_sdk_join_survives_repeated_parent_cancellation(module):
    released = asyncio.Event()

    async def request():
        await released.wait()
        return "settled"

    child = asyncio.create_task(request())
    parent = asyncio.create_task(module.join_tasks([child]))
    await asyncio.sleep(0)
    parent.cancel()
    await asyncio.sleep(0)
    parent.cancel()
    assert not parent.done() and not child.cancelled()
    released.set()
    try:
        await parent
    except asyncio.CancelledError:
        pass
    assert child.done() and child.result() == "settled"


@pytest.mark.parametrize("change", ["tokens", "bytes", "type"])
def test_generation_enforces_actual_token_and_byte_bounds(module, change):
    value = {"text": "synthetic", "input_tokens": 1, "output_tokens": 1, "stop_reason": "length"}
    if change == "tokens":
        value["output_tokens"] = 9
    elif change == "bytes":
        value["text"] = "é" * 1025
    else:
        value["output_tokens"] = True
    with pytest.raises(module.common.AcceptanceError, match="installed_textgen_output_bound_failed"):
        module.validate_generation(value, 8, [{"name": "text_generate", "outputSchema": {"type": "object"}}])


def test_any_acceptance_failure_retains_private_evidence(module):
    report, root = {}, None
    try:
        with pytest.raises(RuntimeError):
            with module.isolated_workspace(report) as (root, lease):
                (root / "receipt").write_bytes(b"private synthetic evidence")
                raise RuntimeError("private error text")
        assert not lease["safe_to_remove"] and report["failure_artifacts_retained"] and report["isolated_state_retained"]
        assert Path(report["retained_state_directory"]) == root and (root / "receipt").exists()
    finally:
        if root is not None:
            shutil.rmtree(root)


def test_main_never_prints_exception_text_or_false_success(module, monkeypatch, tmp_path, capsys):
    host, native, model = tmp_path / "host", tmp_path / "native", tmp_path / "model"
    host.mkdir(); native.touch(); model.touch()

    async def verify(host, native, model, report):
        report["verified"] = True
        raise RuntimeError("private fixture response and key")

    monkeypatch.setattr(module, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["harness", "--host", str(host), "--native", str(native), "--model", str(model)])
    assert module.main() == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["verified"] is False and report["diagnostic"]["type"] == "RuntimeError"
    assert "private fixture" not in output


def test_cleanup_failure_retains_location_without_exception_details(module, monkeypatch):
    report, root = {}, None
    try:
        with pytest.raises(module.common.AcceptanceError, match="acceptance_workspace_cleanup_failed"):
            with module.isolated_workspace(report) as (root, _):
                def fail(_):
                    raise OSError("private cleanup details")
                monkeypatch.setattr(module.tempfile.TemporaryDirectory, "cleanup", fail)
        assert root.is_dir() and report["isolated_state_retained"]
        assert Path(report["retained_state_directory"]) == root
    finally:
        if root is not None:
            shutil.rmtree(root)
