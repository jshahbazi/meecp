"""Build and verify a local-development drag-to-Applications disk image."""
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

from build_mcpme_app import build as build_app
from build_mcpme_host import ROOT, run


INSTALLATION = """# Install mcpme

Requires an Apple silicon Mac running macOS 26 or later.

1. Quit any running copy of mcpme before replacing it.
2. Drag mcpme.app onto the Applications shortcut.
3. Eject this disk image and open mcpme from Applications.
4. Look for mcpme in the menu bar, then choose Add Project or Manage Projects.

This is a local development build with an ad hoc code signature. It has not
been submitted to Apple for notarization. A downloaded copy may be blocked
by Gatekeeper; this image is not a notarized public release.

The app includes its runtime. Running it requires no uv, Homebrew, Python
installation, or source checkout. Tool packages and models are added separately.

Projects and settings live in ~/Library/Application Support/mcpme. Replacing
the app preserves them. Copy connection configuration after installing at the
final location: client configurations include the app's absolute path.
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_metadata(app: Path) -> dict:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (info.get("CFBundleIdentifier") != "com.jshahbazi.mcpme"
            or info.get("CFBundleExecutable") != "mcpme"
            or info.get("CFBundlePackageType") != "APPL"
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", info.get("CFBundleShortVersionString", ""))):
        raise SystemExit("Input must be a built mcpme application")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    if run("/usr/bin/lipo", "-archs", str(app / "Contents/MacOS/mcpme")) != "arm64":
        raise SystemExit("Input must be an arm64 mcpme application")
    return info


def verify_image(image: Path, workspace: Path, info: dict) -> dict:
    """Mount read-only, simulate a drag install, and exercise the private runtime."""
    mount = workspace / "mounted"
    mount.mkdir()
    run("/usr/bin/hdiutil", "attach", str(image), "-readonly", "-nobrowse",
        "-noautoopen", "-mountpoint", str(mount))
    try:
        shortcut = mount / "Applications"
        if not shortcut.is_symlink() or os.readlink(shortcut) != "/Applications":
            raise SystemExit("Disk image is missing the Applications shortcut")
        if (mount / "Install.md").read_text() != INSTALLATION:
            raise SystemExit("Disk image installation instructions changed")
        if app_metadata(mount / "mcpme.app") != info:
            raise SystemExit("Disk image app metadata changed")
        installed = workspace / "test Applications/mcpme.app"
        installed.parent.mkdir()
        run("/usr/bin/ditto", str(mount / "mcpme.app"), str(installed))
    finally:
        run("/usr/bin/hdiutil", "detach", str(mount))

    # Verify after ejecting, from a path with spaces and without developer tools
    # in PATH. Never launch the user's real installation or touch its state.
    app_metadata(installed)
    home = workspace / "test home"
    home.mkdir(mode=0o700)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "LANG": "en_US.UTF-8", "TMPDIR": str(workspace)}
    subprocess.run([str(installed / "Contents/Resources/host/bin/mcpme"), "--help"],
                   cwd=home, env=environment, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=30)
    return {"image_checksum": True, "read_only_mount": True, "applications_shortcut": True,
            "mounted_app_signature": True, "copied_app_signature": True,
            "ejected_image": True, "private_runtime_minimal_environment": True}


def build(output: Path | None = None, app: Path | None = None) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build requires Apple silicon macOS")
    if output is not None and (output.suffix != ".dmg" or output.exists()):
        raise SystemExit("Select a fresh .dmg output path")
    scratch = ROOT / ".build"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcpme-dmg-app-", dir=scratch) as temporary:
        workspace = Path(temporary)
        source = app.resolve(strict=True) if app else build_app(workspace / "mcpme.app")
        info = app_metadata(source)
        destination = (output or ROOT / "dist" / f"mcpme-{info['CFBundleShortVersionString']}-arm64.dmg").absolute()
        receipt_path = destination.with_name(destination.name + ".build.json")
        if destination.exists() or receipt_path.exists():
            raise SystemExit("Destination or receipt exists; select a fresh output path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the image on its destination filesystem for exclusive publication.
        with tempfile.TemporaryDirectory(prefix=".mcpme-dmg-", dir=destination.parent) as image_temporary:
            image_workspace = Path(image_temporary)
            stage = image_workspace / "contents"
            stage.mkdir()
            run("/usr/bin/ditto", str(source), str(stage / "mcpme.app"))
            (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
            (stage / "Install.md").write_text(INSTALLATION)
            staged_image = image_workspace / "mcpme.dmg"
            run("/usr/bin/hdiutil", "create", "-volname", "mcpme", "-srcfolder", str(stage),
                "-fs", "HFS+", "-format", "UDZO", "-imagekey", "zlib-level=9", str(staged_image))
            run("/usr/bin/hdiutil", "verify", str(staged_image))
            checks = verify_image(staged_image, image_workspace, info)
            app_receipt = json.loads((source / "Contents/Resources/app-build.json").read_bytes())
            receipt = {"schema_version": 1, "distribution": "development_drag_install_dmg",
                       "notarization": "not_requested", "disk_image_signed": False,
                       "version": info["CFBundleShortVersionString"], "build": info["CFBundleVersion"],
                       "minimum_macos": info["LSMinimumSystemVersion"], "architecture": "arm64",
                       "app_build": app_receipt, "checks": checks,
                       "sha256": sha256(staged_image), "bytes": staged_image.stat().st_size}
            staged_receipt = image_workspace / "build.json"
            staged_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            os.link(staged_image, destination)
            try:
                os.link(staged_receipt, receipt_path)
            except OSError:
                destination.unlink()
                raise
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Package an existing app instead of building current source")
    parser.add_argument("--output", type=Path, help="Fresh .dmg path; defaults to dist/mcpme-VERSION-arm64.dmg")
    arguments = parser.parse_args()
    try:
        print(build(arguments.output, arguments.app))
    except KeyboardInterrupt:
        raise SystemExit("dmg_build_interrupted") from None
    except Exception:
        raise SystemExit("dmg_build_failed") from None
