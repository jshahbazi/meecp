"""Prepare the standalone deterministic example; never runs inside the installer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(destination: Path, *, idle_seconds: int = 300) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    source = ROOT / "examples/json-utilities"
    shutil.copyfile(source / "main.swift", destination / "main.swift")
    shutil.copyfile(source / "LICENSE", destination / "LICENSE")
    subprocess.run(["/usr/bin/swiftc", "-O", "-target", "arm64-apple-macosx26.0", str(destination / "main.swift"),
                    "-o", str(destination / "json-utilities")], check=True, capture_output=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination / "json-utilities")],
                   check=True, capture_output=True)
    toolchain = subprocess.check_output(["/usr/bin/swiftc", "--version"], text=True).splitlines()[0]
    schema = {"type": "object", "properties": {"document": {"type": "string"}},
              "required": ["document"], "additionalProperties": False}
    output = {"type": "object", "properties": {"valid": {"type": "boolean"}, "kind": {"type": "string"}},
              "required": ["valid", "kind"], "additionalProperties": False}
    source_revision = "sha256:" + digest(destination / "main.swift")
    manifest = {
        "schema_version": 1, "host_api_version": 1, "package_id": "local.json-utilities", "version": "1.0.0",
        "source_revision": source_revision, "name": "JSON Utilities", "profile": "mcp-stdio",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "entrypoint": {"executable": "json-utilities", "args": [], "cwd": "."},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "lifecycle": {"max_sessions": 8, "idle_timeout_seconds": idle_seconds, "operation_timeout_seconds": 30,
                      "startup_timeout_seconds": 10, "stop_grace_seconds": 2},
        "tools": [{"name": "validate_json", "description": "Validate a JSON document and report its top-level type.",
                   "input_schema": schema, "output_schema": output}],
        "access": {"filesystem": [], "network": [], "secrets": [], "writes": False},
        "resources": {"memory_mb": 32, "cpu_only": True}, "licenses": ["LICENSE"],
    }
    (destination / "mcpme.package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    artifacts = [{"path": name, "sha256": digest(destination / name), "size": (destination / name).stat().st_size,
                  "executable": name == "json-utilities", "source": source_revision,
                  "provenance": "Locally compiled from the included main.swift using the recorded Swift toolchain.",
                  "license": "LICENSE"} for name in ["main.swift", "LICENSE", "json-utilities"]]
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
            "manifest_sha256": digest(destination / "mcpme.package.json"), "artifacts": artifacts,
            "build": {"recipe": "swiftc -O -target arm64-apple-macosx26.0 main.swift -o json-utilities; ad hoc codesign",
                      "builder": "mcpme prepare_json_package.py v1", "toolchain": toolchain}}
    (destination / "mcpme.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/json-utilities-package")
    parser.add_argument("--idle-seconds", type=int, default=300)
    args = parser.parse_args()
    prepare(args.output.absolute(), idle_seconds=args.idle_seconds)
    print("Prepared " + str(args.output.absolute()))
