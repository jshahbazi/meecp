"""Owner upgrade/recovery commands bind exact identities and private inputs."""
import json
from pathlib import Path
import uuid

import pytest

from mcpme import __main__ as cli


def private_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("with_settings", [False, True])
async def test_upgrade_plan_submits_exact_slot_package_and_settings(monkeypatch, tmp_path, with_settings):
    slot_id, plan_id = str(uuid.uuid4()), str(uuid.uuid4())
    settings = {"schema_version": 1, "configuration": {"label": "Synthetic", "count": -2, "enabled": False},
                "secret_references": {"service_key": str(uuid.uuid4())}}
    source = tmp_path / "package with spaces"
    arguments = ["--state-dir", str(tmp_path), "upgrade", "plan", "--slot", slot_id,
                 "--package", str(source), "--json"]
    expected = {"slot_id": slot_id, "package": str(source)}
    if with_settings:
        path = private_json(tmp_path / "settings.json", settings)
        arguments.extend(["--settings", str(path)])
        expected["settings"] = settings

    async def request(state, method, route, body):
        assert (state, method, route, body) == (tmp_path, "POST", "/v1/upgrade/plan", expected)
        return {"plan_id": plan_id, "kind": "upgrade"}

    monkeypatch.setattr(cli, "call", request)
    assert await cli.execute(cli.parser().parse_args(arguments)) == {"plan_id": plan_id, "kind": "upgrade"}


@pytest.mark.parametrize("action,method,route,body", [
    ("inspect", "GET", "/v1/upgrade/plans/ID", None),
    ("apply", "POST", "/v1/upgrade/apply", {"plan_id": "ID"}),
])
async def test_upgrade_identity_routes(monkeypatch, tmp_path, action, method, route, body):
    plan_id = str(uuid.uuid4())

    async def request(state, actual_method, actual_route, actual_body):
        expected_body = {"plan_id": plan_id} if body else None
        assert (state, actual_method, actual_route, actual_body) == (tmp_path, method, route.replace("ID", plan_id), expected_body)
        return {"plan_id": plan_id}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "upgrade", action, "--plan", plan_id, "--json"]))
    assert result == {"plan_id": plan_id}


@pytest.mark.parametrize("action", ["plan", "inspect", "apply"])
async def test_upgrade_preserves_explicit_grant_without_owner_fallback(monkeypatch, tmp_path, action):
    identity = str(uuid.uuid4())
    credential = tmp_path / "scoped.json"
    calls = []

    async def request(state, method, route, body, *, credential_file):
        calls.append((state, credential_file))
        return {"error": {"code": "grant_scope"}}

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "--credential-file", str(credential), "upgrade", action]
    arguments += ["--slot", identity, "--package", str(tmp_path)] if action == "plan" else ["--plan", identity]
    assert await cli.execute(cli.parser().parse_args(arguments)) == {"error": {"code": "grant_scope"}}
    assert calls == [(tmp_path, credential)]


@pytest.mark.parametrize("kind", ["public", "symlink", "malformed", "duplicate", "oversized", "not_object"])
async def test_upgrade_rejects_unsafe_settings_before_host_call(monkeypatch, tmp_path, kind):
    path = private_json(tmp_path / "settings.json", {})
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "alias.json"
        link.symlink_to(path)
        path = link
    elif kind == "malformed":
        path.write_text("private malformed synthetic input")
    elif kind == "duplicate":
        path.write_text('{"configuration":{},"configuration":{}}')
    elif kind == "oversized":
        path.write_bytes(b" " * 65537)
    elif kind == "not_object":
        path.write_text("[]")

    async def request(*args, **kwargs):
        raise AssertionError("unsafe settings reached host")

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["upgrade", "plan", "--slot", str(uuid.uuid4()),
        "--package", str(tmp_path), "--settings", str(path)]))
    assert result == {"error": {"code": "invalid_settings_file"}}


@pytest.mark.parametrize("action,method,suffix,body", [
    ("get", "GET", "", None), ("cancel", "POST", "/cancel", {}), ("recover", "POST", "/recover", {}),
])
async def test_operation_routes_use_empty_mutation_body(monkeypatch, tmp_path, action, method, suffix, body):
    identity = str(uuid.uuid4())

    async def request(state, actual_method, route, actual_body):
        assert (state, actual_method, route, actual_body) == (tmp_path, method, "/v1/operations/" + identity + suffix, body)
        return {"operation_id": identity, "status": "recovery_required"}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "operations", action, identity, "--json"]))
    assert result["operation_id"] == identity


@pytest.mark.parametrize("action", ["get", "cancel", "recover"])
async def test_operation_commands_keep_explicit_grant(monkeypatch, tmp_path, action):
    credential = tmp_path / "scoped.json"

    async def request(state, method, route, body, *, credential_file):
        assert state == tmp_path and credential_file == credential
        return {"error": {"code": "grant_scope"}}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "--credential-file", str(credential),
        "operations", action, str(uuid.uuid4())]))
    assert result == {"error": {"code": "grant_scope"}}


@pytest.mark.parametrize("with_engine", [False, True])
async def test_model_create_includes_engine_only_when_selected(monkeypatch, tmp_path, with_engine):
    arguments = ["--state-dir", str(tmp_path), "imports", "create", "--source", "model.gguf", "--name", "Synthetic", "--json"]
    monkeypatch.chdir(tmp_path)
    expected = {"source": str(tmp_path / "model.gguf"), "name": "Synthetic", "intent": "", "start": False, "start_policy": "manual"}
    if with_engine:
        arguments.extend(["--model-engine", "engine build"])
        expected["model_engine"] = str(tmp_path / "engine build")

    async def request(state, method, route, body):
        assert (state, method, route, body) == (tmp_path, "POST", "/v1/imports", expected)
        return {"draft_id": str(uuid.uuid4())}

    monkeypatch.setattr(cli, "call", request)
    assert "draft_id" in await cli.execute(cli.parser().parse_args(arguments))


async def test_model_engine_resume_submits_revision_and_absolute_path(monkeypatch, tmp_path):
    identity = str(uuid.uuid4())
    monkeypatch.chdir(tmp_path)

    async def request(state, method, route, body):
        assert (state, method, route, body) == (tmp_path, "POST", "/v1/imports/" + identity + "/engine",
                                             {"revision": 3, "model_engine": str(tmp_path / "engine")})
        return {"draft_id": identity, "revision": 4}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "imports", "engine", identity,
        "--revision", "3", "--model-engine", "engine", "--json"]))
    assert result == {"draft_id": identity, "revision": 4}


@pytest.mark.parametrize("carry", [False, True])
async def test_import_revision_carry_is_explicit_and_preserves_primitive_choices(monkeypatch, tmp_path, carry):
    identity = str(uuid.uuid4())
    requirements = {"configuration": {"greeting": "Synthetic", "count": -1, "enabled": False}, "secret_references": {}}
    path = private_json(tmp_path / "requirements.json", requirements)
    expected = {"revision": 2, "requirements": requirements}
    arguments = ["imports", "revise", identity, "--revision", "2", "--requirements", str(path)]
    if carry:
        arguments.append("--carry-attachment")
        expected["carry_attachment"] = True

    async def request(state, method, route, body):
        assert (method, route, body) == ("POST", "/v1/imports/" + identity + "/revise", expected)
        return {"draft_id": identity}

    monkeypatch.setattr(cli, "call", request)
    assert await cli.execute(cli.parser().parse_args(arguments)) == {"draft_id": identity}


@pytest.mark.parametrize("arguments", [
    ["upgrade", "plan", "--slot", "../slot", "--package", "."],
    ["upgrade", "inspect", "--plan", "../plan"], ["upgrade", "apply", "--plan", "../plan"],
    ["operations", "cancel", "../operation"], ["operations", "recover", "../operation"],
    ["imports", "engine", "../draft", "--revision", "1", "--model-engine", "."],
    ["imports", "engine", str(uuid.uuid4()), "--revision", "1"],
])
def test_new_commands_require_closed_canonical_identity_arguments(arguments):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(arguments)


@pytest.mark.parametrize("route,timeout", [
    ("/v1/upgrade/plan", 300), ("/v1/imports", 300), ("/v1/imports/ID/engine", 300),
    ("/v1/imports/ID/attach", 300), ("/v1/upgrade/apply", 30), ("/v1/operations/ID/recover", 30),
])
async def test_model_copy_and_upgrade_plans_keep_bounded_long_timeout(monkeypatch, tmp_path, route, timeout):
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self, chunk_size):
            yield b'{"accepted":true}'

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == timeout
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, json):
            assert method == "POST" and path == route and json == {}
            return Response()

    monkeypatch.setattr(cli, "read_private_json", lambda path: {"endpoint": "http://127.0.0.1:12345", "token": "synthetic"})
    monkeypatch.setattr(cli, "private_transport", lambda path: object())
    monkeypatch.setattr(cli, "socket_path", lambda state, service: tmp_path / "synthetic.sock")
    monkeypatch.setattr(cli.httpx, "AsyncClient", Client)
    assert await cli.call(tmp_path, "POST", route, {}) == {"accepted": True}
