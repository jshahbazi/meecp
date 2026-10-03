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


def bundle(host: Path, engine: Path):
    from build_preparation_toolchain import refresh_toolchain_manifest
    import sys
    sys.path.insert(0, str(ROOT / "backend"))
    from mcpme.model_intake import copy_artifact
    target = host / "preparation-toolchain/textgen-runtime"
    if target.exists() or not (host / "preparation-toolchain/toolchain.json").is_file():
        raise ValueError("textgen_assets_destination_invalid")
    closure = json.loads((ROOT / "templates/text-generate-v1/engine-closure.json").read_bytes())
    target.mkdir(mode=0o700)
    for name, record in closure.items():
        copy_artifact(engine / name, None if name == "llama/llama-server" else target / name,
            limit=128 * 1024**2, role="runtime-file", expected_sha256=record["sha256"], expected_size=record["bytes"])
    for destination in (host / "backend", target / "backend"):
        destination.mkdir(exist_ok=True)
        shutil.copytree(ROOT / "backend/mcpme_textgen", destination / "mcpme_textgen",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (destination / "jevk5_app").mkdir(exist_ok=True)
        for name in POLICY_FILES:
            shutil.copyfile(ROOT / "backend/jevk5_app" / name, destination / "jevk5_app" / name)
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
    return target
