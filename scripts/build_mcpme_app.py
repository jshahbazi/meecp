"""Build the native mcpme app with ad hoc or Developer ID signing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

from build_mcpme_host import ROOT, BuildCommandError, build as build_host, project_version, run
from prepare_json_package import prepare as prepare_json_package

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
DEVELOPER_ID_REQUIREMENT = (
    "anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
)
STARTER_PACKAGE_PATH = Path("starter-packages/json-utilities")


def prepare_starter_package(resources: Path) -> Path:
    """Compile the starter during the app build, never during user installation."""
    destination = resources / STARTER_PACKAGE_PATH
    destination.parent.mkdir()
    return prepare_json_package(destination)


def finalize_starter_package(package: Path, host: Path, *, release: bool) -> dict:
    """Bind package integrity to the signed files before the app seals Resources."""
    lock_path = package / "mcpme.lock.json"
    lock = json.loads(lock_path.read_bytes())
    # Re-signing modifies Mach-O bytes. Keep the original source/license hashes
    # intact so final validation still catches changes outside that operation.
    for artifact in lock["artifacts"]:
        path = package / artifact["path"]
        with path.open("rb") as source:
            magic = source.read(4)
        if magic in MACHO_MAGIC:
            artifact["sha256"] = sha256(path)
            artifact["size"] = path.stat().st_size
            artifact["provenance"] += (
                " Signed during the mcpme app build with "
                + ("Developer ID." if release else "an ad hoc signature.")
            )
    lock["build"]["recipe"] += "; app build signs bundled Mach-O files with " + (
        "Developer ID" if release else "ad hoc codesign"
    )
    lock["build"]["builder"] += " + mcpme build_mcpme_app.py v1"
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    # Use the packaged validator and dependency closure, with no package code
    # execution or running host required, before publishing the app.
    validation = json.loads(run(str(host / "bin/mcpme"), "validate", "--package", str(package), "--json"))
    manifest = validation.get("package", {})
    package_digest = validation.get("package_digest", "")
    if (validation.get("valid") is not True or manifest.get("package_id") != "local.json-utilities"
            or manifest.get("version") != "1.0.0"
            or not isinstance(package_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", package_digest)):
        raise SystemExit("bundled_starter_package_invalid")
    return {"path": STARTER_PACKAGE_PATH.as_posix(), "package_id": manifest["package_id"],
            "version": manifest["version"], "package_digest": package_digest,
            "lock_sha256": sha256(lock_path)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def developer_identity(identity: str, keychain: Path | None = None) -> str:
    """Resolve only an available Developer ID Application certificate to its SHA-1."""
    arguments = ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"]
    if keychain is not None:
        arguments.append(str(keychain.resolve(strict=True)))
    listing = run(*arguments)
    for certificate_hash, name in re.findall(r'[0-9]+\) ([A-Fa-f0-9]{40}) "([^"]+)"', listing):
        if name.startswith("Developer ID Application: ") and (
            identity == name or identity.lower() == certificate_hash.lower()
        ):
            return certificate_hash
    raise SystemExit("developer_id_identity_unavailable")


def signing_arguments(identity: str, release: bool, keychain: Path | None = None) -> list[str]:
    arguments = ["/usr/bin/codesign", "--force", "--sign", identity]
    if release:
        arguments.extend(["--timestamp", "--options", "runtime"])
    if keychain is not None:
        arguments.extend(["--keychain", str(keychain)])
    return arguments


def verify_signature(path: Path, certificate_hash: str | None = None, deep: bool = False):
    arguments = ["/usr/bin/codesign", "--verify", "--strict"]
    if deep:
        arguments.append("--deep")
    if certificate_hash is not None:
        # The leading '=' selects literal requirement text; otherwise codesign
        # interprets the entire expression as the name of a requirements file.
        arguments.extend(["-R", "=" + DEVELOPER_ID_REQUIREMENT + f' and certificate leaf = H"{certificate_hash}"'])
    run(*arguments, str(path))


def sign_runtime(root: Path, identity: str = "-", release: bool = False,
                 keychain: Path | None = None) -> int:
    count = 0
    # Sign individual native files before the outer bundle seals their resources.
    for path in sorted(root.rglob("*"), key=lambda path: (-len(path.parts), str(path))):
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
        run(*signing_arguments(identity, release, keychain), str(path))
        verify_signature(path, identity if release else None)
        count += 1
    return count


def build(destination: Path, legacy_recovery_app: Path | None = None,
          sign_identity: str | None = None, keychain: Path | None = None,
          version: str | None = None) -> Path:
    # Validate before invoking any tool or signing operation.
    effective_version = project_version(version)
    if keychain is not None and sign_identity is None:
        raise SystemExit("keychain_requires_signing_identity")
    identity = developer_identity(sign_identity, keychain) if sign_identity is not None else "-"
    release = sign_identity is not None
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
    icon_generator = scratch / "mcpme-icon"
    iconset = scratch / "McpmeAppIcon.iconset"
    run("/usr/bin/swiftc", str(ROOT / "Sources/McpmeMenuBar/Brand.swift"),
        str(ROOT / "scripts/mcpme-icon.swift"), "-o", str(icon_generator))
    run(str(icon_generator), str(ROOT / "assets/brand"), str(iconset))
    run("/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns"))
    shutil.copytree(ROOT / "assets/brand", resources / "assets/brand")
    host = (build_host(resources / "host", version=version) if version is not None
            else build_host(resources / "host"))
    starter = prepare_starter_package(resources)
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
            "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": effective_version,
            "CFBundleVersion": "1", "LSUIElement": True, "LSMinimumSystemVersion": "26.0",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication", "CFBundleIconFile": "AppIcon"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    shutil.copyfile(ROOT / "README.md", resources / "README.md")
    bundled_docs = resources / "docs"
    bundled_docs.mkdir()
    for document in sorted((ROOT / "docs").glob("*.md")):
        shutil.copyfile(document, bundled_docs / document.name)
    for name in ("model-manifest.json", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, resources / name)
    native_input_hash = sha256(native)
    signed_files = sign_runtime(staged, identity, release, keychain)
    starter_receipt = finalize_starter_package(starter, host, release=release)
    # Signing changes the reaper hash recorded by the host builder. Refresh that
    # receipt before sealing Resources; never leave development labels on a release.
    host_receipt_path = host / "build.json"
    host_receipt = json.loads(host_receipt_path.read_bytes())
    for relative in host_receipt["artifact_sha256"]:
        host_receipt["artifact_sha256"][relative] = sha256(host / relative)
    host_receipt["signing"] = "developer_id" if release else "ad_hoc"
    host_receipt["distribution"] = "bundled_native_app_host"
    host_receipt_path.write_text(json.dumps(host_receipt, indent=2, sort_keys=True) + "\n")
    receipt = {"schema_version": 1, "version": effective_version,
               "distribution": "developer_id_native_app" if release else "development_native_app",
               "signing": "developer_id" if release else "ad_hoc",
               "hardened_runtime": release, "secure_timestamp": release,
               "signed_native_files": signed_files,
               "notarization": "handled_by_dmg_builder" if release else "not_requested",
               "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
               "source_tree_dirty": bool(run("git", "-C", str(ROOT), "status", "--porcelain")),
               "native_input_sha256": native_input_hash, "swift_source_sha256": sha256(ROOT / "Sources/McpmeMenuBar/main.swift"),
               "branding_source_sha256": sha256(ROOT / "Sources/McpmeMenuBar/Brand.swift"),
               "app_icon_sha256": sha256(resources / "AppIcon.icns"),
               "starter_packages": [starter_receipt],
               "host_receipt_sha256": sha256(host / "build.json"), "legacy_handoff": legacy_recovery_app is not None}
    # Add the receipt before the final signature seals Resources.
    (resources / "app-build.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    run(*signing_arguments(identity, release, keychain), str(staged))
    verify_signature(staged, identity if release else None, deep=True)
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
    parser.add_argument("--sign-identity", help="Developer ID Application name or certificate SHA-1")
    parser.add_argument("--keychain", type=Path, help="Keychain containing the signing certificate")
    parser.add_argument("--version", help="Override the application version (major.minor.patch)")
    arguments = parser.parse_args()
    try:
        print(build(arguments.output.absolute(), arguments.legacy_recovery_app,
                    arguments.sign_identity, arguments.keychain, arguments.version))
    except KeyboardInterrupt:
        raise SystemExit("app_build_interrupted") from None
    except BuildCommandError as error:
        raise SystemExit(error.diagnostic) from None
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit("app_build_failed") from None
