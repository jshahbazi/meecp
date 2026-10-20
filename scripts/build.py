"""Build a relocatable, locally signed arm64 app with no external runtime dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # APFS clone keeps model copies cheap while preserving independence from their source.
    result = subprocess.run(["/bin/cp", "-c", str(source), str(destination)], capture_output=True)
    if result.returncode:
        shutil.copy2(source, destination)


def dependencies(path: Path) -> list[str]:
    lines = run("/usr/bin/otool", "-L", str(path), capture=True).splitlines()[1:]
    return [line.strip().split(" (compatibility")[0] for line in lines]


def system_library(value: str) -> bool:
    return value.startswith(("/System/Library/", "/usr/lib/"))


def resolve_library(value: str, origin: Path, executable: Path) -> Path:
    if value.startswith("@loader_path/"):
        return Path(value.replace("@loader_path", str(origin.parent), 1)).resolve()
    if value.startswith("@executable_path/"):
        return Path(value.replace("@executable_path", str(executable.parent), 1)).resolve()
    if value.startswith("@rpath/"):
        listing = run("/usr/bin/otool", "-l", str(origin), capture=True)
        rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", listing)
        rpaths += [str(executable.parent / "../lib")]
        for rpath in rpaths:
            rpath = rpath.replace("@loader_path", str(origin.parent)).replace("@executable_path", str(executable.parent))
            candidate = Path(rpath) / value.removeprefix("@rpath/")
            if candidate.exists():
                return candidate.resolve()
        raise RuntimeError(f"Cannot resolve bundled library {value}")
    return Path(value).resolve()


def bundle_llama(source: Path, resources: Path) -> dict:
    target = resources / "llama"
    library_dir = target / "lib"
    plugin_dir = target / "plugins"
    library_dir.mkdir(parents=True)
    plugin_dir.mkdir()
    source = source.resolve()
    destination = target / "llama-server.real"
    copy_file(source, destination)
    destinations: dict[Path, Path] = {destination: source}
    library_sources: dict[str, Path] = {}
    ggml_path = next(Path(value).resolve() for value in dependencies(source) if "/ggml/" in value and "libggml-base" in value)
    for plugin in sorted((ggml_path.parent.parent / "libexec").glob("*.so")):
        copied = plugin_dir / plugin.name
        copy_file(plugin, copied)
        destinations[copied] = plugin
    if not (plugin_dir / "libggml-metal.so").exists():
        raise RuntimeError("The source runtime has no Metal plugin")
    pending = list(destinations.items())
    while pending:
        copied, original = pending.pop()
        for dependency in dependencies(original):
            if system_library(dependency):
                continue
            # A dylib's own install ID is not a dependency.
            if original.suffix in (".dylib", ".so") and Path(dependency).name == original.name:
                continue
            resolved = resolve_library(dependency, original, source)
            name = Path(dependency).name
            if name in library_sources and library_sources[name] != resolved:
                raise RuntimeError(f"Conflicting library name: {name}")
            new = library_dir / name
            if name not in library_sources:
                library_sources[name] = resolved
                copy_file(resolved, new)
                destinations[new] = resolved
                pending.append((new, resolved))
            relative = os.path.relpath(new, copied.parent)
            copied.chmod(0o755)
            run("/usr/bin/install_name_tool", "-change", dependency, "@loader_path/" + relative, str(copied))
    backend_directory = str(ggml_path.parent.parent / "libexec").encode() + b"\0"
    replacement_directory = b"/dev/null/jevk5-backends\0"
    relocated_directories = 0
    for copied, original in destinations.items():
        # This bottle checks its compiled Homebrew plugin directory before cwd.
        # Disable that directory while retaining instruction bytes and file offsets.
        # The service sets cwd to its bundled plugins directory. Fail on unknown layouts.
        data = copied.read_bytes()
        occurrences = data.count(backend_directory)
        if occurrences:
            if occurrences != 1 or len(replacement_directory) > len(backend_directory):
                raise RuntimeError("Unknown ggml backend directory layout")
            relocated_directories += occurrences
            padded = replacement_directory.ljust(len(backend_directory), b"\0")
            copied.write_bytes(data.replace(backend_directory, padded))
        if copied.suffix in (".dylib", ".so"):
            run("/usr/bin/install_name_tool", "-id", "@loader_path/" + copied.name, str(copied))
        # Strip stale absolute rpaths. All rewritten imports use loader-relative paths.
        listing = run("/usr/bin/otool", "-l", str(copied), capture=True)
        for rpath in re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", listing):
            if rpath.startswith("/opt/homebrew/"):
                run("/usr/bin/install_name_tool", "-delete_rpath", rpath, str(copied))
        for dependency in dependencies(copied):
            if dependency.startswith("/opt/"):
                raise RuntimeError("Runtime still references Homebrew")
        run("/usr/bin/codesign", "--force", "--sign", "-", str(copied))
    if relocated_directories != 1:
        raise RuntimeError("Expected exactly one pinned ggml backend directory constant")
    supervisor = target / "llama-server"
    run("/usr/bin/clang", "-O2", "-Wall", "-Wextra", "-o", str(supervisor), str(ROOT / "scripts/llama-supervisor.c"))
    run("/usr/bin/codesign", "--force", "--sign", "-", str(supervisor))
    destinations[supervisor] = ROOT / "scripts/llama-supervisor.c"
    return {str(path.relative_to(resources)): {"sha256": digest(path), "source_sha256": digest(original)} for path, original in destinations.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-source", type=Path)
    parser.add_argument("--llama-server", type=Path, default=Path(shutil.which("llama-server") or ""))
    parser.add_argument("--skip-swift-build", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise SystemExit("Build requires an Apple silicon Mac")
    manifest = json.loads((ROOT / "model-manifest.json").read_text())
    model = ROOT / "models" / manifest["file"]
    if not model.exists() and args.model_source:
        if args.model_source.stat().st_size != manifest["bytes"] or digest(args.model_source) != manifest["sha256"]:
            raise SystemExit("Source model does not match the pinned manifest")
        copy_file(args.model_source, model)
    if not model.exists():
        raise SystemExit("Run uv run scripts/fetch_model.py or supply --model-source")
    if model.stat().st_size != manifest["bytes"] or digest(model) != manifest["sha256"]:
        raise SystemExit("Model does not match the pinned manifest")
    if not args.skip_swift_build:
        run("/usr/bin/swift", "build", "-c", "release", "--package-path", str(ROOT))
    binary_dir = Path(run("/usr/bin/swift", "build", "-c", "release", "--show-bin-path", "--package-path", str(ROOT), capture=True).strip())
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    app = dist / "JevK5.app"
    staging = dist / "JevK5.building.app"
    if staging.exists():
        shutil.rmtree(staging)
    contents = staging / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    copy_file(binary_dir / "JevK5MenuBar", macos / "JevK5MenuBar")
    copy_file(ROOT / "Info.plist", contents / "Info.plist")
    for directory in ("backend", "vendor", "docs"):
        shutil.copytree(ROOT / directory, resources / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("model-manifest.json", "README.md", "THIRD_PARTY_NOTICES.md"):
        copy_file(ROOT / filename, resources / filename)
    copy_file(model, resources / "models" / model.name)
    python_base = Path(run("uv", "python", "find", "--managed-python", "3.12", capture=True).strip()).resolve().parent.parent
    shutil.copytree(python_base, resources / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"))
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    requirements = runtime_dir / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--output-file", str(requirements), capture=True)
    run("uv", "pip", "install", "--python", str(python_base / "bin/python3.12"), "--target", str(resources / "python/site-packages"), "--requirement", str(requirements))
    from python_bytecode import remove_source_bytecode
    remove_source_bytecode(resources / "python")
    copy_file(ROOT / "scripts" / "jevk5-mcp", macos / "jevk5-mcp")
    (macos / "jevk5-mcp").chmod(0o755)
    receipts = bundle_llama(args.llama_server, resources)
    license_dir = resources / "licenses"
    license_dir.mkdir()
    llama_prefix = args.llama_server.resolve().parent.parent
    ggml_prefix = Path(next(value for value in dependencies(args.llama_server.resolve()) if "/ggml/" in value)).resolve().parent.parent
    for name, prefix in (("llama.cpp", llama_prefix), ("ggml", ggml_prefix)):
        for filename in ("LICENSE", "COPYING", "NOTICE"):
            if (prefix / filename).exists():
                copy_file(prefix / filename, license_dir / f"{name}-{filename}")
    for license_file in sorted(Path("/opt/homebrew/opt/openssl@3").glob("LICENSE*")):
        copy_file(license_file, license_dir / ("openssl-" + license_file.name))
    for license_file in python_base.glob("LICENSE*"):
        copy_file(license_file, license_dir / ("python-" + license_file.name))
    copy_file(python_base / "lib/python3.12/LICENSE.txt", license_dir / "python-LICENSE.txt")
    copy_file(Path("/opt/homebrew/opt/libomp/LICENSE.TXT"), license_dir / "libomp-LICENSE.TXT")
    if (ROOT / "scripts" / "icon.swift").exists():
        run("/usr/bin/swift", str(ROOT / "scripts/icon.swift"), str(runtime_dir / "AppIcon.iconset"))
        run("/usr/bin/iconutil", "-c", "icns", "-o", str(resources / "AppIcon.icns"), str(runtime_dir / "AppIcon.iconset"))
    (resources / "runtime-manifest.json").write_text(json.dumps({"llama": receipts, "python": run(str(python_base / "bin/python3.12"), "--version", capture=True).strip(), "source_commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD", capture=True).strip()}, indent=2) + "\n")
    run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(staging))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(staging))
    if app.exists():
        shutil.rmtree(app)
    staging.rename(app)
    print(f"Built {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
