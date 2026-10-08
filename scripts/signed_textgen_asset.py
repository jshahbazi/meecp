"""Release-only, exact archive admission for the compiled TextGen profile."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from meecp.preparation_builder import _opened
from meecp.preparation_textgen_signed_runtime import manifest, verify_runtime

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


def fail():
    raise ValueError("signed_textgen_asset_invalid")


def verify_signatures(root, value, *, certificate=None):
    """Do not invoke any asset executable; inspect signatures of every Mach-O."""
    signing = value["signing"]
    if certificate is not None and certificate.upper() != signing["certificate_sha1"]:
        fail()
    count = 0
    for name in value["artifacts"]:
        path = root / name
        with _opened(path) as stream:
            native = stream.read(4) in MACHO_MAGIC
        if not native:
            continue
        requirement = ('=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
                       ' and certificate leaf = H"' + signing["certificate_sha1"] + '"')
        for arguments in (("--verify", "--strict", "-R", requirement),
                          ("--display", "--verbose=4"), ("--display", "--entitlements", ":-")):
            result = subprocess.run(["/usr/bin/codesign", *arguments, str(path)], capture_output=True, timeout=30)
            if result.returncode:
                fail()
            if "--verbose=4" in arguments:
                output = result.stderr.decode("utf-8", errors="replace")
                if ("(runtime)" not in output or not re.search(r"^Timestamp=.+$", output, re.M)
                        or not re.search(r"^TeamIdentifier=" + re.escape(signing["team_id"]) + r"$", output, re.M)):
                    fail()
            if "--entitlements" in arguments and b"<key>" in result.stdout + result.stderr:
                fail()
        count += 1
    if count < 4:
        fail()
    verify_runtime(root, value, exact=True)
    return count


def _archive_pass(stream, value, destination=None):
    stream.seek(0)
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for item in archive:
            record = value["artifacts"].get(item.name)
            if (record is None or item.name in seen or not item.isreg() or item.pax_headers
                    or item.linkname or item.size != record["size"] or item.mode != (0o700 if record["executable"] else 0o600)):
                fail()
            seen.add(item.name)
            target = None
            if destination is not None:
                path = destination / item.name
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                target = path.open("xb")
            digest, size = hashlib.sha256(), 0
            try:
                source = archive.extractfile(item)
                if source is None:
                    fail()
                with source:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > record["size"]:
                            fail()
                        digest.update(chunk)
                        if target is not None:
                            target.write(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    fail()
                if target is not None:
                    target.flush(); os.fsync(target.fileno()); os.fchmod(target.fileno(), item.mode)
            finally:
                if target is not None:
                    target.close()
    if seen != set(value["artifacts"]):
        fail()


def admit_archive(archive, destination=None, *, value=None):
    """Validate the whole held archive before creating any runtime member."""
    value = manifest() if value is None else value
    if destination is not None and (destination.exists() or destination.is_symlink()
            or not destination.is_absolute() or destination.parent.resolve(strict=True) != destination.parent):
        fail()
    with _opened(archive) as stream:
        if os.fstat(stream.fileno()).st_size != value["archive"]["bytes"]:
            fail()
        if hashlib.file_digest(stream, "sha256").hexdigest() != value["archive"]["sha256"]:
            fail()
        _archive_pass(stream, value)
        if destination is not None:
            destination.mkdir(mode=0o700)
            _archive_pass(stream, value, destination)
            verify_runtime(destination, value, exact=True)
    return value


def bundle(host, archive):
    from build_preparation_toolchain import refresh_toolchain_manifest
    target = host / "preparation-toolchain/textgen-runtime"
    value = admit_archive(archive, target)
    verify_signatures(target, value)
    refresh_toolchain_manifest(host / "preparation-toolchain")
    return target


def verify_host(host, *, certificate=None):
    target = host / "preparation-toolchain/textgen-runtime"
    value = verify_runtime(target, exact=True)
    verify_signatures(target, value, certificate=certificate)
    return value
