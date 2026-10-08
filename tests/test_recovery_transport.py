"""Recovery bundles pair the native client with its matching private backend."""
import importlib.util
import json
from pathlib import Path
import plistlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("recovery_builder", ROOT / "scripts/build_jevk5_recovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = tmp_path / "built-native"
    native.mkdir()
    (native / "JevK5MenuBar").write_bytes(b"synthetic-updated-native")
    def run(*args):
        return str(native) if "--show-bin-path" in args else "synthetic-revision" if args[0] == "git" else ""
    monkeypatch.setattr(module, "run", run)
    return module


def old_bundle(tmp_path):
    source = tmp_path / "Original.app"
    resources = source / "Contents/Resources"
    macos = source / "Contents/MacOS"
    macos.mkdir(parents=True)
    (resources / "backend/jevk5_app").mkdir(parents=True)
    (source / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.jshahbazi.jevk5", "CFBundleExecutable": "JevK5MenuBar"}))
    (macos / "JevK5MenuBar").write_bytes(b"synthetic-old-native")
    (macos / "jevk5-mcp").write_text("old launcher")
    (resources / "backend/jevk5_app/__main__.py").write_text("old backend")
    for name in ("models/fixture.gguf", "python/fixture", "vendor/fixture", "licenses/fixture"):
        path = resources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained synthetic artifact")
    return source


def test_recovery_refreshes_transport_without_modifying_original(tmp_path, builder):
    source = old_bundle(tmp_path)
    original = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    output = builder.build(source, tmp_path / "Recovery.app")
    resources = output / "Contents/Resources"
    assert (resources / "backend/jevk5_app/__main__.py").read_bytes() == (ROOT / "backend/jevk5_app/__main__.py").read_bytes()
    assert (resources / "backend/meecp_local_transport.py").read_bytes() == (ROOT / "backend/meecp_local_transport.py").read_bytes()
    assert (output / "Contents/MacOS/jevk5-mcp").read_bytes() == (ROOT / "scripts/jevk5-mcp").read_bytes()
    assert (output / "Contents/MacOS/JevK5MenuBar").read_bytes() == b"synthetic-updated-native"
    for name in ("models/fixture.gguf", "python/fixture", "vendor/fixture", "licenses/fixture"):
        assert (resources / name).read_bytes() == b"retained synthetic artifact"
    assert {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()} == original
    receipt = json.loads(output.with_name(output.name + ".build.json").read_bytes())
    assert "updated_bundled_launcher" in receipt["changes"]


def test_recovery_unlinks_native_leaf_symlink_and_rejects_escaped_parent(tmp_path, builder):
    source = old_bundle(tmp_path)
    binary = source / "Contents/MacOS/JevK5MenuBar"
    original = binary.with_name("original-native")
    binary.rename(original)
    binary.symlink_to(original)
    output = builder.build(source, tmp_path / "Recovery.app")
    assert not (output / "Contents/MacOS/JevK5MenuBar").is_symlink()
    assert original.read_bytes() == b"synthetic-old-native"
    macos = source / "Contents/MacOS"
    original_dir = source / "original-bin"
    macos.rename(original_dir)
    macos.symlink_to(original_dir)
    with pytest.raises(ValueError, match="recovery_destination_escape"):
        builder.build(source, tmp_path / "Unsafe.app")
    assert (original_dir / "jevk5-mcp").read_text() == "old launcher"
