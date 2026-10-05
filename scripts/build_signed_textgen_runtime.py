"""Produce one immutable Developer ID TextGen runtime from explicit pinned inputs.

This release-only command never signs during ordinary preparation. Outputs are
fresh private paths; credentials and raw command output are never persisted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import gzip
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from build_mcpme_app import MACHO_MAGIC, sign_runtime, verify_signature
from mcpme.model_intake import copy_artifact
from mcpme.preparation_acquisition import WheelAcquirer
from mcpme.preparation_builder import _runtime, load_private_toolchain
from mcpme.preparation_textgen_signed_runtime import PROFILE
from signed_textgen_asset import admit_archive, verify_signatures

logging.disable(logging.CRITICAL)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def unique_identity():
    result = subprocess.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("signed_asset_identity_unavailable")
    identities = re.findall(rb'\b([A-F0-9]{40}) "Developer ID Application: [^"\n]+"', result.stdout)
    if len(set(identities)) != 1:
        raise ValueError("signed_asset_unique_identity_required")
    return identities[0].decode()


def signature_metadata(path, certificate):
    verify_signature(path, certificate)
    result = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(path)],
                            capture_output=True, timeout=30)
    output = result.stderr.decode("utf-8", errors="replace")
    team = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", output, re.M)
    if result.returncode or team is None or "(runtime)" not in output or not re.search(r"^Timestamp=.+$", output, re.M):
        raise ValueError("signed_asset_signature_policy_mismatch")
    entitlement = subprocess.run(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(path)],
                                 capture_output=True, timeout=30)
    if entitlement.returncode:
        raise ValueError("signed_asset_entitlement_check_failed")
    if b"<key>" in entitlement.stdout or b"<key>" in entitlement.stderr:
        raise ValueError("signed_asset_entitlement_exception_forbidden")
    return team.group(1)


def refresh_installed_records(root):
    """Preserve wheel origins while reflecting explicit native-file transforms."""
    site = root / "python/site-packages"
    for record in site.glob("*.dist-info/RECORD"):
        rows = list(csv.reader(io.StringIO(record.read_text())))
        changed = []
        for name, hashed, size in rows:
            unresolved = site / name
            path = unresolved.resolve()
            if not path.is_relative_to(root) or not path.is_file() or unresolved.is_symlink():
                raise ValueError("signed_asset_wheel_record_invalid")
            if path == record:
                changed.append((name, "", ""))
            else:
                data = path.read_bytes()
                value = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                changed.append((name, "sha256=" + value, str(len(data))))
        target = io.StringIO(); csv.writer(target, lineterminator="\n").writerows(changed)
        record.write_text(target.getvalue())


def build(engine, toolchain_host, destination, archive, metadata):
    os.umask(0o077)
    for path in (engine, toolchain_host):
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError("signed_asset_canonical_input_required")
    for path in (destination, archive, metadata):
        if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent or path.exists() or path.is_symlink():
            raise ValueError("signed_asset_fresh_output_required")
    destination.mkdir(mode=0o700); metadata.mkdir(mode=0o700)
    runtime = destination / "runtime"; runtime.mkdir(mode=0o700)
    work = destination / "work"; work.mkdir(mode=0o700)
    original_assets = ROOT / "templates/text-generate-v1"
    closure = json.loads((original_assets / "engine-closure.json").read_bytes())
    input_hashes = {name: digest(engine / name) for name in closure}
    producer_hashes = {name: digest(ROOT / name) for name in (
        "scripts/build_signed_textgen_runtime.py", "scripts/signed_textgen_asset.py",
        "scripts/build_mcpme_app.py", "backend/mcpme/preparation_builder.py",
        "backend/mcpme/preparation_textgen_signed_runtime.py")}
    for name, record in closure.items():
        copy_artifact(engine / name, None if name == "llama/llama-server" else runtime / name,
                      limit=128 * 1024**2, role="runtime-file", expected_sha256=record["sha256"], expected_size=record["bytes"])
    # _runtime copies its own exact Python license; avoid a second destination.
    (runtime / "licenses/python-LICENSE.txt").unlink()
    project = work / "project"; project.mkdir(mode=0o700)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(original_assets / name, project / name)
    toolchain = load_private_toolchain(toolchain_host)
    commands, wheels = [], []
    def run(spec):
        result = subprocess.run(spec.argv, cwd=spec.cwd, env=spec.env, capture_output=True,
                                timeout=spec.timeout_seconds)
        commands.append({"operation": spec.operation, "code": result.returncode})
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError("signed_asset_locked_runtime_build_failed")
        return result.stdout.decode("utf-8")
    def acquire(requests, target, check):
        asyncio.run(WheelAcquirer().acquire(requests, target, allowed_hosts=frozenset({"files.pythonhosted.org"}),
                                           disk_bytes=2 * 1024**3, wall_seconds=900, check_canceled=check))
        wheels.extend({"filename": item.filename, "sha256": item.sha256, "bytes": item.size} for item in requests)
    _runtime(runtime, project, toolchain, run, lambda: None, work, acquire)
    (runtime / "bin").mkdir()
    source = original_assets / "launcher.c"
    result = subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source),
                             "-o", str(runtime / "bin/textgen-engine")], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("signed_asset_launcher_compile_failed")
    shutil.copyfile(runtime / "bin/textgen-engine", runtime / "bin/textgen-adapter")
    (runtime / "bin/textgen-adapter").chmod(0o700)
    before = {str(p.relative_to(runtime)): digest(p) for p in runtime.rglob("*") if p.is_file()}
    certificate = unique_identity()
    sign_runtime(runtime, certificate, True)
    # The same signed launcher is selected by basename; preserve its exact bytes.
    shutil.copyfile(runtime / "bin/textgen-engine", runtime / "bin/textgen-adapter")
    teams, signed = set(), []
    for path in sorted(runtime.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                native = stream.read(4) in MACHO_MAGIC
            if native:
                teams.add(signature_metadata(path, certificate)); signed.append(path.relative_to(runtime).as_posix())
    if len(teams) != 1:
        raise ValueError("signed_asset_team_identity_mismatch")
    refresh_installed_records(runtime)
    origin_path = runtime / "python-runtime-origin.json"
    origin = json.loads(origin_path.read_bytes())
    origin["upstream_managed_interpreter_sha256"] = origin["managed_interpreter_sha256"]
    origin["managed_interpreter_sha256"] = digest(runtime / "python/bin/python3.12")
    origin["installation"] = "Frozen hash-checked wheels and private runtime; explicit Developer ID transformation"
    origin_path.write_text(json.dumps(origin, sort_keys=True, indent=2) + "\n")
    signed_closure = {name: {"bytes": (runtime / name).stat().st_size, "sha256": digest(runtime / name)}
                      for name in closure if name != "llama/llama-server"}
    # The reviewed legacy wrapper is an input only, never a package member.
    signed_closure["llama/llama-server"] = closure["llama/llama-server"]
    private_json(runtime / "origin.json", {"schema_version": 1, "engine_closure": signed_closure,
        "launcher_source_sha256": digest(source), "engine_policy": "Frozen Developer ID CPU closure; direct host ownership."})
    signing = {"certificate_sha1": certificate, "team_id": next(iter(teams)),
               "hardened_runtime": True, "secure_timestamp": True, "entitlements": {}}
    provenance = {"schema_version": 1, "profile": PROFILE, "source_commit": subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "toolchain_identity": toolchain.identity, "original_engine_closure": closure,
        "producer_source_sha256": producer_hashes,
        "launcher_source_sha256": digest(source), "dependency_project_sha256": digest(project / "pyproject.toml"),
        "dependency_lock_sha256": digest(project / "uv.lock"), "upstream_wheels": wheels, "signing": signing,
        "native_paths": signed, "transformed_files": {name: {"input_sha256": old, "final_sha256": digest(runtime / name)}
            for name, old in before.items() if digest(runtime / name) != old},
        "policy": "Sign once; preserve this asset unchanged in future app builds; no entitlement exceptions."}
    private_json(metadata / "signed-runtime-provenance.json", provenance)
    records = {}
    for path in sorted(runtime.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("signed_asset_unsafe_output")
        if path.is_file():
            records[path.relative_to(runtime).as_posix()] = {"sha256": digest(path), "size": path.stat().st_size,
                                                            "executable": bool(path.stat().st_mode & 0o111)}
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tar:
                for name, record in records.items():
                    item = tarfile.TarInfo(name); item.size = record["size"]; item.mode = 0o700 if record["executable"] else 0o600
                    with (runtime / name).open("rb") as stream:
                        tar.addfile(item, stream)
        output.flush(); os.fsync(output.fileno())
    manifest = {"schema_version": 1, "profile": PROFILE, "signing": signing, "artifacts": records,
                "archive": {"format": "tar-gzip", "sha256": digest(archive), "bytes": archive.stat().st_size},
                "provenance_sha256": digest(metadata / "signed-runtime-provenance.json")}
    private_json(metadata / "signed-runtime.json", manifest)
    private_json(metadata / "engine-closure.json", signed_closure)
    from mcpme.preparation_textgen_signed_runtime import manifest as read_manifest
    value = read_manifest(metadata)
    admit_archive(archive, value=value)
    verify_signatures(runtime, value)
    if (input_hashes != {name: digest(engine / name) for name in closure}
            or producer_hashes != {name: digest(ROOT / name) for name in producer_hashes}
            or load_private_toolchain(toolchain_host).identity != toolchain.identity):
        raise ValueError("signed_asset_input_changed")
    private_json(metadata / "production-receipt.json", {"schema_version": 1, "profile": PROFILE,
        "archive_sha256": digest(archive), "manifest_sha256": digest(metadata / "signed-runtime.json"),
        "provenance_sha256": manifest["provenance_sha256"], "native_files": len(signed), "artifact_files": len(records),
        "commands": commands, "models": 0, "provider_calls": 0, "signed_once": True,
        "original_engine_preserved": True, "toolchain_preserved": True,
        "producer_source_sha256": producer_hashes, "strict_archive_validated": True})
    return metadata / "production-receipt.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("engine", "toolchain-host", "destination", "archive", "metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.engine, args.toolchain_host, args.destination, args.archive, args.metadata)
    except (Exception, KeyboardInterrupt):
        print("signed_textgen_asset_failed"); raise SystemExit(1) from None
    print("signed_textgen_asset_created")
