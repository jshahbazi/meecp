"""Fail-closed, integrity, and nonlogging contracts for the CI credential scanner."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib

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


def test_directory_scan_uses_same_exact_relative_paths_as_history(monkeypatch, tmp_path):
    captured = []
    def run(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)
    monkeypatch.setattr(scanner.subprocess, "run", run)
    scanner.run_scanner(tmp_path / "gitleaks", "dir", tmp_path, tmp_path / "config", tmp_path / "ignore")
    arguments, options = captured[0]
    assert arguments[1:3] == ["dir", "."] and options["cwd"] == tmp_path


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
        assert args[3].read_text() == scanner.trusted_config()
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


def test_public_metadata_exception_is_exact_value_and_path_for_one_rule_only():
    config = tomllib.loads(scanner.trusted_config())
    assert set(config) == {"extend", "rules"}
    assert config["extend"] == {"useDefault": True}
    assert len(config["rules"]) == 1
    rule = config["rules"][0]
    assert set(rule) == {"id", "allowlists"} and rule["id"] == "generic-api-key"
    assert len(rule["allowlists"]) == 1
    exception = rule["allowlists"][0]
    assert set(exception) == {"description", "condition", "regexTarget", "paths", "regexes"}
    assert exception["condition"] == "AND" and exception["regexTarget"] == "secret"
    assert len(exception["paths"]) == len(exception["regexes"]) == 1
    assert re.fullmatch("[0-9a-f]{64}", scanner.PUBLIC_GGUF_METADATA_DIGEST)
    for path in scanner.PUBLIC_GGUF_METADATA_PATHS:
        assert re.fullmatch(exception["paths"][0], path)
        assert not re.fullmatch(exception["paths"][0], "other/" + path)
        assert not re.fullmatch(exception["paths"][0], path + ".backup")
    assert re.fullmatch(exception["regexes"][0], scanner.PUBLIC_GGUF_METADATA_DIGEST)
    assert not re.fullmatch(exception["regexes"][0], scanner.PUBLIC_GGUF_METADATA_DIGEST + "0")
    assert not re.fullmatch(exception["regexes"][0], "0" + scanner.PUBLIC_GGUF_METADATA_DIGEST)


@pytest.fixture(scope="module")
def actual_scanner(tmp_path_factory):
    if os.environ.get("MCPME_RUN_SECRET_SCANNER_INTEGRATION") != "1":
        pytest.skip("explicit pinned scanner integration run required")
    workspace = tmp_path_factory.mktemp("actual-scanner")
    binary = scanner.scanner_binary(workspace)
    config = workspace / "trusted.toml"
    config.write_text(scanner.trusted_config())
    ignore = workspace / "ignore"
    ignore.mkdir()
    return binary, config, ignore


@pytest.mark.parametrize("path", scanner.PUBLIC_GGUF_METADATA_PATHS)
@pytest.mark.parametrize("case", ["reviewed", "different_value", "same_value_other_path", "extra_value_same_file"])
@pytest.mark.parametrize("mode", ["dir", "git"])
def test_actual_pinned_scanner_preserves_detection_at_excepted_paths(actual_scanner, tmp_path, path, case, mode, capsys):
    binary, config, ignore = actual_scanner
    value = scanner.PUBLIC_GGUF_METADATA_DIGEST
    synthetic = hashlib.sha256(b"mcpme non-credential scanner negative fixture").hexdigest()
    if case == "different_value": value = synthetic
    if case == "same_value_other_path": path = "unreviewed/" + path
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    content = 'TOKENIZER_SHA256 = "' + value + '"\n'
    if case == "extra_value_same_file": content += 'API_KEY = "' + synthetic + '"\n'
    target.write_text(content)
    if mode == "git":
        for arguments in (("init", "--quiet"), ("add", "."),
                          ("-c", "user.name=Scanner test", "-c", "user.email=scanner@example.invalid",
                           "commit", "--quiet", "-m", "test: synthetic scanner fixture")):
            subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if case == "reviewed":
        scanner.run_scanner(binary, mode, tmp_path, config, ignore)
    else:
        with pytest.raises(scanner.ScanFailure, match="secret_scan_detected_credentials"):
            scanner.run_scanner(binary, mode, tmp_path, config, ignore)
    assert capsys.readouterr() == ("", "")
