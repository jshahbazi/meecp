"""Check release credential failure and cleanup boundaries using synthetic data."""

import importlib.util
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / ".github/scripts/apple_credentials.py"
    spec = importlib.util.spec_from_file_location("apple_credentials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    for name in module.REQUIRED:
        monkeypatch.delenv(name, raising=False)
    return module


def test_missing_credentials_fail_before_any_keychain_creation(credentials, tmp_path):
    with pytest.raises(ValueError, match="signing_credentials_missing"):
        credentials.setup()
    assert not (tmp_path / "mcpme-signing").exists()


def test_credential_files_are_private_and_never_overwritten(credentials, tmp_path):
    target = tmp_path / "synthetic-key"
    credentials.write_private(target, b"synthetic-private-data")
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        credentials.write_private(target, b"replacement")
    assert target.read_bytes() == b"synthetic-private-data"


def test_cleanup_removes_files_even_when_keychain_deletion_fails(credentials, monkeypatch):
    root = credentials.workspace()
    root.mkdir()
    for name in ("signing.keychain-db", "certificate.p12", "AuthKey.p8"):
        (root / name).write_bytes(b"synthetic-private-data")

    def rejected(*arguments):
        raise subprocess.CalledProcessError(1, "synthetic-security-command")

    monkeypatch.setattr(credentials, "run", rejected)
    with pytest.raises(subprocess.CalledProcessError):
        credentials.cleanup()
    assert not root.exists()


def test_tool_output_is_suppressed(credentials, monkeypatch):
    calls = []
    monkeypatch.setattr(credentials.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    credentials.run("synthetic-command")
    assert calls[0]["stdout"] == subprocess.DEVNULL
    assert calls[0]["stderr"] == subprocess.DEVNULL
    assert calls[0]["stdin"] == subprocess.DEVNULL


def test_command_line_error_does_not_echo_credentials(credentials, monkeypatch, capsys):
    monkeypatch.setattr(credentials.sys, "argv", ["apple_credentials.py", "setup"])

    def rejected():
        raise ValueError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(credentials, "setup", rejected)
    assert credentials.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "apple_credentials_setup_failed\n"
