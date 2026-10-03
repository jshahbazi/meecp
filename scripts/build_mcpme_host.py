"""Build the relocatable CLI host with a private Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.13"
MAX_RUNTIME_FILES = 16384
MAX_RUNTIME_BYTES = 512 * 1024**2


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


def bundle_preparation_toolchain(destination: Path, runtime: Path) -> None:
    """Bundle deterministic installed-host build tools without PATH discovery."""
    from build_preparation_toolchain import bundle
    bundle(destination / "preparation-toolchain", runtime, template_root=ROOT / "templates")


def bundle_textgen_backend(destination: Path) -> None:
    from bundle_textgen_assets import bundle_backend
    bundle_backend(destination / "backend")


def bundle_textgen_toolchain(destination: Path, engine: Path) -> None:
    from bundle_textgen_assets import bundle
    bundle(destination, engine)


def bundle_command_helper(destination: Path) -> None:
    """Ship reviewed ownership bytes without enabling the experimental profile.

    This is an inventory, not an admission configuration. No runtime path reads
    it to construct HostCommandAdmission; the production constructor stays closed.
    """
    source = ROOT / "scripts/mcpme-command-reap.c"
    helper = destination / "bin/mcpme-command-reap"
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(helper))
    run("/usr/bin/codesign", "--force", "--sign", "-", str(helper))
    (destination / "command-execution.json").write_text(json.dumps({
        "schema_version": 1, "package_schema": 2, "profile": "host-command",
        "component": "macos26-arm64-command-v3", "receipt_format": 3,
        "helper": "bin/mcpme-command-reap", "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "helper_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "platform": {"os": "macos", "major": 26, "architecture": "arm64"},
        "production_admission": "disabled", "configuration_authority": "inventory-only",
        "open_gates": ["aggregate-process-memory-enforcement", "signed-relocated-native-app"]},
        sort_keys=True, indent=2) + "\n")


def refresh_command_inventory(destination: Path) -> None:
    """Refresh only the artifact hash after distribution signing changes bytes."""
    path = destination / "command-execution.json"
    value = json.loads(path.read_bytes())
    if (value.get("helper") != "bin/mcpme-command-reap" or value.get("production_admission") != "disabled"
            or value.get("configuration_authority") != "inventory-only"):
        raise ValueError("command_inventory_invalid")
    value["helper_sha256"] = hashlib.sha256((destination / "bin/mcpme-command-reap").read_bytes()).hexdigest()
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _resource_inventory_contract() -> dict:
    return {
        "schema_version": 1, "component": "macos26-arm64-command-resource-v1", "receipt_format": 4,
        "helper": "bin/mcpme-command-resource-reap", "resource_policy": "held-group-windowed-usage-v1",
        "confinement_policy": "macos26-arm64-monitored-fork-exec-v2",
        "platform": {"os": "macos", "major": 26, "architecture": "arm64"},
        "production_admission": "disabled", "configuration_authority": "inventory-only",
        "lifetime_coverage_complete": False, "hard_aggregate_enforced": False, "result_admissible": False,
        "open_gates": ["complete-lifetime-accounting", "aggregate-process-memory-enforcement",
                       "release-platform-acceptance", "signed-relocated-native-app"],
    }


def bundle_command_resource_helper(destination: Path) -> None:
    """Package the report-only monitor without granting execution admission."""
    source = ROOT / "scripts/mcpme-command-resource-reap.c"
    header = ROOT / "scripts/mcpme-command-resource-v1.h"
    value = _resource_inventory_contract()
    helper = destination / value["helper"]
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(helper))
    run("/usr/bin/codesign", "--force", "--sign", "-", str(helper))
    run("/usr/bin/codesign", "--verify", "--strict", str(helper))
    value.update(helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                 helper_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 helper_header_sha256=hashlib.sha256(header.read_bytes()).hexdigest())
    (destination / "command-resource-execution.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def refresh_command_resource_inventory(destination: Path) -> None:
    """Rebind signed artifact bytes without changing source identities or gates."""
    path = destination / "command-resource-execution.json"
    value = json.loads(path.read_bytes())
    contract = _resource_inventory_contract()
    hashes = {"helper_sha256", "helper_source_sha256", "helper_header_sha256"}
    if (not isinstance(value, dict) or set(value) != set(contract) | hashes
            or any(type(value.get(key)) is not type(expected) or value[key] != expected
                   for key, expected in contract.items())
            or any(not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
                   for key in hashes)):
        raise ValueError("command_resource_inventory_invalid")
    value["helper_sha256"] = hashlib.sha256((destination / contract["helper"]).read_bytes()).hexdigest()
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _resolved_runtime_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError:
        # Python 3.12 reports symlink cycles as RuntimeError, outside the build
        # CLI's OSError/ValueError diagnostic boundary. Normalize only this seam.
        raise ValueError("private_python_runtime_invalid") from None


def copy_private_runtime(runtime: Path, destination: Path) -> None:
    """Copy bounded regular bytes, resolving only internal file links.

    Directory links are unsupported, avoiding traversal cycles and implicit
    closure expansion. The selected managed runtime contains only file links.
    """
    root = _resolved_runtime_path(runtime)
    def identity(details):
        return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns)
    records, total, count = [], 0, 0
    for path in root.rglob("*"):
        count += 1
        if count > MAX_RUNTIME_FILES:
            raise ValueError("private_python_runtime_limit")
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        target = _resolved_runtime_path(path)
        if not target.is_relative_to(root):
            raise ValueError("private_python_runtime_escape")
        details = target.stat()
        mode = details.st_mode
        if stat.S_ISDIR(mode) and not path.is_symlink():
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("private_python_runtime_type")
        size = details.st_size
        total += size
        if total > MAX_RUNTIME_BYTES:
            raise ValueError("private_python_runtime_limit")
        records.append((relative, target, size, mode, identity(details)))
    destination.mkdir()
    for relative, source, size, mode, expected in records:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # A substituted FIFO must not block open before the regular-file check.
        # Pin the validated inode/size/timestamps on the held descriptor both
        # before and after the bounded read; final symlinks cannot be followed.
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as read:
            opened = os.fstat(read.fileno())
            if not stat.S_ISREG(opened.st_mode) or identity(opened) != expected:
                raise ValueError("private_python_runtime_changed")
            with target.open("xb") as write:
                remaining = size
                while remaining:
                    chunk = read.read(min(1024**2, remaining))
                    if not chunk:
                        raise ValueError("private_python_runtime_changed")
                    write.write(chunk); remaining -= len(chunk)
                if read.read(1) or identity(os.fstat(read.fileno())) != expected:
                    raise ValueError("private_python_runtime_changed")
        target.chmod(stat.S_IMODE(mode))


def build(destination: Path, version: str | None = None, *, textgen_engine: Path | None = None):
    version = project_version(version)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build requires Apple silicon macOS")
    if destination.exists():
        raise SystemExit("Destination already exists; select a new output directory")
    destination.mkdir(parents=True)
    runtime = _resolved_runtime_path(Path(run("uv", "python", "find", "--managed-python", PYTHON_VERSION))).parent.parent
    copy_private_runtime(runtime, destination / "python")
    shutil.copytree(ROOT / "backend/mcpme", destination / "backend/mcpme",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "backend/mcpme_local_transport.py", destination / "backend/mcpme_local_transport.py")
    bundle_textgen_backend(destination)
    (destination / "bin").mkdir()
    run("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(ROOT / "scripts/mcpme-reap.c"), "-o", str(destination / "bin/mcpme-reap"))
    bundle_command_helper(destination)
    bundle_command_resource_helper(destination)
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
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    bundle_model_template_assets(destination)
    bundle_preparation_toolchain(destination, runtime)
    if textgen_engine is not None:
        bundle_textgen_toolchain(destination, textgen_engine)
    shutil.copyfile(ROOT / "README.md", destination / "README.md")
    shutil.copytree(ROOT / "assets/brand", destination / "assets/brand")
    shutil.copytree(ROOT / "docs", destination / "docs",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    licenses = destination / "licenses"
    licenses.mkdir()
    shutil.copyfile(runtime / "lib/python3.12/LICENSE.txt", licenses / "python-LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    run("/usr/bin/codesign", "--force", "--sign", "-", str(destination / "bin/mcpme-reap"))
    # Freeze the whole payload, including Python, installed wheels, launchers and
    # licenses. build.json is created only after this list, so it cannot hash itself.
    source_files = [path for path in sorted(destination.rglob("*")) if path.is_file()]
    source_hashes = {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in source_files}
    (destination / "build.json").write_text(json.dumps({"host_version": version, "artifact_sha256": source_hashes,
        "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "source_tree_dirty": bool(run("git", "-C", str(ROOT), "status", "--porcelain")),
        "python": run(str(runtime / "bin/python3.12"), "--version"),
        "toolchain": run("/usr/bin/cc", "--version").splitlines()[0],
        "signing": "ad_hoc_native_helpers", "distribution": "development_cli_milestone"}, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mcpme-host")
    parser.add_argument("--textgen-engine", type=Path, help="Exact reviewed engine Resources directory for private CPU text preparation")
    args = parser.parse_args()
    try:
        print(build(args.output.absolute(), textgen_engine=args.textgen_engine))
    except KeyboardInterrupt:
        raise SystemExit("host_build_interrupted") from None
    except BuildCommandError as failure:
        raise SystemExit(failure.diagnostic) from None
    except (OSError, ValueError):
        raise SystemExit("host_build_failed") from None
