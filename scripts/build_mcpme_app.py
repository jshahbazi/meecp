"""Build the native mcpme development app around its locked private host."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

from build_mcpme_host import ROOT, build as build_host, run

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sign_runtime(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise SystemExit("Bundled runtime contains an external symlink")
            continue
        if not path.is_file():
            continue
        with path.open("rb") as source:
            magic = source.read(4)
        if magic not in MACHO_MAGIC:
            continue
        if path.suffix == ".dylib":
            # python-build-standalone gives libpython an absolute install ID;
            # rewrite that self identity before inspecting actual imports.
            run("/usr/bin/install_name_tool", "-id", "@loader_path/" + path.name, str(path))
        listing = run("/usr/bin/otool", "-L", str(path))
        for line in listing.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise SystemExit("Bundled runtime contains an external native dependency")
        run("/usr/bin/codesign", "--force", "--sign", "-", str(path))


def build(destination: Path, legacy_recovery_app: Path | None = None) -> Path:
    if destination.exists():
        raise SystemExit("Destination exists; select a new output path")
    scratch = ROOT / ".build/mcpme-native"
    run("/usr/bin/swift", "build", "-c", "release", "--product", "McpmeMenuBar", "--package-path", str(ROOT), "--scratch-path", str(scratch))
    binary_directory = Path(run("/usr/bin/swift", "build", "-c", "release", "--show-bin-path", "--package-path", str(ROOT), "--scratch-path", str(scratch)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(destination.stem + ".building.app")
    if staged.exists():
        raise SystemExit("A previous app staging directory exists; inspect it before retrying")
    contents = staged / "Contents"
    macos, resources = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True); resources.mkdir()
    native = macos / "mcpme"
    shutil.copy2(binary_directory / "McpmeMenuBar", native)
    host = build_host(resources / "host")
    shutil.copy2(native, host / "bin/mcpme-handoff")
    (host / "native-app.json").write_text(json.dumps({"schema_version": 1, "bundle_identifier": "com.jshahbazi.mcpme",
                                                     "native_executable": "../../MacOS/mcpme"}, sort_keys=True) + "\n")
    if legacy_recovery_app is not None:
        legacy_recovery_app = legacy_recovery_app.resolve()
        metadata = plistlib.loads((legacy_recovery_app / "Contents/Info.plist").read_bytes())
        if metadata.get("CFBundleIdentifier") != "com.jshahbazi.jevk5" or metadata.get("CFBundleExecutable") != "JevK5MenuBar":
            raise SystemExit("Recovery bundle has an incompatible identity")
        legacy = legacy_recovery_app / "Contents/MacOS/JevK5MenuBar"
        (host / "legacy-handoff.json").write_text(json.dumps({"schema_version": 1,
            "bundle_identifier": "com.jshahbazi.jevk5", "executable_sha256": sha256(legacy),
            "default_bundle_path": str(legacy_recovery_app)}, sort_keys=True) + "\n")
    info = {"CFBundleIdentifier": "com.jshahbazi.mcpme", "CFBundleName": "mcpme", "CFBundleDisplayName": "mcpme",
            "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "1", "LSUIElement": True, "LSMinimumSystemVersion": "26.0",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    shutil.copyfile(ROOT / "README.md", resources / "README.md")
    bundled_docs = resources / "docs"
    bundled_docs.mkdir()
    for document in sorted((ROOT / "docs").glob("*.md")):
        shutil.copyfile(document, bundled_docs / document.name)
    for name in ("model-manifest.json", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, resources / name)
    native_input_hash = sha256(native)
    sign_runtime(staged)
    receipt = {"schema_version": 1, "distribution": "development_native_app", "signing": "ad_hoc",
               "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
               "source_tree_dirty": bool(run("git", "-C", str(ROOT), "status", "--porcelain")),
               "native_input_sha256": native_input_hash, "swift_source_sha256": sha256(ROOT / "Sources/McpmeMenuBar/main.swift"),
               "host_receipt_sha256": sha256(host / "build.json"), "legacy_handoff": legacy_recovery_app is not None}
    # Add the receipt before the final signature seals Resources.
    (resources / "app-build.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(staged))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(staged))
    staged.rename(destination)
    # An executable signature seals Resources; keep its final digest outside that
    # seal to avoid a self-referential receipt changing the executable digest.
    final_receipt = {**receipt, "native_sha256": sha256(destination / "Contents/MacOS/mcpme"),
                     "bundle": str(destination)}
    destination.with_name(destination.name + ".build.json").write_text(json.dumps(final_receipt, indent=2, sort_keys=True) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mcpme.app")
    parser.add_argument("--legacy-recovery-app", type=Path)
    arguments = parser.parse_args()
    try:
        print(build(arguments.output.absolute(), arguments.legacy_recovery_app))
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit("app_build_failed") from None
