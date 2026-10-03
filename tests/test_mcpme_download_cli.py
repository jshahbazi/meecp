"""Pinned download commands preserve owner transport and explicit grant denial."""
from pathlib import Path
import uuid

import pytest

from mcpme import __main__ as cli


URL = "https://packages.example.test/app.zip"
SHA256 = "ab" * 32
PACKAGE_DIGEST = "cd" * 32


@pytest.mark.parametrize("package_digest", [None, PACKAGE_DIGEST])
async def test_fetch_submits_pins_and_returns_durable_job(monkeypatch, tmp_path: Path, package_digest):
    download_id = str(uuid.uuid4())
    calls = []

    async def request(state, method, path, body):
        calls.append((state, method, path, body))
        return {"download_id": download_id, "status": "pending", "received_bytes": 0}

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "downloads", "fetch", "--url", URL,
                 "--sha256", SHA256, "--size", "512", "--json"]
    expected = {"url": URL, "sha256": SHA256, "size": 512}
    if package_digest is not None:
        arguments.extend(["--package-digest", package_digest])
        expected["package_digest"] = package_digest
    result = await cli.execute(cli.parser().parse_args(arguments))
    assert result == {"download_id": download_id, "status": "pending", "received_bytes": 0}
    assert calls == [(tmp_path, "POST", "/v1/downloads", expected)]


@pytest.mark.parametrize("action,method,suffix,body", [
    ("list", "GET", "", None),
    ("inspect", "GET", "/ID", None),
    ("cancel", "POST", "/ID/cancel", {}),
])
async def test_download_metadata_routes(monkeypatch, tmp_path, action, method, suffix, body):
    download_id = str(uuid.uuid4())
    route = "/v1/downloads" + suffix.replace("ID", download_id)

    async def request(state, actual_method, path, actual_body):
        assert (state, actual_method, path, actual_body) == (tmp_path, method, route, body)
        return {"downloads": []} if action == "list" else {"download_id": download_id, "status": "cancelled"}

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "downloads", action]
    if action != "list":
        arguments.append(download_id)
    assert "error" not in await cli.execute(cli.parser().parse_args(arguments))


@pytest.mark.parametrize("action", ["fetch", "list", "inspect", "cancel"])
async def test_download_commands_preserve_explicit_scoped_credential(monkeypatch, tmp_path, action):
    credential = tmp_path / "grant.json"

    async def request(state, method, path, body, *, credential_file):
        assert state == tmp_path and credential_file == credential
        assert path.startswith("/v1/downloads")
        return {"error": {"code": "grant_scope"}}

    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "--credential-file", str(credential), "downloads", action]
    if action == "fetch":
        arguments.extend(["--url", URL, "--sha256", SHA256, "--size", "512"])
    elif action != "list":
        arguments.append(str(uuid.uuid4()))
    assert (await cli.execute(cli.parser().parse_args(arguments)))["error"]["code"] == "grant_scope"


@pytest.mark.parametrize("argument,value", [
    ("--sha256", "AB" * 32), ("--sha256", "a" * 63), ("--sha256", "g" * 64),
    ("--package-digest", "CD" * 32), ("--package-digest", ""),
    ("--size", "0"), ("--size", "-1"), ("--size", "1.5"),
])
def test_fetch_rejects_invalid_pins(argument, value):
    arguments = ["downloads", "fetch", "--url", URL, "--sha256", SHA256, "--size", "512"]
    if argument == "--package-digest":
        arguments.extend([argument, value])
    else:
        arguments[arguments.index(argument) + 1] = value
    with pytest.raises(SystemExit):
        cli.parser().parse_args(arguments)


@pytest.mark.parametrize("action", ["inspect", "cancel"])
def test_download_identity_must_be_canonical_uuid(action):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["downloads", action, "../other-record"])
