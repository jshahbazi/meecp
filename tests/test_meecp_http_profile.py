from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import socket

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
import pytest
import uvicorn

from meecp.contracts import PackageError, validate_package
from meecp.http_service import make_http_app, protect_server, read_config
from test_meecp_packages import edit_manifest, make_package
from test_meecp_templates import fixture_export, preparation, stub_runtime


def http_package(tmp_path):
    package = make_package(tmp_path / "package")
    edit_manifest(package, profile="mcp-http", entrypoint=None, adapter_process="service",
        processes=[{"id": "service", "role": "adapter", "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
                    "depends_on": [], "readiness": {"path": "/health", "echo_generation": True}}],
        http={"configuration": "descriptor-v1", "session_termination": "delete"})
    return package


def test_http_schema_is_static_and_foreground_singleton(tmp_path, monkeypatch):
    package = http_package(tmp_path)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("static validation cannot execute"))
    result = validate_package(package)
    assert result.manifest.profile == "mcp-http" and result.manifest.model is None
    assert result.manifest.adapter_process == "service"
    assert result.manifest.http.configuration == "descriptor-v1"


@pytest.mark.parametrize("change,code", [
    ({"http": None}, "http_configuration_required"),
    ({"http": {"configuration": "environment", "session_termination": "delete"}}, "invalid_package_schema"),
    ({"http": {"configuration": "descriptor-v1", "session_termination": "none"}}, "invalid_package_schema"),
    ({"http": {"configuration": "descriptor-v1", "session_termination": "delete", "port": 19385}}, "invalid_package_schema"),
    ({"entrypoint": {"executable": "bin/service"}}, "unsupported_process_topology"),
    ({"adapter_process": "unknown"}, "http_identity_contract_required"),
    ({"resources": {"cpu_only": False, "gpu": True, "memory_mb": 64}}, "unsupported_process_topology"),
])
def test_http_schema_rejects_unowned_or_uninjectable_topologies(tmp_path, change, code):
    package = http_package(tmp_path)
    edit_manifest(package, **change)
    with pytest.raises(PackageError, match=code):
        validate_package(package)


@pytest.mark.parametrize("field,value", [("role", "engine"), ("depends_on", ["other"]),
                                        ("readiness", {"path": "/health", "echo_generation": False}),
                                        ("readiness", {"path": "/wrong", "echo_generation": True}),
                                        ("entrypoint", {"executable": "bin/service", "args": ["--meecp-config-fd"]})])
def test_http_identity_and_reserved_descriptor_contract(tmp_path, field, value):
    package = http_package(tmp_path)
    manifest = json.loads((package / "meecp.package.json").read_bytes())
    manifest["processes"][0][field] = value
    edit_manifest(package, processes=manifest["processes"])
    with pytest.raises(PackageError, match="http_identity_contract_required|invalid_entrypoint"):
        validate_package(package)


def server():
    selected = Server("synthetic-http")
    @selected.list_tools()
    async def listing():
        return [types.Tool(name="add", description="Add two synthetic integers.",
            inputSchema={"type": "object", "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                         "required": ["left", "right"], "additionalProperties": False},
            outputSchema={"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"], "additionalProperties": False})]
    @selected.call_tool()
    async def call(name, arguments):
        if arguments["left"] == 99:
            raise ValueError("private caller marker")
        return {"total": arguments["left"] + arguments["right"]}
    return selected


@asynccontextmanager
async def service():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    configuration = {"endpoint": {"host": "127.0.0.1", "port": port}, "api_key": "k" * 64, "nonce": "a" * 64,
                     "generation": 1, "model": None, "dependencies": {}}
    app, manager = make_http_app(server(), configuration)
    runtime = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, log_level="critical", access_log=False))
    task = asyncio.create_task(runtime.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not runtime.started:
                assert not task.done()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", configuration, manager
    finally:
        runtime.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.asyncio
async def test_private_http_identity_policy_real_sdk_and_delete():
    async with service() as (endpoint, config, manager):
        headers = {"authorization": "Bearer " + config["api_key"]}
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            assert (await http.get(endpoint + "/health")).status_code == 401
            assert (await http.get(endpoint + "/health", headers={"authorization": "Bearer wrong"})).status_code == 401
            assert (await http.get(endpoint + "/health", headers={**headers, "origin": "https://invalid.test"})).status_code == 403
            assert (await http.get(endpoint + "/health", headers={**headers, "host": "invalid.test"})).status_code == 403
            assert (await http.get(endpoint + "/health", headers=headers)).json() == {"ready": True, "generation": 1, "nonce": "a" * 64}
            malformed = await http.post(endpoint + "/mcp", headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 123, "arguments": {"private": "caller marker"}}})
            assert malformed.status_code == 400 and "caller marker" not in malformed.text
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=10) as http:
            async with streamable_http_client(endpoint + "/mcp", http_client=http) as (read, write, get_session):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    assert initialized.protocolVersion == "2025-11-25"
                    assert set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}
                    assert [tool.name for tool in (await client.list_tools()).tools] == ["add"]
                    assert (await client.call_tool("add", {"left": 1, "right": 2})).structuredContent == {"total": 3}
                    failure = await client.call_tool("add", {"left": 99, "right": 2})
                    assert failure.isError and failure.content[0].text == "tool_execution_failed"
                    invalid = await client.call_tool("add", {"left": "private caller marker", "right": 2})
                    assert invalid.isError and "caller marker" not in str(invalid)
                    session_id = get_session()
                    assert session_id in manager._server_instances
            assert session_id not in manager._server_instances
            response = await http.delete(endpoint + "/mcp", headers={"mcp-session-id": session_id, "mcp-protocol-version": "2025-11-25"})
            assert response.status_code == 404


def test_http_factory_blocks_extra_capabilities_before_readiness():
    selected = server()
    @selected.list_resources()
    async def resources():
        return []
    with pytest.raises(ValueError, match="unsupported_mcp_capability"):
        protect_server(selected)
    with pytest.raises(ValueError, match="http_factory_contract_required"):
        protect_server(object())


@pytest.mark.parametrize("field,value", [
    ("generation", True), ("nonce", "wrong"), ("api_key", "short"),
    ("endpoint", {"host": "0.0.0.0", "port": 1234}),
    ("model", {}), ("dependencies", {"external": {}}),
    ("lifecycle", {"max_sessions": 65}), ("lifecycle", {"max_request_bytes": 0}),
])
def test_runtime_descriptor_rejects_unowned_identity_and_unbounded_policy(field, value):
    config = {"endpoint": {"host": "127.0.0.1", "port": 1234}, "api_key": "k" * 64, "nonce": "a" * 64,
              "generation": 1, "model": None, "dependencies": {}}
    config[field] = value
    reader, writer = os.pipe()
    os.write(writer, json.dumps(config).encode())
    os.close(writer)
    with pytest.raises(ValueError, match="invalid_http_configuration"):
        read_config(reader)
    with pytest.raises(OSError):
        os.fstat(reader)


def smoke_tools():
    return [{"name": "add", "description": "Add two synthetic integers.",
             "input_schema": {"type": "object", "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                              "required": ["left", "right"], "additionalProperties": False},
             "output_schema": {"type": "object", "properties": {"total": {"type": "integer"}},
                               "required": ["total"], "additionalProperties": False}}]


def test_smoke_fixtures_are_static_and_bound_to_declared_tool_schemas(tmp_path, monkeypatch):
    package = http_package(tmp_path)
    edit_manifest(package, tools=smoke_tools(), validation={"fixtures": [
        {"tool": "add", "arguments": {"left": 1, "right": 2}, "expected": {"total": 3}}]})
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("inspection cannot execute a smoke fixture"))
    validated = validate_package(package)
    assert validated.manifest.validation.fixtures[0].expected == {"total": 3}


@pytest.mark.parametrize("fixture", [
    {"tool": "unknown", "arguments": {"left": 1, "right": 2}, "expected": {"total": 3}},
    {"tool": "add", "arguments": {"left": "invalid", "right": 2}, "expected": {"total": 3}},
    {"tool": "add", "arguments": {"left": 1, "right": 2}, "expected": {"total": "invalid"}},
    {"tool": "add", "arguments": {"left": 1, "right": 2, "extra": 3}, "expected": {"total": 3}},
])
def test_smoke_fixture_schema_mismatches_block_inspection(tmp_path, fixture):
    package = http_package(tmp_path)
    edit_manifest(package, tools=smoke_tools(), validation={"fixtures": [fixture]})
    with pytest.raises(PackageError, match="invalid_smoke_fixture"):
        validate_package(package)


@pytest.mark.parametrize("validation,code", [
    ({"fixtures": [{"tool": "validate_json", "arguments": {}, "expected": {}}] * 5}, "invalid_package_schema"),
    ({"fixtures": [{"tool": "validate_json", "arguments": {"value": "x" * 16384}, "expected": {}}]}, "smoke_fixture_limit"),
    ({"fixtures": [], "command": "arbitrary"}, "invalid_package_schema"),
])
def test_smoke_fixtures_have_closed_bounded_metadata(tmp_path, validation, code):
    package = http_package(tmp_path)
    edit_manifest(package, validation=validation)
    with pytest.raises(PackageError, match=code):
        validate_package(package)


def test_http_generator_consumes_source_export_without_starting_factory(tmp_path, monkeypatch):
    export, exported, tool = fixture_export(tmp_path)
    export["requirements"]["profile"] = "mcp-http"
    exported.write_text(json.dumps(export))
    args = preparation.parser().parse_args(["existing-mcp", "--setup-export", str(exported), "--tool-spec", str(tool),
        "--module", "arithmetic", "--http-factory", "create_server", "--source-license", "LICENSE",
        "--package-id", "local.http", "--name", "HTTP", "--output", str(tmp_path / "package")])
    monkeypatch.setattr(preparation, "prepare_runtime", stub_runtime)
    package = preparation.prepare(args)
    assert package.manifest.profile == "mcp-http"
    assert package.manifest.source_revision == export["source"]["revision"]
    assert package.manifest.entrypoint is None and len(package.manifest.processes) == 1
    assert (package.root / "http_runtime.py").is_file() and (package.root / "http_policy.py").is_file()
    assert json.loads((package.root / "http-factory.json").read_bytes()) == {"module": "arithmetic", "factory": "create_server"}
    assert "http_runtime.py" in (package.root / "bootstrap.py").read_text()
    assert "meecp" not in (package.root / "bootstrap.py").read_text()


def test_http_generator_seals_typed_settings_and_synthetic_smoke_declarations(tmp_path, monkeypatch):
    export, exported, tool = fixture_export(tmp_path)
    export["requirements"]["profile"] = "mcp-http"
    export["requirements"]["configuration"] = {"factor": 3}
    exported.write_text(json.dumps(export))
    configuration = tmp_path / "configuration.json"
    configuration.write_text(json.dumps({"fields": {"factor": {"type": "integer", "default": 2, "minimum": 1, "maximum": 4}}}))
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({"fixtures": [{"tool": "add_numbers", "arguments": {"left": 1, "right": 2}, "expected": {"total": 3}}]}))
    args = preparation.parser().parse_args(["existing-mcp", "--setup-export", str(exported), "--tool-spec", str(tool),
        "--module", "arithmetic", "--http-factory", "create_server", "--source-license", "LICENSE",
        "--configuration-spec", str(configuration), "--validation-spec", str(validation),
        "--package-id", "local.http", "--name", "HTTP", "--output", str(tmp_path / "package")])
    monkeypatch.setattr(preparation, "prepare_runtime", stub_runtime)
    package = preparation.prepare(args)
    assert package.manifest.configuration.fields["factor"].default == 2
    assert package.manifest.validation.fixtures[0].arguments == {"left": 1, "right": 2}
    assert package.manifest.source_revision == export["source"]["revision"]
    assert "snapshot_path" not in (package.root / "preparation.json").read_text()


@pytest.mark.parametrize("selection", [
    ["--script", "arithmetic.py", "--http-factory", "create_server"],
    ["--executable", "arithmetic.py", "--http-factory", "create_server"],
    ["--module", "arithmetic", "--http-factory", "invalid.factory"],
    ["--module", "arithmetic", "--http-factory", "create_server", "--entrypoint-arg=--port=1"],
])
def test_http_generator_rejects_uninjectable_execution_choices_before_build(tmp_path, selection, monkeypatch):
    export, exported, tool = fixture_export(tmp_path)
    export["requirements"]["profile"] = "mcp-http"
    exported.write_text(json.dumps(export))
    args = preparation.parser().parse_args(["existing-mcp", "--setup-export", str(exported), "--tool-spec", str(tool),
        *selection, "--source-license", "LICENSE", "--package-id", "local.http", "--name", "HTTP",
        "--output", str(tmp_path / "package")])
    monkeypatch.setattr(preparation, "prepare_runtime", lambda *args: pytest.fail("invalid HTTP choice cannot build or execute"))
    with pytest.raises(preparation.PreparationError, match="http_factory_contract_required"):
        preparation.prepare(args)
    assert not (tmp_path / "package").exists()
