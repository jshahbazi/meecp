"""Check release credential failure and cleanup boundaries using synthetic data."""

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

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
    assert not (tmp_path / "meecp-signing").exists()


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


def test_search_list_capture_never_logs_tool_output(credentials, monkeypatch, capsys):
    calls = []

    def listing(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout='"/synthetic runner/login.keychain-db"\n')

    monkeypatch.setattr(credentials.subprocess, "run", listing)
    assert credentials.capture("synthetic-command") == '"/synthetic runner/login.keychain-db"\n'
    assert calls[0]["stdout"] == subprocess.PIPE
    assert calls[0]["stderr"] == subprocess.DEVNULL
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert calls[0]["check"] is True
    assert capsys.readouterr() == ("", "")


def test_registration_saves_private_original_list_before_mutation(credentials, monkeypatch):
    root = credentials.workspace()
    root.mkdir(mode=0o700)
    keychain = root / "signing.keychain-db"
    original = ["/synthetic runner/login.keychain-db", "/synthetic/library/system.keychain"]
    monkeypatch.setattr(credentials, "capture", lambda *args: '\n'.join(json.dumps(path) for path in original))
    calls = []

    def check_state(*arguments):
        state = root / credentials.SEARCH_LIST_FILENAME
        assert state.stat().st_mode & 0o777 == 0o600
        assert json.loads(state.read_text()) == original
        calls.append(arguments)

    monkeypatch.setattr(credentials, "run", check_state)
    credentials.register_keychain(root, keychain)
    assert calls == [
        ("/usr/bin/security", "list-keychains", "-d", "user", "-s", str(keychain), *original)
    ]


def test_setup_saves_search_list_before_keychain_creation(credentials, monkeypatch):
    for name in credentials.REQUIRED:
        monkeypatch.setenv(name, "c3ludGhldGlj")
    monkeypatch.setenv("MACOS_SIGNING_IDENTITY", "Developer ID Application: Synthetic (TEAM)")
    monkeypatch.setattr(credentials, "capture", lambda *args: '"/synthetic/login.keychain-db"\n')
    monkeypatch.setattr(credentials, "preflight_signing", lambda keychain: None)
    calls = []

    def check_state(*arguments):
        assert credentials.read_search_list(credentials.workspace()) == ["/synthetic/login.keychain-db"]
        calls.append(arguments)

    monkeypatch.setattr(credentials, "run", check_state)
    credentials.setup()
    assert calls[0][:2] == ("/usr/bin/security", "create-keychain")
    registration = next(call for call in calls if call[1] == "list-keychains")
    assert registration[-1] == "/synthetic/login.keychain-db"
    assert not (credentials.workspace() / "certificate.p12").exists()
    assert not (credentials.workspace() / "AuthKey.p8").exists()


def test_registration_reuses_original_snapshot_after_partial_setup(credentials, monkeypatch):
    root = credentials.workspace()
    root.mkdir()
    original = ["/synthetic original/login.keychain-db"]
    credentials.write_private(root / credentials.SEARCH_LIST_FILENAME, json.dumps(original).encode())
    monkeypatch.setattr(credentials, "capture", lambda *args: pytest.fail("must not replace original snapshot"))
    calls = []
    monkeypatch.setattr(credentials, "run", lambda *args: calls.append(args))
    credentials.register_keychain(root, root / "signing.keychain-db")
    assert calls[0][-1] == original[0]
    assert credentials.read_search_list(root) == original


@pytest.mark.parametrize("original", [[], ["/synthetic runner/login.keychain-db", "/synthetic/library/system.keychain"]])
def test_cleanup_restores_exact_original_list_and_removes_workspace(credentials, monkeypatch, original):
    root = credentials.workspace()
    root.mkdir()
    keychain = root / "signing.keychain-db"
    keychain.write_bytes(b"synthetic-keychain")
    credentials.write_private(root / credentials.SEARCH_LIST_FILENAME, json.dumps(original).encode())
    calls = []
    monkeypatch.setattr(credentials, "run", lambda *args: calls.append(args))
    credentials.cleanup()
    assert calls == [
        ("/usr/bin/security", "list-keychains", "-d", "user", "-s", *original),
        ("/usr/bin/security", "delete-keychain", str(keychain)),
    ]
    assert not root.exists()


def test_failed_restore_still_deletes_keychain_and_removes_private_files(credentials, monkeypatch, capsys):
    root = credentials.workspace()
    root.mkdir()
    keychain = root / "signing.keychain-db"
    keychain.write_bytes(b"synthetic-keychain")
    credentials.write_private(root / credentials.SEARCH_LIST_FILENAME, b'["/synthetic/login.keychain-db"]')
    (root / "certificate.p12").write_bytes(b"synthetic-private-data")
    calls = []

    def rejected_restore(*arguments):
        calls.append(arguments)
        if arguments[1] == "list-keychains":
            raise subprocess.CalledProcessError(1, "synthetic-secret-must-not-appear")

    monkeypatch.setattr(credentials, "run", rejected_restore)
    monkeypatch.setattr(credentials.sys, "argv", ["apple_credentials.py", "cleanup"])
    assert credentials.main() == 1
    assert calls[-1] == ("/usr/bin/security", "delete-keychain", str(keychain))
    assert not root.exists()
    assert capsys.readouterr() == ("", "apple_credentials_cleanup_failed\n")


def test_invalid_saved_search_list_still_deletes_keychain(credentials, monkeypatch):
    root = credentials.workspace()
    root.mkdir()
    keychain = root / "signing.keychain-db"
    keychain.write_bytes(b"synthetic-keychain")
    credentials.write_private(root / credentials.SEARCH_LIST_FILENAME, b'{"unexpected":"synthetic-data"}')
    calls = []
    monkeypatch.setattr(credentials, "run", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="keychain_search_list_invalid"):
        credentials.cleanup()
    assert calls == [("/usr/bin/security", "delete-keychain", str(keychain))]
    assert not root.exists()


@pytest.mark.parametrize("failed_step", ["clang", "--force", "--verify"])
def test_preflight_always_removes_probe_files_after_failure(credentials, monkeypatch, failed_step):
    root = credentials.workspace()
    root.mkdir()
    keychain = root / "signing.keychain-db"
    identity = "Developer ID Application: Synthetic (TEAM)"
    monkeypatch.setenv("MACOS_SIGNING_IDENTITY", identity)
    calls = []

    def fake_tool(*arguments):
        calls.append(arguments)
        if arguments[1] == "clang":
            (root / "signing-probe").write_bytes(b"synthetic-binary")
        if arguments[1] == failed_step:
            raise subprocess.CalledProcessError(1, "synthetic-preflight-failed")

    monkeypatch.setattr(credentials, "run", fake_tool)
    with pytest.raises(subprocess.CalledProcessError):
        credentials.preflight_signing(keychain)
    if failed_step != "clang":
        assert calls[1][:8] == (
            "/usr/bin/codesign", "--force", "--sign", identity,
            "--keychain", str(keychain), "--options", "runtime",
        )
    if failed_step == "--verify":
        assert calls[2][1:3] == ("--verify", "--strict")
    assert not (root / "signing-probe.c").exists()
    assert not (root / "signing-probe").exists()


def test_command_line_error_does_not_echo_credentials(credentials, monkeypatch, capsys):
    monkeypatch.setattr(credentials.sys, "argv", ["apple_credentials.py", "setup"])

    def rejected():
        raise ValueError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(credentials, "setup", rejected)
    assert credentials.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "apple_credentials_setup_failed\n"
