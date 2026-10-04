from pathlib import Path
import json
import plistlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_phase3_native_onboarding as native


def app_fixture(tmp_path, signing="developer_id"):
    app = tmp_path / "source mcpme.app"
    host = app / "Contents/Resources/host"
    host.mkdir(parents=True)
    (host / "build.json").write_text("{}")
    binary = app / "Contents/MacOS/mcpme"
    binary.parent.mkdir()
    binary.write_bytes(b"immutable compiled bytes")
    binary.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.jshahbazi.mcpme", "CFBundleExecutable": "mcpme"}))
    (app / "Contents/Resources/app-build.json").write_text(json.dumps({"signing": signing}))
    seal = app / "Contents/_CodeSignature"
    seal.mkdir()
    (seal / "CodeResources").write_bytes(b"sealed resource context")
    destination = tmp_path / "private workspace"
    destination.mkdir(mode=0o700)
    return app, host, binary, destination


def test_relocation_preserves_main_executable_resource_signature_context(tmp_path, monkeypatch):
    app, host, binary, destination = app_fixture(tmp_path)
    checked = []

    def signature(path):
        enclosing = native.native_app_root(path)
        assert (enclosing / "Contents/Info.plist").is_file()
        assert (enclosing / "Contents/_CodeSignature/CodeResources").read_bytes() == b"sealed resource context"
        checked.append(enclosing)
        return True

    monkeypatch.setattr(native, "verify_native_app_signature", signature)
    report = {}
    copied_host, copied_binary = native.relocate_host_and_native(destination, host, binary, report)
    assert checked == [app, destination / "relocated mcpme.app"]
    assert copied_binary.read_bytes() == binary.read_bytes()
    assert copied_binary.stat().st_mode & 0o777 == 0o755
    assert copied_host == copied_binary.parent.parent / "Resources/host"
    assert report["native_bundle_preserved"] and report["native_app_signature_verified"]


def test_wrong_embedded_host_is_rejected_before_copy_or_signature(tmp_path, monkeypatch):
    _, _, binary, destination = app_fixture(tmp_path)
    monkeypatch.setattr(native, "verify_native_app_signature", lambda _: pytest.fail("unexpected signature call"))
    with pytest.raises(native.AcceptanceError) as caught:
        native.relocate_host_and_native(destination, tmp_path / "other host", binary, {})
    assert caught.value.code == "native_app_host_pair_mismatch"
    assert list(destination.iterdir()) == []


def test_invalid_source_signature_is_rejected_before_copy(tmp_path, monkeypatch):
    _, host, binary, destination = app_fixture(tmp_path)
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(native.AcceptanceError) as caught:
        native.relocate_host_and_native(destination, host, binary, {})
    assert caught.value.code == "native_app_signature_invalid"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("signing", ["ad_hoc", "developer_id"])
def test_signature_verification_retains_declared_signing_policy(tmp_path, monkeypatch, signing):
    _, _, binary, _ = app_fixture(tmp_path, signing)
    calls = []
    monkeypatch.setattr(native.subprocess, "run", lambda args, **kwargs:
        calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert native.verify_native_app_signature(binary)
    args, kwargs = calls[0]
    assert args[:4] == ["/usr/bin/codesign", "--verify", "--strict", "--deep"]
    assert ("-R" in args) == (signing == "developer_id")
    if signing == "developer_id":
        assert args[args.index("-R") + 1].startswith("=anchor apple generic")
    assert kwargs == {"capture_output": True, "timeout": 30}


@pytest.mark.parametrize("field,value,code", [
    ("info", [], "native_app_metadata_invalid"),
    ("receipt", [], "native_app_metadata_invalid"),
    ("receipt", {"signing": "unknown"}, "native_app_identity_mismatch"),
])
def test_invalid_app_metadata_is_rejected_before_signature(tmp_path, monkeypatch, field, value, code):
    app, _, binary, _ = app_fixture(tmp_path)
    if field == "info":
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(value))
    else:
        (app / "Contents/Resources/app-build.json").write_text(json.dumps(value))
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: pytest.fail("unexpected signature call"))
    with pytest.raises(native.AcceptanceError) as caught:
        native.verify_native_app_signature(binary)
    assert caught.value.code == code


def test_standalone_development_native_retains_existing_relocation(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "build.json").write_text("{}")
    binary = tmp_path / "McpmeMenuBar"
    binary.write_bytes(b"standalone compiled bytes")
    binary.chmod(0o755)
    destination = tmp_path / "private workspace"
    destination.mkdir(mode=0o700)
    report = {}
    copied_host, copied_native = native.relocate_host_and_native(destination, host, binary, report)
    assert copied_native.read_bytes() == binary.read_bytes()
    assert copied_host == destination / "relocated host"
    assert not report["native_bundle_preserved"]
