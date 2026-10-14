"""Fail-closed, integrity, and nonlogging contracts for the CI credential scanner."""
import hashlib
import importlib.util
import io
import json
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
    assert len(rule["allowlists"]) == 6
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
    stark = rule["allowlists"][1]
    assert stark["condition"] == "AND" and stark["regexTarget"] == "secret"
    assert stark["paths"] == [r"^(?:engines/STARK_VALIDATION\.json|engines/STARK_JEVK5_VALIDATION\.json)$"]
    assert stark["regexes"] == [r"^(?:06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523|9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d)$"]


def test_current_textgen_recipe_metadata_has_an_exact_reviewed_scan_path():
    root = SCRIPT.parents[2]
    catalog = json.loads((root / "templates/builtin-recipes/catalog.json").read_bytes())
    recipe = next(item for item in catalog["recipes"] if item["recipe_id"] == "mcpme.text-generation")
    relative = (Path("templates/builtin-recipes") / recipe["directory"] / "model-provenance.json").as_posix()
    assert relative in scanner.PUBLIC_GGUF_METADATA_PATHS
    metadata = json.loads((root / relative).read_bytes())
    assert metadata["tokenizer_sha256"] == scanner.PUBLIC_GGUF_METADATA_DIGEST


def test_historical_metadata_exceptions_are_only_the_verified_value_path_pairs():
    # Fingerprints of the independently verified public digests prevent this
    # test from silently following a changed exception value.
    expected = (
        (
            ("experiments/uzu-rust/jev-scoring/RESULTS.json",
             "experiments/uzu-rust/jev-scoring/RESULTS_BF16.json",
             "experiments/uzu-rust/jev-scoring/RESULTS_INITIAL.json"),
            ("eea2847a2ab2cf5f3cfd567a72bbbbd6d451d943e74cf18e20e8c1231e813325",
             "fb65dde9dceafaaf8287bf5a7032a03fe4672b760a4174f18a38e6281a51412a"),
        ),
        (
            ("experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json",
             "experiments/uzu-rust/jev-scoring/RESULTS_BF16.json"),
            ("6c81ed19c5cd927b777125c0b0d035e84a4c597047a5f44f270c04b26275daad",
             "6f734ae48e4317a19204d817316d0df8cd24617915aac9f7f058062d41eb35a0",
             "cf5cb5f3cff8a4cc098b9d93ffea397e3b25c9ee8dbbf526c2b8da71b05ba0da",
             "08ae7b3c605653a13a166d952e265332eee0581349c2f5819d79370e2171202d",
             "bb1f03103c06beb1194511d0389c9ef25fd17c025736f1a5659b2f195c12b038",
             "d637ee2f3d3e6df209a76ad564ae957023bdd4940c852e90ca03292f5981827b"),
        ),
        (
            ("experiments/uzu-rust/BENCHMARK_RESULTS.json",),
            ("db8c2ba0ae12ae7151b5cbdf944ab3a56c892d3f60305167e6b1c5513b82e728",
             "8fa8b6249fef02b1cc47431b140cd3f0e7f36674d9996b99d41df096f5857136",
             "92d903bc6c1aec571973e96cff99e09635a324da3a09942c7d591d5ba5a39bf5"),
        ),
        (
            ("experiments/uzu-rust/QUALITY_RESULTS.json",),
            ("0bdde2f133baaddc549a055b3382ac4a23be0d9889c05fd0067f8024f1041da7",),
        ),
    )
    groups = scanner.PUBLIC_UZU_METADATA_EXCEPTIONS
    config = tomllib.loads(scanner.trusted_config())
    rule = config["rules"][0]
    assert rule["id"] == "generic-api-key"
    assert len(groups) == len(expected) == 4
    assert len(rule["allowlists"]) == 6
    assert sum(len(paths) * len(digests) for _, paths, digests in groups) == 22
    for (description, paths, digests), (expected_paths, fingerprints), exception in zip(
            groups, expected, rule["allowlists"][2:], strict=True):
        assert paths == expected_paths
        assert tuple(hashlib.sha256(value.encode()).hexdigest() for value in digests) == fingerprints
        assert all(re.fullmatch("[0-9a-f]{64}", value) for value in digests)
        assert exception == {
            "description": description, "condition": "AND", "regexTarget": "secret",
            "paths": [r"^(?:" + "|".join(re.escape(path) for path in paths) + r")$"],
            "regexes": [r"^(?:" + "|".join(re.escape(value) for value in digests) + r")$"],
        }
        for path in paths:
            assert re.fullmatch(exception["paths"][0], path)
            assert not re.fullmatch(exception["paths"][0], "unreviewed/" + path)
            assert not re.fullmatch(exception["paths"][0], path + ".backup")
        for value in digests:
            assert re.fullmatch(exception["regexes"][0], value)
            assert not re.fullmatch(exception["regexes"][0], value + "0")
            assert not re.fullmatch(exception["regexes"][0], "0" + value)


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


@pytest.mark.parametrize("case", ["reviewed", "different_value", "same_value_other_path", "extra_value_same_file"])
@pytest.mark.parametrize("mode", ["dir", "git"])
@pytest.mark.parametrize("metadata_index", [0, 1])
@pytest.mark.parametrize("reviewed_path", [scanner.PUBLIC_STARK_MODEL_FILES_PATH,
                                        scanner.PUBLIC_STARK_INTEGRATION_MODEL_FILES_PATH])
def test_actual_pinned_scanner_preserves_detection_at_stark_tokenizer_paths(actual_scanner, tmp_path, case, mode, metadata_index, reviewed_path, capsys):
    binary, config, ignore = actual_scanner
    path = reviewed_path
    value = scanner.PUBLIC_STARK_MODEL_FILES_SHA256[metadata_index]
    synthetic = hashlib.sha256(b"stark non-credential scanner negative fixture").hexdigest()
    if case == "different_value": value = synthetic
    if case == "same_value_other_path": path = "unreviewed/STARK_VALIDATION.json"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    key = ("tokenizer.json", "tokenizer_config.json")[metadata_index]
    content = '"' + key + '": "' + value + '"\n'
    if case == "extra_value_same_file": content += 'API_KEY = "' + synthetic + '"\n'
    target.write_text(content)
    if mode == "git":
        for arguments in (("init", "--quiet"), ("add", "."),
                          ("-c", "user.name=Scanner test", "-c", "user.email=scanner@example.invalid",
                           "commit", "--quiet", "-m", "test: synthetic scanner fixture")):
            subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if case == "reviewed": scanner.run_scanner(binary, mode, tmp_path, config, ignore)
    else:
        with pytest.raises(scanner.ScanFailure, match="secret_scan_detected_credentials"):
            scanner.run_scanner(binary, mode, tmp_path, config, ignore)
    assert capsys.readouterr() == ("", "")


HISTORICAL_METADATA_PAIRS = [
    pytest.param((path, value), id=f"group-{group_index}-path-{path_index}-value-{value_index}")
    for group_index, (_, paths, digests) in enumerate(scanner.PUBLIC_UZU_METADATA_EXCEPTIONS)
    for path_index, path in enumerate(paths)
    for value_index, value in enumerate(digests)
]


@pytest.mark.parametrize("pair", HISTORICAL_METADATA_PAIRS)
@pytest.mark.parametrize("mode", ["dir", "git"])
@pytest.mark.parametrize("case", [
    "reviewed", "different_value", "other_group_value", "same_value_other_path", "extra_value_same_file",
])
def test_actual_pinned_scanner_preserves_detection_for_historical_metadata(
        actual_scanner, tmp_path, pair, mode, case, capsys):
    binary, config, ignore = actual_scanner
    path, value = pair
    synthetic = hashlib.sha256(b"historical metadata scanner negative fixture").hexdigest()
    if case == "different_value":
        value = synthetic
    elif case == "other_group_value":
        admitted = {digest for _, paths, digests in scanner.PUBLIC_UZU_METADATA_EXCEPTIONS
                    if path in paths for digest in digests}
        value = next(digest for _, _, digests in scanner.PUBLIC_UZU_METADATA_EXCEPTIONS
                     for digest in digests if digest not in admitted)
    elif case == "same_value_other_path":
        path = "unreviewed/" + path
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    # Use a scanner-triggering declaration so a passing reviewed fixture cannot
    # be explained by a source line that never matched the built-in rule.
    content = 'API_KEY = "' + value + '"\n'
    if case == "extra_value_same_file":
        content += 'API_KEY = "' + synthetic + '"\n'
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
