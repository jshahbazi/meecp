"""Bundle the exact private tools used by installed-host preparation.

Downloads happen at distribution build time. Preparation never discovers uv or
Python on PATH and never uses a developer's interpreter or dependency cache.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.12.13"
UV_ARCHIVE_URL = "https://github.com/astral-sh/uv/releases/download/0.12.13/uv-aarch64-apple-darwin.tar.gz"
UV_ARCHIVE_SHA256 = "7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed"
UV_EXECUTABLE_SHA256 = "964e82d2f7736a5dd3c8d4dfe299697286c655ff44c9ae3802886f27c5bf4d4a"
UV_LICENSES = {
    "LICENSE-APACHE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    "LICENSE-MIT": "860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c",
}
MANIFEST_NAME = "toolchain.json"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fetch(url: str, expected: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "mcpme-distribution-builder"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(limit + 1)
    except (OSError, ValueError):
        raise ValueError("preparation_toolchain_download_failed") from None
    if len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("preparation_toolchain_download_identity_mismatch")
    return data


def refresh_toolchain_manifest(root: Path) -> dict:
    """Refresh packaged hashes after the distribution signer changes Mach-O files."""
    manifest_path = root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    artifacts = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("unsafe_preparation_toolchain_artifact")
        if path.is_file() and path != manifest_path:
            artifacts[path.relative_to(root).as_posix()] = {
                "sha256": _digest(path), "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
            }
    manifest["artifacts"] = artifacts
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    return manifest


def bundle(destination: Path, runtime: Path, *, template_root: Path | None = None,
           archive: bytes | None = None, license_bytes: dict[str, bytes] | None = None) -> Path:
    """Copy a standalone runtime and checked upstream executable; no tool execution."""
    if destination.exists():
        raise ValueError("preparation_toolchain_destination_exists")
    runtime = runtime.resolve()
    if not (runtime / "bin/python3.12").is_file() or not (runtime / "lib/python3.12/LICENSE.txt").is_file():
        raise ValueError("private_python_runtime_required")
    archive = archive if archive is not None else _fetch(UV_ARCHIVE_URL, UV_ARCHIVE_SHA256, 64 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != UV_ARCHIVE_SHA256:
        raise ValueError("preparation_toolchain_download_identity_mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as compressed:
        selected = compressed.getmember("uv-aarch64-apple-darwin/uv")
        if not selected.isfile() or selected.size > 128 * 1024 * 1024:
            raise ValueError("unsafe_preparation_toolchain_artifact")
        stream = compressed.extractfile(selected)
        if stream is None:
            raise ValueError("unsafe_preparation_toolchain_artifact")
        executable = stream.read(selected.size + 1)
    if len(executable) != selected.size or hashlib.sha256(executable).hexdigest() != UV_EXECUTABLE_SHA256:
        raise ValueError("preparation_toolchain_executable_identity_mismatch")
    licenses = {}
    for name, expected in UV_LICENSES.items():
        value = (license_bytes or {}).get(name)
        if value is None:
            value = _fetch("https://raw.githubusercontent.com/astral-sh/uv/0.12.13/" + name, expected, 64 * 1024)
        if hashlib.sha256(value).hexdigest() != expected:
            raise ValueError("preparation_toolchain_license_identity_mismatch")
        licenses[name] = value
    destination.mkdir(parents=True)
    (destination / "bin").mkdir()
    (destination / "bin/uv").write_bytes(executable)
    (destination / "bin/uv").chmod(0o755)
    # Dereference runtime aliases so every artifact is copied into the bundle.
    # The standalone distribution is trusted build input; reject external links.
    for path in runtime.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(runtime):
            raise ValueError("private_python_runtime_escape")
    shutil.copytree(runtime, destination / "python", symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"))
    shutil.copytree(template_root or ROOT / "templates", destination / "templates",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / "licenses").mkdir()
    for name, value in licenses.items():
        (destination / "licenses" / ("uv-" + name)).write_bytes(value)
    (destination / "licenses/NOTICE.md").write_text(
        "# Preparation toolchain notices\n\n"
        "uv " + UV_VERSION + " is distributed under Apache-2.0 or MIT. "
        "The exact upstream license texts are uv-LICENSE-APACHE and uv-LICENSE-MIT.\n\n"
        "Private CPython 3.12.13 is copied from the host's standalone distribution. "
        "Its PSF and bundled component notices remain under python/. "
        "The adapter template license remains templates/LICENSE.\n")
    (destination / MANIFEST_NAME).write_text(json.dumps({
        "schema_version": 1, "platform": "aarch64-apple-darwin", "uv_version": UV_VERSION,
        "python_version": "3.12.13", "mcp_version": "1.30.0",
        "origin": {"uv_archive": UV_ARCHIVE_URL, "uv_archive_sha256": UV_ARCHIVE_SHA256,
                   "uv_executable_sha256": UV_EXECUTABLE_SHA256,
                   "python": "host bundled uv-managed CPython 3.12.13; python-build-standalone"},
        "licenses": ["licenses/uv-LICENSE-APACHE", "licenses/uv-LICENSE-MIT",
                     "licenses/NOTICE.md", "python/lib/python3.12/LICENSE.txt", "templates/LICENSE"],
        "artifacts": {},
    }) + "\n")
    refresh_toolchain_manifest(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    try:
        bundle(options.output.absolute(), options.runtime.absolute())
    except (OSError, ValueError, tarfile.TarError):
        raise SystemExit("preparation_toolchain_build_failed") from None
