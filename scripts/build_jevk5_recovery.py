"""Clone a legacy JevK5 bundle and refresh its trusted backend transport."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

from build_mcpme_host import ROOT, run


def clone_file(source: str, destination: str) -> str:
    result = subprocess.run(["/bin/cp", "-c", source, destination], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        shutil.copy2(source, destination)
    return destination


def replace_tree(destination: Path, source: Path, root: Path) -> None:
    """Replace a copied tree without ever following a legacy symlink outward."""
    parent = destination.parent.resolve()
    if not parent.is_relative_to(root.resolve()):
        raise ValueError("recovery_destination_escape")
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def replace_file(destination: Path, source: Path, root: Path) -> None:
    if not destination.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError("recovery_destination_escape")
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        raise ValueError("recovery_file_collision")
    shutil.copy2(source, destination)


def build(source: Path, destination: Path) -> Path:
    source = source.resolve()
    if destination.exists():
        raise SystemExit("Destination exists; choose a new recovery path")
    info = plistlib.loads((source / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "com.jshahbazi.jevk5" or info.get("CFBundleExecutable") != "JevK5MenuBar":
        raise SystemExit("Source is not the supported legacy JevK5 bundle")
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(source):
            raise SystemExit("Legacy bundle contains an external symlink")
    scratch = ROOT / ".build/mcpme-native"
    run("/usr/bin/swift", "build", "-c", "release", "--product", "JevK5MenuBar", "--package-path", str(ROOT), "--scratch-path", str(scratch))
    binary_dir = Path(run("/usr/bin/swift", "build", "-c", "release", "--show-bin-path", "--package-path", str(ROOT), "--scratch-path", str(scratch)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, copy_function=clone_file,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    resources = destination / "Contents/Resources"
    replace_tree(resources / "backend/jevk5_app", ROOT / "backend/jevk5_app", destination)
    helper = resources / "backend/mcpme_local_transport.py"
    replace_file(helper, ROOT / "backend/mcpme_local_transport.py", destination)
    launcher = destination / "Contents/MacOS/jevk5-mcp"
    replace_file(launcher, ROOT / "scripts/jevk5-mcp", destination)
    launcher.chmod(0o755)
    binary = destination / "Contents/MacOS/JevK5MenuBar"
    replace_file(binary, binary_dir / "JevK5MenuBar", destination)
    run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(destination))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(destination))
    receipt = {"schema_version": 1, "source_bundle": str(source), "recovery_bundle": str(destination),
               "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
               "bundle_identifier": "com.jshahbazi.jevk5",
               "native_executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "changes": ["native_login_handoff_commands", "updated_backend_private_transport_and_safe_import_proxy_fixes",
                           "updated_bundled_launcher"], "signing": "ad_hoc"}
    destination.with_name(destination.name + ".build.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "dist/JevK5.app")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/JevK5-recovery.app")
    arguments = parser.parse_args()
    try:
        print(build(arguments.source, arguments.output.absolute()))
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit("recovery_build_failed") from None
