"""Build the relocatable CLI host with a private Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.13"


def project_version(version: str | None = None) -> str:
    if version is None:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise SystemExit("release_version_requires_major_minor_patch")
    return version


# Only these fixed labels can enter a build failure diagnostic. Arguments and
# tool output are used for classification, never copied into the exception.
_COMMAND_OPERATIONS = {
    "codesign": (("--verify", "codesign_verify"), ("--sign", "codesign_sign"),
                 ("-s", "codesign_sign"), ("--display", "codesign_display"),
                 ("-d", "codesign_display"), (None, "codesign")),
    "security": tuple((command, "security_" + command.replace("-", "_")) for command in (
        "find-identity", "create-keychain", "unlock-keychain", "delete-keychain",
        "import", "set-key-partition-list", "list-keychains", "find-certificate"))
        + ((None, "security"),),
    "hdiutil": tuple((command, "hdiutil_" + command) for command in (
        "create", "attach", "detach", "verify")) + ((None, "hdiutil"),),
    "swift": (("--show-bin-path", "swift_build_path"), ("build", "swift_build"),
              (None, "swift")),
    "swiftc": ((None, "swiftc_compile"),),
    "uv": (("find", "uv_python_find"), ("export", "uv_export"),
           ("install", "uv_pip_install"), ("sync", "uv_sync"), (None, "uv")),
    "cc": (("--version", "cc_version"), (None, "cc_compile")),
    "git": (("rev-parse", "git_revision"), ("status", "git_status"), (None, "git")),
    "python3.12": (("--version", "python_version"), (None, "python")),
    "ditto": (("-c", "ditto_archive"), (None, "ditto_copy")),
    "lipo": ((None, "lipo_architecture"),),
    "otool": ((None, "otool_dependencies"),),
    "install_name_tool": ((None, "install_name_tool"),),
    "iconutil": ((None, "iconutil_convert"),),
    "mcpme-icon": ((None, "icon_generate"),),
    "spctl": ((None, "spctl_assess"),),
    "xcrun": ((None, "xcrun"),),
}
_XCRUN_OPERATIONS = {
    ("notarytool", "submit"): "notarytool_submit",
    ("notarytool", "store-credentials"): "notarytool_store_credentials",
    ("notarytool", "log"): "notarytool_log",
    ("notarytool", "info"): "notarytool_info",
    ("stapler", "staple"): "stapler_staple",
    ("stapler", "validate"): "stapler_validate",
}
_ALLOWED_OPERATIONS = frozenset({"unknown_tool", *_XCRUN_OPERATIONS.values(),
    *(label for operations in _COMMAND_OPERATIONS.values() for _, label in operations)})
_SIGNING_FAILURES = {
    "user interaction is not allowed": "interaction_not_allowed",
    "errsecinternalcomponent": "internal_component",
    "specified item could not be found in the keychain": "keychain_item_missing",
    "no identity found": "identity_missing",
    "unable to build chain to self-signed root": "certificate_chain_invalid",
}
_ALLOWED_CATEGORIES = frozenset({*_SIGNING_FAILURES.values(), "input_unreadable"})


class BuildCommandError(subprocess.CalledProcessError):
    """A failed build command with no command arguments or captured output."""

    def __init__(self, operation: str, returncode: int, category: str | None = None):
        operation = operation if operation in _ALLOWED_OPERATIONS else "unknown_tool"
        returncode = returncode if type(returncode) is int else 1
        self._diagnostic = f"build_command_{operation}_failed_exit_{returncode}"
        if category in _ALLOWED_CATEGORIES:
            self._diagnostic += "_" + category
        super().__init__(returncode, operation)
        self.args = (self._diagnostic,)

    @property
    def diagnostic(self) -> str:
        return self._diagnostic

    def __str__(self) -> str:
        return self.diagnostic


def _command_operation(arguments: tuple[str, ...]) -> str:
    tool = Path(arguments[0]).name if arguments else ""
    if tool == "xcrun":
        return _XCRUN_OPERATIONS.get(arguments[1:3], "xcrun")
    tokens = arguments[1:]
    if tool in {"security", "hdiutil"}:
        tokens = arguments[1:2]
    elif tool == "uv":
        if arguments[1:3] == ("python", "find"):
            return "uv_python_find"
        if arguments[1:3] == ("pip", "install"):
            return "uv_pip_install"
        tokens = arguments[1:2] if arguments[1:2] in (("export",), ("sync",)) else ()
    for token, label in _COMMAND_OPERATIONS.get(tool, ()):
        if token is None or token in tokens:
            return label
    return "unknown_tool"


def _failure_category(operation: str, stderr: str | bytes | None) -> str | None:
    if not operation.startswith(("codesign", "security")) or stderr is None:
        return None
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    lowered = stderr.lower()
    if operation == "codesign_verify" and any(marker in lowered for marker in (
        "cannot read", "no such file or directory")):
        return "input_unreadable"
    return next((category for marker, category in _SIGNING_FAILURES.items() if marker in lowered), None)


def run(*args: str) -> str:
    operation = _command_operation(args)
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as failure:
        returncode = failure.returncode
        category = _failure_category(operation, failure.stderr)
    # Raise after leaving the handler so the raw exception does not become an
    # implicit exception context containing arguments, stdout, or stderr.
    raise BuildCommandError(operation, returncode, category) from None


def write_launcher(launcher: Path):
    launcher.write_text('#!/bin/sh\nset -eu\nBIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                        'MCPME_ROOT=$(CDPATH= cd -- "$BIN/.." && pwd)\n'
                        'unset PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP PYTHONINSPECT\n'
                        'export PYTHONPATH="$MCPME_ROOT/backend:$MCPME_ROOT/python/site-packages"\n'
                        'export PYTHONNOUSERSITE=1\n'
                        'export PYTHONDONTWRITEBYTECODE=1\n'
                        'exec "$MCPME_ROOT/python/bin/python3.12" -P -m mcpme "$@"\n')
    launcher.chmod(0o755)


def bundle_model_template_assets(destination: Path) -> None:
    """Ship the versioned generator's source closure without a checkout dependency."""
    assets = destination / "templates/jevk5-llamacpp-v1/assets"
    assets.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("jevk5_app", "mcpme_jevk5"):
        shutil.copytree(ROOT / "backend" / name, assets / "backend" / name, ignore=ignore)
    shutil.copyfile(ROOT / "backend/mcpme_local_transport.py", assets / "backend/mcpme_local_transport.py")
    shutil.copytree(ROOT / "vendor", assets / "vendor", ignore=ignore)
    for name in ("model-manifest.json", "THIRD_PARTY_NOTICES.md", "uv.lock"):
        shutil.copyfile(ROOT / name, assets / name)
    (assets / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/jevk5-package-launcher.c", assets / "scripts/jevk5-package-launcher.c")


def build(destination: Path, version: str | None = None):
    version = project_version(version)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build requires Apple silicon macOS")
    if destination.exists():
        raise SystemExit("Destination already exists; select a new output directory")
    destination.mkdir(parents=True)
    runtime = Path(run("uv", "python", "find", "--managed-python", PYTHON_VERSION)).resolve().parent.parent
    shutil.copytree(runtime, destination / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "backend/mcpme", destination / "backend/mcpme",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "backend/mcpme_local_transport.py", destination / "backend/mcpme_local_transport.py")
    (destination / "bin").mkdir()
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(ROOT / "scripts/mcpme-reap.c"), "-o", str(destination / "bin/mcpme-reap"))
    requirements = destination / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
        "--output-file", str(requirements))
    run("uv", "pip", "install", "--python", str(runtime / "bin/python3.12"),
        "--target", str(destination / "python/site-packages"), "--require-hashes", "--requirement", str(requirements))
    launcher = destination / "bin/mcpme"
    write_launcher(launcher)
    shutil.copyfile(ROOT / "uv.lock", destination / "uv.lock")
    shutil.copyfile(ROOT / "pyproject.toml", destination / "pyproject.toml")
    shutil.copytree(ROOT / "templates", destination / "templates",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundle_model_template_assets(destination)
    shutil.copyfile(ROOT / "README.md", destination / "README.md")
    shutil.copytree(ROOT / "assets/brand", destination / "assets/brand")
    (destination / "docs").mkdir()
    for document in sorted((ROOT / "docs").glob("*.md")):
        shutil.copyfile(document, destination / "docs" / document.name)
    licenses = destination / "licenses"
    licenses.mkdir()
    shutil.copyfile(runtime / "lib/python3.12/LICENSE.txt", licenses / "python-LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    run("/usr/bin/codesign", "--force", "--sign", "-", str(destination / "bin/mcpme-reap"))
    source_files = [*[path for path in sorted((destination / "backend").rglob("*")) if path.is_file()],
                    *[path for path in sorted((destination / "assets/brand").rglob("*")) if path.is_file()],
                    *sorted((destination / "docs").glob("*.md")), destination / "uv.lock",
                    destination / "pyproject.toml", *[path for path in sorted((destination / "templates").rglob("*")) if path.is_file()],
                    destination / "requirements.txt", destination / "README.md", destination / "bin/mcpme-reap"]
    source_hashes = {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in source_files}
    (destination / "build.json").write_text(json.dumps({"host_version": version, "artifact_sha256": source_hashes,
        "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "source_tree_dirty": bool(run("git", "-C", str(ROOT), "status", "--porcelain")),
        "python": run(str(runtime / "bin/python3.12"), "--version"),
        "toolchain": run("/usr/bin/cc", "--version").splitlines()[0],
        "signing": "ad_hoc_reaper_only", "distribution": "development_cli_milestone"}, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mcpme-host")
    args = parser.parse_args()
    try:
        print(build(args.output.absolute()))
    except KeyboardInterrupt:
        raise SystemExit("host_build_interrupted") from None
    except BuildCommandError as failure:
        raise SystemExit(failure.diagnostic) from None
    except (OSError, ValueError):
        raise SystemExit("host_build_failed") from None
