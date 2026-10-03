"""Build failures identify the tool without exposing credentials or tool output."""
from pathlib import Path
import hashlib
import json
import runpy
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_mcpme_host as host_builder  # noqa: E402


SECRET = "private-credential-and-path-value"


def fail_command(monkeypatch, *, returncode=1, stderr=SECRET):
    def check_output(arguments, **kwargs):
        assert kwargs == {"text": True, "stderr": subprocess.PIPE}
        raise subprocess.CalledProcessError(returncode, arguments, output=SECRET, stderr=stderr)

    monkeypatch.setattr(host_builder.subprocess, "check_output", check_output)


@pytest.mark.parametrize("arguments,operation", [
    (("/usr/bin/codesign", "--sign", SECRET), "codesign_sign"),
    (("/usr/bin/codesign", "--verify", "-R", SECRET), "codesign_verify"),
    (("/usr/bin/security", "find-identity", SECRET), "security_find_identity"),
    (("/usr/bin/xcrun", "notarytool", "submit", SECRET), "notarytool_submit"),
    (("/usr/bin/xcrun", "notarytool", "store-credentials", SECRET), "notarytool_store_credentials"),
    (("/usr/bin/xcrun", "stapler", "staple", SECRET), "stapler_staple"),
    (("/usr/bin/xcrun", "stapler", "validate", SECRET), "stapler_validate"),
    (("/usr/bin/swift", "build", "--package-path", SECRET), "swift_build"),
    (("uv", "python", "find", SECRET), "uv_python_find"),
    (("uv", "pip", "install", "--python", SECRET), "uv_pip_install"),
    (("uv", "export", "--output-file", "find"), "uv_export"),
    (("/usr/bin/security", "import", "find-identity", "-P", SECRET), "security_import"),
    (("/usr/bin/cc", "-o", SECRET), "cc_compile"),
    (("/usr/bin/hdiutil", "create", SECRET), "hdiutil_create"),
    (("/usr/bin/hdiutil", "attach", SECRET), "hdiutil_attach"),
    (("/usr/bin/git", "-C", SECRET, "rev-parse", "HEAD"), "git_revision"),
    (("/usr/bin/ditto", "-c", "-k", SECRET), "ditto_archive"),
    (("/usr/sbin/spctl", "--assess", SECRET), "spctl_assess"),
    ((f"/{SECRET}/malicious-{SECRET}", SECRET), "unknown_tool"),
    (("/usr/bin/xcrun", SECRET, SECRET), "xcrun"),
])
def test_failures_have_only_allowlisted_diagnostics(monkeypatch, capsys, arguments, operation):
    fail_command(monkeypatch, returncode=3)
    with pytest.raises(host_builder.BuildCommandError) as raised:
        host_builder.run(*arguments)
    error = raised.value
    assert isinstance(error, subprocess.CalledProcessError)
    assert error.diagnostic == f"build_command_{operation}_failed_exit_3"
    assert str(error) == error.diagnostic
    assert error.cmd == operation
    assert error.args == (error.diagnostic,)
    assert error.stdout is None
    assert error.stderr is None
    assert error.__context__ is None
    assert error.__cause__ is None
    assert SECRET not in repr(error)
    assert SECRET not in repr(vars(error))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("stderr,category", [
    (f"{SECRET}: User interaction is not allowed.", "interaction_not_allowed"),
    (f"{SECRET}: errSecInternalComponent", "internal_component"),
    (f"{SECRET}: The specified item could not be found in the keychain.", "keychain_item_missing"),
    (f"{SECRET}: no identity found", "identity_missing"),
    (f"{SECRET}: unable to build chain to self-signed root", "certificate_chain_invalid"),
])
def test_signing_categories_are_fixed_and_do_not_retain_stderr(monkeypatch, stderr, category):
    fail_command(monkeypatch, stderr=stderr)
    with pytest.raises(host_builder.BuildCommandError) as raised:
        host_builder.run("/usr/bin/codesign", "--sign", SECRET, f"/{SECRET}/app")
    error = raised.value
    assert error.diagnostic == f"build_command_codesign_sign_failed_exit_1_{category}"
    assert SECRET not in repr(vars(error))


@pytest.mark.parametrize("stderr", [b"private cannot read requirement", "private No such file or directory"])
def test_unreadable_input_category_is_only_for_codesign_verify(monkeypatch, stderr):
    fail_command(monkeypatch, stderr=stderr)
    with pytest.raises(host_builder.BuildCommandError) as raised:
        host_builder.run("/usr/bin/codesign", "--verify", "-R", SECRET)
    assert raised.value.diagnostic == "build_command_codesign_verify_failed_exit_1_input_unreadable"
    for arguments in (("/usr/bin/codesign", "--sign", SECRET),
                      ("/usr/bin/security", "find-identity", SECRET),
                      ("/usr/bin/hdiutil", "verify", SECRET)):
        with pytest.raises(host_builder.BuildCommandError) as raised:
            host_builder.run(*arguments)
        assert "input_unreadable" not in raised.value.diagnostic


def test_unknown_tools_do_not_gain_signing_categories(monkeypatch):
    fail_command(monkeypatch, stderr=f"{SECRET}: errSecInternalComponent")
    with pytest.raises(host_builder.BuildCommandError) as raised:
        host_builder.run(f"/{SECRET}", "--sign", SECRET)
    assert raised.value.diagnostic == "build_command_unknown_tool_failed_exit_1"


def test_successful_stdout_is_returned_without_logging(monkeypatch, capsys):
    def check_output(arguments, **kwargs):
        assert arguments == ("uv", "python", "find", SECRET)
        return f"  /{SECRET}/python\n"

    monkeypatch.setattr(host_builder.subprocess, "check_output", check_output)
    assert host_builder.run("uv", "python", "find", SECRET) == f"/{SECRET}/python"
    assert capsys.readouterr() == ("", "")


def test_exception_constructor_refuses_untrusted_labels():
    error = host_builder.BuildCommandError(SECRET, SECRET, SECRET)
    assert error.diagnostic == "build_command_unknown_tool_failed_exit_1"
    assert SECRET not in repr(error)
    assert SECRET not in repr(vars(error))


def test_signal_exit_status_remains_numeric_and_sanitized(monkeypatch):
    fail_command(monkeypatch, returncode=-9)
    with pytest.raises(host_builder.BuildCommandError) as raised:
        host_builder.run("/usr/bin/swift", "build", SECRET)
    assert raised.value.returncode == -9
    assert raised.value.diagnostic == "build_command_swift_build_failed_exit_-9"


def test_host_cli_exits_with_safe_command_diagnostic(monkeypatch, tmp_path, capsys):
    fail_command(monkeypatch, stderr=f"{SECRET}: credential tool failure")
    monkeypatch.setattr(host_builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["build_mcpme_host.py", "--output", str(tmp_path / SECRET)])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPTS / "build_mcpme_host.py"), run_name="__main__")
    assert raised.value.code == "build_command_uv_python_find_failed_exit_1"
    assert capsys.readouterr() == ("", "")


def test_bundled_model_generator_has_licensed_source_closure_without_checkout(tmp_path):
    host_builder.bundle_model_template_assets(tmp_path)
    assets = tmp_path / "templates/jevk5-llamacpp-v1/assets"
    for name in ("backend/jevk5_app/server.py", "backend/jevk5_app/scorer.py", "backend/mcpme_jevk5/runtime.py", "backend/mcpme_local_transport.py",
                 "vendor/jevk5/prompt.py", "vendor/jevk5/LICENSE", "model-manifest.json",
                 "THIRD_PARTY_NOTICES.md", "uv.lock", "scripts/jevk5-package-launcher.c"):
        copied = assets / name
        assert copied.is_file()
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == hashlib.sha256((host_builder.ROOT / name).read_bytes()).hexdigest()
    assert not list(assets.rglob("*.pyc")) and not list(assets.rglob("__pycache__"))


@pytest.mark.parametrize("with_engine", [False, True])
def test_host_build_preserves_nested_document_links_and_binds_all_document_hashes(monkeypatch, tmp_path, with_engine):
    source = tmp_path / "source"
    files = {"pyproject.toml": '[project]\nversion = "0.1.0"\n', "uv.lock": "synthetic lock",
             "backend/mcpme/service.py": "synthetic source", "backend/mcpme_local_transport.py": "synthetic transport",
             "README.md": "synthetic readme", "THIRD_PARTY_NOTICES.md": "synthetic notices",
             "scripts/mcpme-command-reap.c": "synthetic helper source",
             "assets/brand/source.svg": "synthetic branding", "templates/catalog.json": "{}",
             "templates/task/.venv/private-input.txt": "excluded local environment",
             "docs/AUTOMATIC_PREPARATION.md": "[Review](reviews/DIRECTORY_ONBOARDING_REVIEW.md)\n",
             "docs/reviews/DIRECTORY_ONBOARDING_REVIEW.md": "# Directory onboarding review\n",
             "docs/reviews/figure.svg": "synthetic documentation figure",
             "docs/reviews/__pycache__/ignored.pyc": "ignored cache", "docs/reviews/ignored.pyc": "ignored bytecode"}
    for relative, value in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3.12").write_text("synthetic Python")
    (runtime / "lib/python3.12").mkdir(parents=True)
    (runtime / "lib/python3.12/LICENSE.txt").write_text("synthetic Python license")
    calls = []

    def run(*arguments):
        calls.append(arguments)
        if arguments[:3] == ("uv", "python", "find"):
            return str(runtime / "bin/python3.12")
        if arguments[0] == "/usr/bin/cc" and "-o" in arguments:
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"synthetic native reaper")
        if arguments[:2] == ("uv", "export"):
            Path(arguments[arguments.index("--output-file") + 1]).write_text("synthetic requirements")
        if arguments[0] == "/usr/bin/codesign":
            reaper = Path(arguments[-1])
            reaper.write_bytes(reaper.read_bytes() + b" signed")
        if arguments[0] == "/usr/bin/cc" and "--version" in arguments:
            return "synthetic compiler"
        if arguments[-1:] == ("--version",):
            return "Python 3.12.13"
        if "rev-parse" in arguments:
            return "a" * 40
        return ""

    monkeypatch.setattr(host_builder, "ROOT", source)
    monkeypatch.setattr(host_builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(host_builder, "run", run)
    monkeypatch.setattr(host_builder, "bundle_model_template_assets", lambda destination: None)
    monkeypatch.setattr(host_builder, "bundle_textgen_backend", lambda destination: None)
    def toolchain(destination, runtime):
        (destination / "preparation-toolchain").mkdir()
        (destination / "preparation-toolchain/toolchain.json").write_text("synthetic toolchain")
    monkeypatch.setattr(host_builder, "bundle_preparation_toolchain", toolchain)
    engine = tmp_path / "explicit-engine"
    def bundle_engine(destination, selected):
        assert selected == engine and (destination / "preparation-toolchain/toolchain.json").is_file()
        (destination / "preparation-toolchain/engine").write_bytes(b"synthetic exact engine")
    monkeypatch.setattr(host_builder, "bundle_textgen_toolchain", bundle_engine)
    host = host_builder.build(tmp_path / "host", textgen_engine=engine if with_engine else None)
    receipt = json.loads((host / "build.json").read_text())
    included = {"docs/AUTOMATIC_PREPARATION.md", "docs/reviews/DIRECTORY_ONBOARDING_REVIEW.md", "docs/reviews/figure.svg"}
    for relative in included:
        assert (host / relative).read_bytes() == (source / relative).read_bytes()
        assert receipt["artifact_sha256"][relative] == hashlib.sha256((source / relative).read_bytes()).hexdigest()
    assert {name for name in receipt["artifact_sha256"] if name.startswith("docs/")} == included
    assert not list((host / "docs").rglob("__pycache__")) and not list((host / "docs").rglob("*.pyc"))
    assert not list((host / "templates").rglob(".venv"))
    assert ("preparation-toolchain/engine" in receipt["artifact_sha256"]) is with_engine
    if with_engine:
        assert receipt["artifact_sha256"]["preparation-toolchain/engine"] == hashlib.sha256(b"synthetic exact engine").hexdigest()
    assert receipt["signing"] == "ad_hoc_native_helpers"
    assert receipt["artifact_sha256"]["bin/mcpme-reap"] == hashlib.sha256((host / "bin/mcpme-reap").read_bytes()).hexdigest()
    assert len([call for call in calls if call[0] == "/usr/bin/codesign"]) == 2
    metadata = json.loads((host / "command-execution.json").read_bytes())
    assert metadata["production_admission"] == "disabled" and metadata["configuration_authority"] == "inventory-only"
    assert metadata["receipt_format"] == 3 and metadata["package_schema"] == 2
    assert metadata["helper_sha256"] == hashlib.sha256((host / metadata["helper"]).read_bytes()).hexdigest()
    for name in (metadata["helper"], "command-execution.json"):
        assert receipt["artifact_sha256"][name] == hashlib.sha256((host / name).read_bytes()).hexdigest()
    assert set(receipt["artifact_sha256"]) == {path.relative_to(host).as_posix() for path in host.rglob("*")
                                             if path.is_file() and path != host / "build.json"}
    assert {"python/bin/python3.12", "bin/mcpme", "licenses/python-LICENSE.txt"} <= receipt["artifact_sha256"].keys()
    helper = host / metadata["helper"]
    helper.write_bytes(helper.read_bytes() + b" distribution signature")
    host_builder.refresh_command_inventory(host)
    refreshed = json.loads((host / "command-execution.json").read_bytes())
    assert refreshed == {**metadata, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
    refreshed["production_admission"] = "enabled"
    (host / "command-execution.json").write_text(json.dumps(refreshed))
    with pytest.raises(ValueError, match="command_inventory_invalid"):
        host_builder.refresh_command_inventory(host)


def test_private_runtime_copy_dereferences_internal_files_and_excludes_bytecode(tmp_path):
    source = tmp_path / "runtime"; source.mkdir()
    (source / "python").write_bytes(b"synthetic runtime")
    (source / "python").chmod(0o755)
    (source / "python3").symlink_to("python")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/ignored.pyc").write_bytes(b"ignored")
    (source / "ignored.pyc").write_bytes(b"ignored")
    target = tmp_path / "copied"
    host_builder.copy_private_runtime(source, target)
    assert (target / "python3").read_bytes() == b"synthetic runtime"
    assert not (target / "python3").is_symlink()
    assert (target / "python3").stat().st_mode & 0o111
    assert {path.name for path in target.iterdir()} == {"python", "python3"}


@pytest.mark.parametrize("kind", ["escape", "directory", "size", "count"])
def test_private_runtime_copy_rejects_unbounded_or_external_closure_before_copy(tmp_path, monkeypatch, kind):
    source = tmp_path / "runtime"; source.mkdir()
    (source / "python").write_bytes(b"synthetic runtime")
    if kind == "escape":
        outside = tmp_path / "outside"; outside.write_bytes(b"unadmitted")
        (source / "link").symlink_to(outside)
    elif kind == "directory":
        (source / "directory").mkdir(); (source / "link").symlink_to("directory")
    elif kind == "size":
        monkeypatch.setattr(host_builder, "MAX_RUNTIME_BYTES", 1)
    else:
        monkeypatch.setattr(host_builder, "MAX_RUNTIME_FILES", 0)
    target = tmp_path / "copied"
    with pytest.raises(ValueError, match="private_python_runtime_"):
        host_builder.copy_private_runtime(source, target)
    assert not target.exists()


@pytest.mark.parametrize("replacement", ["fifo", "regular_inode", "same_inode_write"])
def test_private_runtime_copy_rejects_concurrent_source_substitution_without_blocking(tmp_path, replacement):
    # Exercise the actual replacement in an owned bounded subprocess, so the
    # previous blocking FIFO behavior fails within the test deadline.
    source = tmp_path / "runtime"; source.mkdir()
    (source / "python").write_bytes(b"original")
    code = "\n".join([
        "import os, sys", "from pathlib import Path", "sys.path.insert(0, sys.argv[1])",
        "import build_mcpme_host as builder", "source = Path(sys.argv[2])", "original = os.open",
        "def replace(path, flags, *args, **kwargs):",
        "    if Path(path) == source / 'python':",
        "        if sys.argv[4] == 'fifo':",
        "            Path(path).unlink(); os.mkfifo(path)",
        "        elif sys.argv[4] == 'regular_inode':",
        "            other = source / 'replacement'; other.write_bytes(b'original'); other.replace(path)",
        "        else: Path(path).write_bytes(b'modified')",
        "    return original(path, flags, *args, **kwargs)",
        "os.open = replace",
        "try: builder.copy_private_runtime(source, Path(sys.argv[3]))",
        "except ValueError as error: assert str(error) == 'private_python_runtime_changed'",
        "else: raise AssertionError('source replacement accepted')",
    ])
    result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(source), str(tmp_path / "copy"), replacement],
                            capture_output=True, timeout=3)
    assert result.returncode == 0
    assert not (tmp_path / "copy/python").exists()
