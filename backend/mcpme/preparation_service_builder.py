"""Generate and seal a service adapter from exact non-filesystem source data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

from .contracts import PackageManifest, validate_package
from .preparation_builder import _runtime, _copy, _digest, load_private_toolchain
from .preparation_contracts import PreparationError, canonical_bytes

BOOTSTRAP = b'''from pathlib import Path
import sys
root = Path(__file__).resolve().parent
sys.path[:] = [str(root / "backend"), str(root / "python/site-packages"), *[p for p in sys.path if p and (p.endswith("python312.zip") or p.endswith("python3.12") or p.endswith("lib-dynload"))]]
from mcpme_service.server import main
main(root)
'''
LAUNCHER = b'''#!/bin/sh
set -eu
BIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)
exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" "$@" 2>/dev/null
'''


def prepare_service(value, draft, toolchain, output, run, check, *, acquire_wheels):
    from .preparation_service import validate_descriptor
    connection = validate_descriptor(value, draft)
    if not output.is_absolute() or output.parent.resolve() != output.parent or output.exists():
        raise PreparationError("private_preparation_workspace_required")
    current = load_private_toolchain(toolchain.root.parent, check)
    if current.identity != toolchain.identity:
        raise PreparationError("preparation_toolchain_changed")
    root = Path(__file__).resolve().parents[2]
    assets = root / "templates/service-connection-v1"
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        selected = current.root / "templates/service-connection-v1" / name
        if _digest(selected, check) != _digest(assets / name, check):
            raise PreparationError("service_template_identity_mismatch")
    output.mkdir(mode=0o700)
    work = output.parent / (output.name + "-builder"); work.mkdir(mode=0o700)
    runtime = _runtime(output, assets, current, run, check, work, acquire_wheels)
    for path in sorted((root / "backend/mcpme_service").glob("*.py")):
        _copy(path, output / "backend/mcpme_service" / path.name, check)
    _copy(assets / "LICENSE", output / "licenses/LICENSE", check)
    (output / "bootstrap.py").write_bytes(BOOTSTRAP)
    (output / "bin").mkdir(mode=0o700)
    (output / "bin/service").write_bytes(LAUNCHER); (output / "bin/service").chmod(0o700)
    (output / "service.json").write_bytes(canonical_bytes(connection.portable()))
    manifest = PackageManifest.model_validate({"schema_version": 1, "host_api_version": 1,
        "package_id": value.package.id, "version": value.package.version, "name": value.package.name,
        "source_revision": connection.revision, "profile": "mcp-stdio",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "tools": [tool.model_dump(exclude_none=True) for tool in value.tools], "access": value.access.model_dump(),
        "resources": value.resources.model_dump(), "lifecycle": value.lifecycle.model_dump(),
        "validation": {"fixtures": [{"tool": f.tool, "arguments": f.arguments, "expected": f.expected} for f in value.validation]},
        "licenses": [path.relative_to(output).as_posix() for path in sorted((output / "licenses").rglob("*")) if path.is_file()]})
    (output / "mcpme.package.json").write_bytes(canonical_bytes(manifest.model_dump(exclude_none=True)))
    records = []
    for path in sorted(output.rglob("*")):
        check(); info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            path.chmod(0o700); continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PreparationError("unsafe_generated_artifact")
        executable = bool(info.st_mode & 0o111); path.chmod(0o700 if executable else 0o600)
        name = path.relative_to(output).as_posix()
        if name == "mcpme.package.json":
            continue
        license = "licenses/DEPENDENCIES.md" if name.startswith("python/site-packages/") else (
            "licenses/python-LICENSE.txt" if name.startswith("python/") else "licenses/LICENSE")
        records.append({"path": name, "sha256": _digest(path, check), "size": path.stat().st_size, "executable": executable,
            "source": connection.revision, "provenance": "Host-generated service adapter and locked private runtime.", "license": license})
    (output / "mcpme.lock.json").write_text(json.dumps({"schema_version": 1, "package_id": manifest.package_id,
        "version": manifest.version, "manifest_sha256": _digest(output / "mcpme.package.json", check), "artifacts": records,
        "build": {"recipe": "mcpme.local-openai-text 1.0.0", "builder": "mcpme service preparation v1", "toolchain": runtime}},
        sort_keys=True, separators=(",", ":"), allow_nan=False))
    return validate_package(output)
