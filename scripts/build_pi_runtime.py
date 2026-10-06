"""Bundle the locked Pi SDK and verified official Apple silicon Node runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agents/pi"
NODE_VERSION = "24.15.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-darwin-arm64.tar.gz"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "372331b969779ab5d15b949884fc6eaf88d5afe87bde8ba881d6400b9100ffc4"
MAX_FILES = 30000
MAX_BYTES = 300 * 1024**2


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _node(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / NODE_ARCHIVE
    if not archive.is_file() or sha256(archive) != NODE_SHA256:
        temporary = archive.with_suffix(".download")
        temporary.unlink(missing_ok=True)
        try:
            with urllib.request.urlopen(NODE_URL, timeout=60) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if sha256(temporary) != NODE_SHA256:
                raise ValueError("pi_node_archive_invalid")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    runtime = cache / f"node-v{NODE_VERSION}-darwin-arm64"
    if runtime.exists():
        shutil.rmtree(runtime)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > MAX_FILES or sum(item.size for item in members) > MAX_BYTES:
            raise ValueError("pi_node_archive_limit")
        prefix = f"node-v{NODE_VERSION}-darwin-arm64/"
        if any(item.name != prefix.rstrip("/") and not item.name.startswith(prefix) for item in members):
            raise ValueError("pi_node_archive_invalid")
        source.extractall(cache, members=members, filter="data")
    return runtime


def _command(arguments: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> str:
    try:
        result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=timeout, check=False)
        if result.returncode != 0:
            raise ValueError("pi_runtime_command_failed")
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("pi_runtime_command_failed") from None


def _copy_modules(source: Path, destination: Path, *, preserve_build_files=False) -> None:
    """Retain package runtime assets/licenses; omit CLI links and source maps."""
    total = 0
    count = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not preserve_build_files and any(part in {".bin", "docs", "examples", "test", "tests", "__tests__"} for part in relative.parts):
            continue
        if not preserve_build_files and path.name.endswith((".map", ".d.ts", ".d.mts", ".d.cts")):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(source.resolve()) or not resolved.is_file():
            raise ValueError("pi_dependency_file_invalid")
        details = resolved.stat()
        count += 1
        total += details.st_size
        if count > MAX_FILES or total > MAX_BYTES:
            raise ValueError("pi_dependency_limit")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)
        target.chmod(stat.S_IMODE(details.st_mode))


def refresh_inventory(destination: Path) -> None:
    """Bind actual artifact bytes after app signing, without changing paths."""
    records = []
    total = 0
    for path in sorted(destination.rglob("*")):
        relative = path.relative_to(destination).as_posix()
        if relative == "inventory.json":
            continue
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("pi_runtime_file_invalid")
        total += details.st_size
        if len(records) >= MAX_FILES or total > MAX_BYTES:
            raise ValueError("pi_runtime_limit")
        records.append({"path": relative, "sha256": sha256(path), "size": details.st_size,
                        "mode": stat.S_IMODE(details.st_mode)})
    if not (destination / "bin/node").is_file() or not (destination / "worker.mjs").is_file():
        raise ValueError("pi_runtime_missing")
    value = {"schema_version": 1, "node": "bin/node", "worker": "worker.mjs", "files": records}
    (destination / "inventory.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def bundle(destination: Path, *, cache: Path | None = None) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("pi_runtime_requires_macos_arm64")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("pi_runtime_destination_exists")
    cache = cache or ROOT / "runtime/pi-build"
    node_runtime = _node(cache)
    node = node_runtime / "bin/node"
    npm = node_runtime / "lib/node_modules/npm/bin/npm-cli.js"
    env = {"HOME": str(cache), "PATH": f"{node_runtime / 'bin'}:/usr/bin:/bin",
           "npm_config_cache": str(cache / "npm-cache"), "npm_config_registry": "https://registry.npmjs.org/",
           "npm_config_update_notifier": "false", "npm_config_fund": "false"}
    # Respect an explicitly configured build-machine trust store while keeping
    # TLS verification enabled. Do not inherit npm credentials or Node flags.
    if os.environ.get("NODE_EXTRA_CA_CERTS"):
        env["NODE_EXTRA_CA_CERTS"] = os.environ["NODE_EXTRA_CA_CERTS"]
    if _command([str(node), "--version"], cwd=cache, env=env, timeout=10) != "v" + NODE_VERSION:
        raise ValueError("pi_node_version_invalid")
    lock = json.loads((SOURCE / "package-lock.json").read_bytes())
    pi = lock["packages"]["node_modules/@earendil-works/pi-coding-agent"]
    provenance = json.loads((SOURCE / "provenance.json").read_bytes())
    if pi["version"] != provenance["pi"]["version"] or pi["integrity"] != provenance["pi"]["integrity"]:
        raise ValueError("pi_lock_identity_invalid")
    for relative, entry in lock["packages"].items():
        if relative and (not entry.get("resolved", "").startswith("https://registry.npmjs.org/")
                         or not entry.get("integrity", "").startswith("sha512-")):
            raise ValueError("pi_lock_source_invalid")
    with tempfile.TemporaryDirectory(prefix="dependencies-", dir=cache) as temporary:
        staging = Path(temporary)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(SOURCE / name, staging / name)
        _command([str(node), str(npm), "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
                 cwd=staging, env=env, timeout=300)
        destination.mkdir(parents=True)
        try:
            (destination / "bin").mkdir()
            shutil.copyfile(node, destination / "bin/node")
            (destination / "bin/node").chmod(0o755)
            for name in ("worker.mjs", "package.json", "package-lock.json", "provenance.json"):
                shutil.copyfile(SOURCE / name, destination / name)
            shutil.copytree(SOURCE / "licenses", destination / "licenses")
            shutil.copyfile(node_runtime / "LICENSE", destination / "licenses/NODE-LICENSE.txt")
            # npm ships in the same hash-verified official Node archive. It is
            # a private general project installer, never a global installation.
            _copy_modules(node_runtime / "lib/node_modules/npm", destination / "npm", preserve_build_files=True)
            _copy_modules(staging / "node_modules", destination / "node_modules")
            _command([str(destination / "bin/node"), "--check", str(destination / "worker.mjs")],
                     cwd=destination, env=env, timeout=10)
            refresh_inventory(destination)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/pi-runtime")
    options = parser.parse_args()
    try:
        print(bundle(options.output))
    except KeyboardInterrupt:
        raise SystemExit("pi_runtime_build_interrupted") from None
    except (OSError, ValueError, KeyError, tarfile.TarError):
        raise SystemExit("pi_runtime_build_failed") from None
