"""Distribution-time bundling of the reviewed text engine and adapter closure.

The caller explicitly supplies the exact relocated engine. This helper never
searches an app bundle, Homebrew, a checkout runtime, or PATH for engine bytes.
Call after bundle_preparation_toolchain and before hashing/signing the host.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILES = ("__init__.py", "server.py", "contracts.py", "state.py")


def bundle_backend(destination: Path):
    """Copy the static family implementation even without an admitted engine."""
    textgen_files = tuple(sorted((ROOT / "backend/meecp_textgen").glob("*.py")))
    legacy_files = tuple(sorted((ROOT / "backend/mcpme_textgen").glob("*.py")))
    for name, files in (("meecp_textgen", textgen_files), ("mcpme_textgen", legacy_files),
                        ("jevk5_app", tuple(ROOT / "backend/jevk5_app" / name for name in POLICY_FILES))):
        target = destination / name
        target.mkdir(parents=True, exist_ok=True)
        for source in files:
            copied = target / source.name
            if copied.exists():
                if copied.is_symlink() or copied.read_bytes() != source.read_bytes():
                    raise ValueError("textgen_backend_identity_mismatch")
            else:
                shutil.copyfile(source, copied)


def verify_runtime(host: Path):
    """Require the reviewed engine bytes after distribution-time signing."""
    target = host / "preparation-toolchain/textgen-runtime"
    closure = json.loads((ROOT / "templates/text-generate-v1/engine-closure.json").read_bytes())
    origin = json.loads((target / "origin.json").read_bytes())
    if (origin.get("engine_closure") != closure or origin.get("launcher_source_sha256") !=
            hashlib.sha256((ROOT / "templates/text-generate-v1/launcher.c").read_bytes()).hexdigest()):
        raise ValueError("textgen_runtime_identity_mismatch")
    for name, record in closure.items():
        if name == "llama/llama-server":
            continue
        path = target / name
        if path.resolve() != path or not path.is_file() or path.stat().st_size != record["bytes"]:
            raise ValueError("textgen_runtime_identity_mismatch")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                raise ValueError("textgen_runtime_identity_mismatch")


def bundle(host: Path, engine: Path):
    from build_preparation_toolchain import refresh_toolchain_manifest
    import sys
    sys.path.insert(0, str(ROOT / "backend"))
    from meecp.model_intake import copy_artifact
    target = host / "preparation-toolchain/textgen-runtime"
    if target.exists() or not (host / "preparation-toolchain/toolchain.json").is_file():
        raise ValueError("textgen_assets_destination_invalid")
    closure = json.loads((ROOT / "templates/text-generate-v1/engine-closure.json").read_bytes())
    target.mkdir(mode=0o700)
    for name, record in closure.items():
        copy_artifact(engine / name, None if name == "llama/llama-server" else target / name,
            limit=128 * 1024**2, role="runtime-file", expected_sha256=record["sha256"], expected_size=record["bytes"])
    for destination in (host / "backend", target / "backend"):
        bundle_backend(destination)
    (target / "bin").mkdir()
    source = ROOT / "templates/text-generate-v1/launcher.c"
    for arguments in (("/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                       str(source), "-o", str(target / "bin/textgen-engine")),
                      ("/usr/bin/codesign", "--force", "--sign", "-", str(target / "bin/textgen-engine"))):
        result = subprocess.run(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise ValueError("textgen_launcher_build_failed")
    shutil.copyfile(target / "bin/textgen-engine", target / "bin/textgen-adapter")
    (target / "bin/textgen-adapter").chmod(0o755)
    (target / "origin.json").write_text(json.dumps({"schema_version": 1,
        "engine_closure": closure, "launcher_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "engine_policy": "Exact reviewed CPU closure; legacy supervisor verified and omitted; direct host ownership."},
        sort_keys=True, indent=2) + "\n")
    refresh_toolchain_manifest(host / "preparation-toolchain")
    verify_runtime(host)
    return target
