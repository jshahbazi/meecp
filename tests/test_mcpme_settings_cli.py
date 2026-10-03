"""Owner CLI reads private inputs and emits secret metadata only."""
import json
import os
from pathlib import Path
import uuid

import pytest

from mcpme import __main__ as cli


def private_file(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.write_bytes(content)
    path.chmod(0o600)
    return path


async def test_secret_create_reads_private_file_without_value_argument(monkeypatch, tmp_path):
    path = private_file(tmp_path, "private input", b"synthetic value\n")
    identity = str(uuid.uuid4())
    calls = []

    async def request(state, method, route, body):
        calls.append((state, method, route, body))
        return {"secret_id": identity, "name": "service key", "status": "active",
                "created_at": 1, "updated_at": 1, "value": body["value"], "sha256": "unsafe", "length": 16}

    monkeypatch.setattr(cli, "call", request)
    arguments = cli.parser().parse_args(["--state-dir", str(tmp_path), "secrets", "create",
        "--name", "service key", "--file", str(path), "--json"])
    result = await cli.execute(arguments)
    assert calls == [(tmp_path, "POST", "/v1/secrets", {"name": "service key", "value": "synthetic value\n"})]
    assert result == {"secret_id": identity, "name": "service key", "status": "active", "created_at": 1, "updated_at": 1}
    assert "synthetic value" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink", "directory", "empty", "large", "non_utf8", "ancestor_symlink"])
async def test_unsafe_secret_input_never_calls_host(monkeypatch, tmp_path, kind):
    path = private_file(tmp_path, "input", b"synthetic")
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "symlink"
        link.symlink_to(path)
        path = link
    elif kind == "hardlink":
        os.link(path, tmp_path / "other link")
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif kind == "empty":
        path.write_bytes(b"")
    elif kind == "large":
        path.write_bytes(b"x" * 4097)
    elif kind == "non_utf8":
        path.write_bytes(b"\xff")
    elif kind == "ancestor_symlink":
        parent = tmp_path / "alias"
        parent.symlink_to(tmp_path, target_is_directory=True)
        path = parent / path.name

    async def request(*args, **kwargs):
        raise AssertionError("unsafe input reached host")

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["secrets", "create", "--name", "key", "--file", str(path)]))
    assert result == {"error": {"code": "invalid_secret_file"}}


@pytest.mark.parametrize("action,method,suffix,body", [
    ("list", "GET", "", None), ("inspect", "GET", "/ID", None), ("revoke", "POST", "/ID/revoke", {})])
async def test_secret_metadata_routes(monkeypatch, tmp_path, action, method, suffix, body):
    identity = str(uuid.uuid4())
    record = {"secret_id": identity, "name": "key", "status": "active", "value": "never output", "length": 4}

    async def request(state, actual_method, route, actual_body):
        assert (state, actual_method, route, actual_body) == (tmp_path, method, "/v1/secrets" + suffix.replace("ID", identity), body)
        return {"secrets": [record], "count": 1} if action == "list" else record

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "secrets", action]
    if action != "list":
        arguments.append(identity)
    result = await cli.execute(cli.parser().parse_args(arguments))
    assert "never output" not in json.dumps(result)
    if action == "list":
        assert result == {"secrets": [{"secret_id": identity, "name": "key", "status": "active"}], "count": 1}
    else:
        assert result == {"secret_id": identity, "name": "key", "status": "active"}


@pytest.mark.parametrize("action", ["create", "list", "inspect", "revoke"])
async def test_secret_commands_preserve_explicit_grant(monkeypatch, tmp_path, action):
    credential = tmp_path / "grant.json"
    path = private_file(tmp_path, "secret", b"synthetic")

    async def request(state, method, route, body, *, credential_file):
        assert state == tmp_path and credential_file == credential
        assert route.startswith("/v1/secrets")
        return {"error": {"code": "grant_scope", "detail": "never output"}}

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "--credential-file", str(credential), "secrets", action]
    if action == "create":
        arguments.extend(["--name", "key", "--file", str(path)])
    elif action != "list":
        arguments.append(str(uuid.uuid4()))
    assert await cli.execute(cli.parser().parse_args(arguments)) == {"error": {"code": "grant_scope"}}


async def test_install_plan_submits_private_settings(monkeypatch, tmp_path):
    settings = {"schema_version": 1, "configuration": {"greeting": "Hello", "enabled": True},
                "secret_references": {"service_key": str(uuid.uuid4())}}
    path = private_file(tmp_path, "settings.json", json.dumps(settings).encode())
    package = tmp_path / "package"

    async def request(state, method, route, body):
        assert (state, method, route) == (tmp_path, "POST", "/v1/install/plan")
        assert body == {"package": str(package), "start_policy": "manual", "start": False, "settings": settings}
        return {"plan_id": str(uuid.uuid4())}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "install", "plan",
        "--package", str(package), "--settings", str(path)]))
    assert "plan_id" in result


@pytest.mark.parametrize("content,mode", [(b'{}', 0o644), (b'{}' + b' ' * 65535, 0o600),
    (b'{"configuration":{},"configuration":{}}', 0o600), (b'[]', 0o600), (b'{', 0o600)])
async def test_invalid_settings_file_never_calls_host(monkeypatch, tmp_path, content, mode):
    path = private_file(tmp_path, "settings.json", content)
    path.chmod(mode)

    async def request(*args, **kwargs):
        raise AssertionError("invalid settings reached host")

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["install", "plan", "--package", str(tmp_path), "--settings", str(path)]))
    assert result == {"error": {"code": "invalid_settings_file"}}


async def test_import_plan_cannot_override_draft_settings(monkeypatch, tmp_path):
    async def request(*args, **kwargs):
        raise AssertionError("import override reached host")

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["install", "plan", "--import", str(uuid.uuid4()),
        "--revision", "1", "--settings", str(tmp_path / "missing.json")]))
    assert result == {"error": {"code": "invalid_import_plan"}}


@pytest.mark.parametrize("action", ["inspect", "revoke"])
def test_secret_identity_must_be_canonical_uuid(action):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["secrets", action, "../secret"])


def test_secret_value_cannot_be_given_on_command_line(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["secrets", "create", "--name", "key", "--file", str(tmp_path), "--value", "synthetic"])
    captured = capsys.readouterr()
    assert "invalid_secret_arguments" in captured.err
    assert "synthetic" not in captured.err and "synthetic" not in captured.out


async def test_import_requirements_accept_configuration_and_private_references(monkeypatch, tmp_path):
    identity = str(uuid.uuid4())
    requirements = {"configuration": {"greeting": "Hello"}, "secret_references": {"service_key": str(uuid.uuid4())}}
    path = private_file(tmp_path, "requirements.json", json.dumps(requirements).encode())

    async def request(state, method, route, body):
        assert (method, route, body) == ("POST", "/v1/imports/" + identity + "/revise", {"revision": 1, "requirements": requirements})
        return {"draft_id": identity}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["imports", "revise", identity, "--revision", "1", "--requirements", str(path)]))
    assert result == {"draft_id": identity}


async def test_import_references_require_private_file(monkeypatch, tmp_path):
    path = private_file(tmp_path, "requirements.json", json.dumps({"secret_references": {"service_key": str(uuid.uuid4())}}).encode())
    path.chmod(0o644)

    async def request(*args, **kwargs):
        raise AssertionError("public references reached host")

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["imports", "revise", str(uuid.uuid4()), "--revision", "1", "--requirements", str(path)]))
    assert result == {"error": {"code": "invalid_requirements_file"}}


async def test_legacy_nonsecret_requirements_do_not_require_private_mode(monkeypatch, tmp_path):
    path = private_file(tmp_path, "requirements.json", b'{"profile":"mcp-stdio"}')
    path.chmod(0o644)

    async def request(state, method, route, body):
        assert body["requirements"] == {"profile": "mcp-stdio"}
        return {"accepted": True}

    monkeypatch.setattr(cli, "call", request)
    assert await cli.execute(cli.parser().parse_args(["imports", "revise", str(uuid.uuid4()), "--revision", "1", "--requirements", str(path)])) == {"accepted": True}


async def test_legacy_secret_name_list_preserves_draft_blocker_flow(monkeypatch, tmp_path):
    path = private_file(tmp_path, "requirements.json", b'{"secret_references":["service_key"]}')
    path.chmod(0o644)

    async def request(state, method, route, body):
        assert body["requirements"] == {"secret_references": ["service_key"]}
        return {"state": "blocked", "blockers": ["secret_reference_unavailable"]}

    monkeypatch.setattr(cli, "call", request)
    result = await cli.execute(cli.parser().parse_args(["imports", "revise", str(uuid.uuid4()), "--revision", "1", "--requirements", str(path)]))
    assert result == {"state": "blocked", "blockers": ["secret_reference_unavailable"]}
