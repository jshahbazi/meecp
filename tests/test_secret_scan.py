"""Fail-closed, integrity, and nonlogging contracts for the CI credential scanner."""
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/scan_secrets.py"
SPEC = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


def test_download_checksum_mismatch_never_extracts_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"modified download"))
    with pytest.raises(scanner.ScanFailure, match="secret_scan_failed"):
        scanner.scanner_binary(tmp_path)
    assert not (tmp_path / "gitleaks").exists()


def test_only_regular_binary_is_extracted_from_verified_archive(monkeypatch, tmp_path):
    contents = io.BytesIO()
    with tarfile.open(fileobj=contents, mode="w:gz") as archive:
        binary = tarfile.TarInfo("gitleaks")
        binary.size = 4
        archive.addfile(binary, io.BytesIO(b"test"))
        escape = tarfile.TarInfo("../../escape")
        escape.size = 6
        archive.addfile(escape, io.BytesIO(b"unused"))
    data = contents.getvalue()
    monkeypatch.setattr(scanner, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(scanner.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    binary = scanner.scanner_binary(tmp_path)
    assert binary.read_bytes() == b"test"
    assert binary.stat().st_mode & 0o777 == 0o700
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gitleaks", "gitleaks.tar.gz"]


def test_archive_cannot_replace_binary_with_symlink(monkeypatch, tmp_path):
    contents = io.BytesIO()
    with tarfile.open(fileobj=contents, mode="w:gz") as archive:
        link = tarfile.TarInfo("gitleaks")
        link.type = tarfile.SYMTYPE
        link.linkname = "/usr/bin/true"
        archive.addfile(link)
    data = contents.getvalue()
    monkeypatch.setattr(scanner, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(scanner.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    with pytest.raises(scanner.ScanFailure, match="secret_scan_failed"):
        scanner.scanner_binary(tmp_path)
    assert not (tmp_path / "gitleaks").exists()


@pytest.mark.parametrize("exit_code,expected", [(0, None), (10, "secret_scan_detected_credentials"), (1, "secret_scan_failed")])
def test_tool_stdout_and_stderr_are_never_logged(tmp_path, capsys, exit_code, expected):
    binary = tmp_path / "fake-gitleaks"
    binary.write_text(f"#!{sys.executable}\nimport sys\nprint('private scanner output')\n"
                      f"print('private scanner diagnostics',file=sys.stderr)\nsys.exit({exit_code})\n")
    binary.chmod(0o700)
    arguments = (binary, "git", tmp_path, tmp_path / "config", tmp_path / "ignore")
    if expected:
        with pytest.raises(scanner.ScanFailure, match=expected):
            scanner.run_scanner(*arguments)
    else:
        scanner.run_scanner(*arguments)
    assert capsys.readouterr() == ("", "")


def test_history_scan_uses_all_refs_and_blocks_config_and_ignore_overrides(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setenv("GITLEAKS_CONFIG", "unsafe-config")
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "unsafe-inline-config")

    def run(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    scanner.run_scanner(tmp_path / "gitleaks", "git", tmp_path, tmp_path / "default.toml", tmp_path / "ignore")
    arguments, options = captured[0]
    assert "--log-opts=--all" in arguments
    assert "--redact=100" in arguments
    assert "--no-banner" in arguments
    assert "--ignore-gitleaks-allow" in arguments
    assert arguments[arguments.index("--config") + 1] == str(tmp_path / "default.toml")
    assert arguments[arguments.index("--gitleaks-ignore-path") + 1] == str(tmp_path / "ignore")
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert not any(key.startswith("GITLEAKS_") for key in options["env"])


def test_current_source_includes_tracked_edits_and_excludes_untracked_artifacts(monkeypatch, tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "tracked.py").write_text("current tracked source")
    (repository / "ignored-runtime.bin").write_text("untracked generated runtime")
    monkeypatch.setattr(scanner, "git_output", lambda *args: b"tracked.py\0deleted.py\0")
    snapshot = tmp_path / "snapshot"
    scanner.tracked_source(repository, snapshot)
    assert (snapshot / "tracked.py").read_text() == "current tracked source"
    assert sorted(path.name for path in snapshot.iterdir()) == ["tracked.py"]


def test_current_source_refuses_external_tracked_symlink(monkeypatch, tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside checkout")
    (repository / "tracked").symlink_to(outside)
    monkeypatch.setattr(scanner, "git_output", lambda *args: b"tracked\0")
    with pytest.raises(scanner.ScanFailure, match="secret_scan_failed"):
        scanner.tracked_source(repository, tmp_path / "snapshot")


def test_shallow_checkout_fails_before_download(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scanner.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(scanner, "git_output", lambda *args: b"true\n")
    downloads = []
    monkeypatch.setattr(scanner, "scanner_binary", lambda workspace: downloads.append(workspace))
    with pytest.raises(scanner.ScanFailure, match="secret_scan_failed"):
        scanner.scan(tmp_path)
    assert downloads == []


def test_temporary_binary_and_config_are_removed_on_scanner_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scanner.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(scanner, "git_output", lambda *args: b"false\n")
    workspaces = []

    def download(workspace):
        workspaces.append(workspace)
        binary = workspace / "gitleaks"
        binary.touch()
        return binary

    def failed(*args):
        assert args[3].read_text() == "[extend]\nuseDefault = true\n"
        raise scanner.ScanFailure("secret_scan_detected_credentials")

    monkeypatch.setattr(scanner, "scanner_binary", download)
    monkeypatch.setattr(scanner, "run_scanner", failed)
    with pytest.raises(scanner.ScanFailure, match="secret_scan_detected_credentials"):
        scanner.scan(tmp_path)
    assert len(workspaces) == 1
    assert not workspaces[0].exists()


@pytest.mark.parametrize("error,code,message", [
    (scanner.ScanFailure("secret_scan_detected_credentials"), 1, "secret_scan_detected_credentials"),
    (subprocess.CalledProcessError(2, ["private command"], stderr="private diagnostic"), 2, "secret_scan_failed"),
])
def test_cli_errors_contain_only_fixed_status(monkeypatch, capsys, error, code, message):
    def failed(repository):
        raise error

    monkeypatch.setattr(scanner, "scan", failed)
    assert scanner.main() == code
    assert capsys.readouterr() == (message + "\n", "")
