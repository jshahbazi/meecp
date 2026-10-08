"""Build, sign, notarize, and verify a drag-to-Applications disk image."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import tempfile

from build_meecp_app import build as build_app, developer_identity, require_command_asset, verify_signature
from build_meecp_host import ROOT, BuildCommandError, project_version, run


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_metadata(app: Path, certificate_hash: str | None = None) -> dict:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (info.get("CFBundleIdentifier") != "com.jshahbazi.mcpme"
            or info.get("CFBundleExecutable") != "meecp"
            or info.get("CFBundlePackageType") != "APPL"
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", info.get("CFBundleShortVersionString", ""))):
        raise SystemExit("Input must be a built meecp application")
    verify_signature(app, certificate_hash, deep=True)
    if run("/usr/bin/lipo", "-archs", str(app / "Contents/MacOS/meecp")) != "arm64":
        raise SystemExit("Input must be an arm64 meecp application")
    return info


def command_profile(app: Path, certificate_hash: str | None = None) -> dict | None:
    """Check the immutable helper pair when a supplied app enables commands."""
    host = app / "Contents/Resources/host"
    policy = host / "templates/release-policies/phase3-v1.json"
    if not policy.exists():
        return None
    try:
        if policy.stat().st_size > 64 * 1024:
            raise ValueError
        value = json.loads(policy.read_bytes())
        if type(value) is not dict:
            raise ValueError
        if value.get("command") is None:
            return None
        from signed_command_helpers import verify_host
        return verify_host(host, certificate=certificate_hash)
    except (OSError, ValueError, TypeError):
        raise SystemExit("signed_command_helpers_invalid") from None


def sign_disk_image(image: Path, certificate_hash: str, keychain: Path | None = None):
    arguments = ["/usr/bin/codesign", "--force", "--sign", certificate_hash, "--timestamp",
                 "-i", "com.jshahbazi.mcpme.dmg"]
    if keychain is not None:
        arguments.extend(["--keychain", str(keychain)])
    run(*arguments, str(image))
    verify_signature(image, certificate_hash)


def notarize(artifact: Path, profile: str, keychain: Path | None = None) -> dict:
    arguments = ["/usr/bin/xcrun", "notarytool", "submit", str(artifact),
                 "--keychain-profile", profile, "--wait", "--timeout", "30m",
                 "--output-format", "json"]
    if keychain is not None:
        arguments.extend(["--keychain", str(keychain)])
    # Capture tool output in memory; never emit responses or download notary logs.
    result = json.loads(run(*arguments))
    submission_id = result.get("id")
    if (result.get("status") != "Accepted" or not isinstance(submission_id, str)
            or not re.fullmatch(r"[A-Fa-f0-9-]{36}", submission_id)):
        raise SystemExit("notarization_not_accepted")
    return {"status": "accepted", "submission_id": submission_id}


def staple(artifact: Path):
    run("/usr/bin/xcrun", "stapler", "staple", str(artifact))
    run("/usr/bin/xcrun", "stapler", "validate", str(artifact))


def verify_runtime(app: Path, workspace: Path):
    """Exercise only the packaged private runtime in an isolated home."""
    home = workspace / "test home"
    home.mkdir(mode=0o700)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "LANG": "en_US.UTF-8", "TMPDIR": str(workspace)}
    subprocess.run([str(app / "Contents/Resources/host/bin/meecp"), "--help"],
                   cwd=home, env=environment, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=30)


def publish_outputs(outputs: list[tuple[Path, Path]]):
    """Link fresh outputs exclusively and remove every new link on failure."""
    published = []
    try:
        for staged, destination in outputs:
            os.link(staged, destination)
            published.append(destination)
    except BaseException:
        for destination in reversed(published):
            destination.unlink()
        raise


def verify_image(image: Path, workspace: Path, info: dict,
                 certificate_hash: str | None = None, gatekeeper: bool = False) -> dict:
    """Mount read-only, simulate a drag install, and exercise the private runtime."""
    mount = workspace / "mounted"
    mount.mkdir()
    run("/usr/bin/hdiutil", "attach", str(image), "-readonly", "-nobrowse",
        "-noautoopen", "-mountpoint", str(mount))
    try:
        shortcut = mount / "Applications"
        if not shortcut.is_symlink() or os.readlink(shortcut) != "/Applications":
            raise SystemExit("Disk image is missing the Applications shortcut")
        if {path.name for path in mount.iterdir() if not path.name.startswith(".")} != {"meecp.app", "Applications"}:
            raise SystemExit("Disk image contains unexpected files")
        if app_metadata(mount / "meecp.app", certificate_hash) != info:
            raise SystemExit("Disk image app metadata changed")
        installed = workspace / "test Applications/meecp.app"
        installed.parent.mkdir()
        run("/usr/bin/ditto", str(mount / "meecp.app"), str(installed))
    finally:
        run("/usr/bin/hdiutil", "detach", str(mount))

    # Verify after ejecting, from a path with spaces and without developer tools
    # in PATH. Never launch the user's real installation or touch its state.
    app_metadata(installed, certificate_hash)
    command_profile(installed, certificate_hash)
    if gatekeeper:
        run("/usr/bin/xcrun", "stapler", "validate", str(installed))
        run("/usr/sbin/spctl", "--assess", "--type", "exec", str(installed))
    verify_runtime(installed, workspace)
    checks = {"image_checksum": True, "read_only_mount": True, "applications_shortcut": True,
            "mounted_app_signature": True, "copied_app_signature": True,
            "ejected_image": True, "private_runtime_minimal_environment": True}
    if gatekeeper:
        checks.update({"copied_app_notarization_ticket": True, "copied_app_gatekeeper_assessment": True,
                       "disk_image_notarization_ticket": True, "disk_image_gatekeeper_assessment": True})
    return checks


def build(output: Path | None = None, app: Path | None = None,
          sign_identity: str | None = None, notary_profile: str | None = None,
          keychain: Path | None = None, version: str | None = None, textgen_engine: Path | None = None,
          textgen_signed_asset: Path | None = None, command_signed_asset: Path | None = None) -> Path:
    # Validate before credential lookup, copying, signing, or notarization.
    if version is not None:
        project_version(version)
    if app is not None and (textgen_engine is not None or textgen_signed_asset is not None):
        raise SystemExit("textgen_engine_requires_source_build")
    if app is not None and command_signed_asset is not None:
        raise SystemExit("command_asset_requires_source_build")
    if command_signed_asset is not None:
        from signed_command_helpers import admit_archive as admit_commands
        admit_commands(command_signed_asset)
    if app is None:
        require_command_asset(ROOT, signed=sign_identity is not None, asset=command_signed_asset)
    if sign_identity is not None and textgen_engine is not None:
        raise SystemExit("signed_textgen_preparation_not_supported")
    if textgen_signed_asset is not None:
        if textgen_engine is not None:
            raise ValueError("textgen_runtime_selection_conflict")
        from signed_textgen_asset import admit_archive
        admit_archive(textgen_signed_asset)
    supplied_textgen = app is not None and (app / "Contents/Resources/host/preparation-toolchain/textgen-runtime").exists()
    if supplied_textgen and sign_identity is not None:
        from signed_textgen_asset import verify_host
        try:
            verify_host(app / "Contents/Resources/host")
        except Exception:
            raise SystemExit("signed_textgen_preparation_not_supported") from None
    supplied_commands = command_profile(app) if app is not None else None
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build requires Apple silicon macOS")
    if output is not None and (output.suffix != ".dmg" or output.exists() or output.is_symlink()):
        raise SystemExit("Select a fresh .dmg output path")
    if (notary_profile is not None or keychain is not None) and sign_identity is None:
        raise SystemExit("notarization_or_keychain_requires_signing_identity")
    if notary_profile is not None and not notary_profile.strip():
        raise SystemExit("notary_profile_required")
    certificate_hash = developer_identity(sign_identity, keychain) if sign_identity is not None else None
    if supplied_textgen and certificate_hash is not None:
        verify_host(app / "Contents/Resources/host", certificate=certificate_hash)
    if supplied_commands is not None and command_profile(app, certificate_hash) != supplied_commands:
        raise SystemExit("signed_command_helpers_changed")
    signed, notarized = certificate_hash is not None, notary_profile is not None
    scratch = ROOT / ".build"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meecp-dmg-app-", dir=scratch) as temporary:
        workspace = Path(temporary)
        source = workspace / "meecp.app"
        if app is not None:
            # Notarization staples a ticket; operate on a copy of supplied artifacts.
            run("/usr/bin/ditto", str(app.resolve(strict=True)), str(source))
        else:
            app_options = {"sign_identity": certificate_hash, "keychain": keychain}
            if version is not None:
                app_options["version"] = version
            if textgen_engine is not None:
                app_options["textgen_engine"] = textgen_engine
            if textgen_signed_asset is not None:
                app_options["textgen_signed_asset"] = textgen_signed_asset
            if command_signed_asset is not None:
                app_options["command_signed_asset"] = command_signed_asset
            build_app(source, **app_options)
        info = app_metadata(source, certificate_hash)
        source_commands = command_profile(source, certificate_hash)
        if supplied_commands is not None and source_commands != supplied_commands:
            raise SystemExit("signed_command_helpers_changed")
        if certificate_hash is not None and (source / "Contents/Resources/host/preparation-toolchain/textgen-runtime").exists():
            from signed_textgen_asset import verify_host
            verify_host(source / "Contents/Resources/host", certificate=certificate_hash)
        if version is not None and info["CFBundleShortVersionString"] != version:
            raise SystemExit("supplied_app_version_mismatch")
        app_receipt = json.loads((source / "Contents/Resources/app-build.json").read_bytes())
        if app_receipt.get("signing") != ("developer_id" if signed else "ad_hoc"):
            raise SystemExit("app_signing_receipt_mismatch")
        app_receipt = {**app_receipt, "notarization": "not_requested"}
        destination = (output or ROOT / "dist" / f"meecp-{info['CFBundleShortVersionString']}-arm64.dmg").absolute()
        receipt_path = destination.with_name(destination.name + ".build.json")
        if (destination.exists() or destination.is_symlink()
                or receipt_path.exists() or receipt_path.is_symlink()):
            raise SystemExit("Destination or receipt exists; select a fresh output path")
        app_notary_result = None
        if notarized:
            archive = workspace / "meecp.zip"
            run("/usr/bin/ditto", "-c", "-k", "--keepParent", str(source), str(archive))
            app_notary_result = notarize(archive, notary_profile, keychain)
            staple(source)
            app_metadata(source, certificate_hash)
            if command_profile(source, certificate_hash) != source_commands:
                raise SystemExit("signed_command_helpers_changed")
            run("/usr/sbin/spctl", "--assess", "--type", "exec", str(source))
            # Keep the embedded receipt sealed; final distribution receipt records
            # the notarization that happened after the application was signed.
            app_receipt = {**app_receipt, "notarization": "accepted",
                           "notary_submission_id": app_notary_result["submission_id"],
                           "notarization_ticket_stapled": True}
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the image on its destination filesystem for exclusive publication.
        with tempfile.TemporaryDirectory(prefix=".meecp-dmg-", dir=destination.parent) as image_temporary:
            image_workspace = Path(image_temporary)
            stage = image_workspace / "contents"
            stage.mkdir()
            run("/usr/bin/ditto", str(source), str(stage / "meecp.app"))
            (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
            staged_image = image_workspace / "meecp.dmg"
            run("/usr/bin/hdiutil", "create", "-volname", "meecp", "-srcfolder", str(stage),
                "-fs", "HFS+", "-format", "UDZO", "-imagekey", "zlib-level=9", str(staged_image))
            run("/usr/bin/hdiutil", "verify", str(staged_image))
            image_notary_result = None
            if signed:
                sign_disk_image(staged_image, certificate_hash, keychain)
            if notarized:
                image_notary_result = notarize(staged_image, notary_profile, keychain)
                staple(staged_image)
                run("/usr/sbin/spctl", "--assess", "--type", "open",
                    "--context", "context:primary-signature", str(staged_image))
            run("/usr/bin/hdiutil", "verify", str(staged_image))
            if signed:
                verify_signature(staged_image, certificate_hash)
            checks = verify_image(staged_image, image_workspace, info,
                                  certificate_hash, notarized)
            distribution = ("notarized_drag_install_dmg" if notarized else
                            "developer_id_drag_install_dmg" if signed else "development_drag_install_dmg")
            receipt = {"schema_version": 1, "distribution": distribution,
                       "notarization": "accepted" if notarized else "not_requested", "disk_image_signed": signed,
                       "version": info["CFBundleShortVersionString"], "build": info["CFBundleVersion"],
                       "minimum_macos": info["LSMinimumSystemVersion"], "architecture": "arm64",
                       "app_build": app_receipt, "checks": checks,
                       "sha256": sha256(staged_image), "bytes": staged_image.stat().st_size}
            if image_notary_result is not None:
                receipt.update({"notary_submission_id": image_notary_result["submission_id"],
                                "notarization_ticket_stapled": True})
            staged_receipt = image_workspace / "build.json"
            staged_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            publish_outputs([(staged_image, destination), (staged_receipt, receipt_path)])
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Package an existing app instead of building current source")
    parser.add_argument("--output", type=Path, help="Fresh .dmg path; defaults to dist/meecp-VERSION-arm64.dmg")
    parser.add_argument("--sign-identity", help="Developer ID Application name or certificate SHA-1")
    parser.add_argument("--notary-profile", help="notarytool Keychain profile; requires --sign-identity")
    parser.add_argument("--keychain", type=Path, help="Keychain containing signing identity and notary profile")
    parser.add_argument("--version", help="Override the application version (major.minor.patch)")
    parser.add_argument("--textgen-engine", type=Path, help="Exact reviewed engine Resources directory; requires source build")
    parser.add_argument("--textgen-signed-asset", type=Path, help="Exact immutable signed TextGen runtime archive; requires source build")
    parser.add_argument("--command-signed-asset", type=Path, help="Exact immutable signed command helper archive; requires source build")
    arguments = parser.parse_args()
    try:
        print(build(arguments.output, arguments.app, arguments.sign_identity,
                    arguments.notary_profile, arguments.keychain,
                    version=arguments.version, textgen_engine=arguments.textgen_engine,
                    textgen_signed_asset=arguments.textgen_signed_asset,
                    command_signed_asset=arguments.command_signed_asset))
    except KeyboardInterrupt:
        raise SystemExit("dmg_build_interrupted") from None
    except BuildCommandError as error:
        raise SystemExit(error.diagnostic) from None
    except Exception:
        raise SystemExit("dmg_build_failed") from None
