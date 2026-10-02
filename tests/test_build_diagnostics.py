"""Build failures identify the tool without exposing credentials or tool output."""
from pathlib import Path
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
