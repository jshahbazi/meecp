"""Copy the reviewed signed command pair without compiling or signing it.

Only producer code supplies an archive path. The fixed Git manifest admits one
immutable pair; it cannot select an endpoint, source program or execution gate.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "command-native-v1"
VERSION = "1.0.0"
CERTIFICATE_SHA1 = "EFE6CD2666B9106E68724C38ADD1C405267A2F94"
TEAM_ID = "S5BT2PRN5B"
COMPONENT = {"id": "host-command-json-v1", "version": "1.0.1",
    "digest": "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607"}
SOURCES = {
    "scripts/mcpme-command-reap.c": "b99f87541598427df51a39486d384a5855813ea3decac55a1d091cafb5e17933",
    "scripts/mcpme-command-resource-reap.c": "09b455941c0a2890e2bdbd2ca91b6d8e47538a4e5f1a4115e7885412df89c400",
    "scripts/mcpme-command-resource-v1.h": "a30d8a2acbdc15d704ed3d2480cad749d78f2647a98171c55b930ae0a9072c55",
}
ARTIFACTS = {
    "bin/mcpme-command-reap": {"sha256": "9dab276553fec8f2039fa6d7a3ce5cc8b7aeebc01f9df730027320e72f0e6513",
        "size": 70752, "mode": 0o700},
    "bin/mcpme-command-resource-reap": {"sha256": "c2ecbc7942981430d6f1bad26809c2e42c409d75aafdc19fc6426faf52110bb5",
        "size": 71504, "mode": 0o700},
}
MAX_ARCHIVE_BYTES = 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024


def fail():
    raise ValueError("signed_command_helpers_invalid")


def _identity(path, full=True):
    value = path.lstat()
    fields = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
    return fields + ((value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if full else ())


def _directory(path):
    if (type(path) is not type(Path()) or not path.is_absolute()
            or path.resolve(strict=True) != path):
        fail()
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o6022:
        fail()


def _read(path, limit):
    if type(path) is not type(Path()) or not path.is_absolute() or path.resolve(strict=True) != path:
        fail()
    before = _identity(path)
    value = path.lstat()
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or value.st_uid != os.getuid() or value.st_mode & 0o6022 or value.st_size > limit):
        fail()
    parents = {parent: _identity(parent, False) for parent in path.parents}
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        held = os.fstat(stream.fileno())
        if (held.st_dev, held.st_ino, held.st_mode, held.st_uid, held.st_nlink,
                held.st_size, held.st_mtime_ns, held.st_ctime_ns) != before:
            fail()
        data = stream.read(limit + 1)
        if len(data) > limit or _identity(path) != before:
            fail()
    if path.resolve(strict=True) != path or any(_identity(parent, False) != identity for parent, identity in parents.items()):
        fail()
    return data


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail()
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: fail())


def manifest():
    """Read the fixed source-owned manifest; callers cannot override its path."""
    try:
        directory = ROOT / "templates" / PROFILE
        data = _read(directory / "manifest.json", 16 * 1024)
        value = _json(data)
        fixed = {"schema_version": 1, "profile": PROFILE, "version": VERSION,
            "component": COMPONENT, "sources": SOURCES, "artifacts": ARTIFACTS,
            "signing": {"certificate_sha1": CERTIFICATE_SHA1, "team_id": TEAM_ID,
                "hardened_runtime": True, "secure_timestamp": True, "entitlements": "empty"}}
        if (type(value) is not dict or set(value) != set(fixed) | {"archive", "license_sha256", "provenance_sha256"}
                or any(_canonical(value[key]) != _canonical(item) for key, item in fixed.items())):
            fail()
        archive = value["archive"]
        if (type(archive) is not dict or set(archive) != {"format", "bytes", "sha256"}
                or archive["format"] != "tar-gzip" or type(archive["bytes"]) is not int
                or not 0 < archive["bytes"] <= MAX_ARCHIVE_BYTES
                or type(archive["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]) is None):
            fail()
        for name, key in (("LICENSE", "license_sha256"), ("provenance.json", "provenance_sha256")):
            if (type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
                    or hashlib.sha256(_read(directory / name, 16 * 1024)).hexdigest() != value[key]):
                fail()
        if _read(directory / "manifest.json", 16 * 1024) != data:
            fail()
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        fail()


def _payloads(data, value):
    if len(data) != value["archive"]["bytes"] or hashlib.sha256(data).hexdigest() != value["archive"]["sha256"]:
        fail()
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
        unpacked = compressed.read(MAX_UNPACKED_BYTES + 1)
        if len(unpacked) > MAX_UNPACKED_BYTES or compressed.read(1):
            fail()
    result = {}
    with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
        for item in archive:
            record = value["artifacts"].get(item.name)
            if (record is None or item.name in result or item.type != tarfile.REGTYPE
                    or item.pax_headers or item.linkname or item.size != record["size"]
                    or item.mode != record["mode"] or item.uid != 0 or item.gid != 0
                    or item.mtime != 0 or item.uname or item.gname or item.sparse is not None):
                fail()
            source = archive.extractfile(item)
            if source is None:
                fail()
            with source:
                contents = source.read(record["size"] + 1)
            if len(contents) != record["size"] or hashlib.sha256(contents).hexdigest() != record["sha256"]:
                fail()
            result[item.name] = contents
    if set(result) != set(value["artifacts"]):
        fail()
    return result


def _signature(path, certificate):
    if certificate is not None and (type(certificate) is not str or certificate.upper() != CERTIFICATE_SHA1):
        fail()
    requirement = ('=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        ' and certificate leaf = H"' + CERTIFICATE_SHA1 + '"')
    arguments = (("--verify", "--strict", "-R", requirement),
        ("--display", "--verbose=4"), ("--display", "--entitlements", ":-"))
    for flags in arguments:
        result = subprocess.run(["/usr/bin/codesign", *flags, str(path)], capture_output=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        if result.returncode:
            fail()
        if "--verbose=4" in flags:
            detail = result.stderr.decode("utf-8", errors="replace")
            if ("(runtime)" not in detail or not re.search(r"^Timestamp=.+$", detail, re.M)
                    or not re.search(r"^TeamIdentifier=" + TEAM_ID + r"$", detail, re.M)):
                fail()
        if "--entitlements" in flags and b"<key>" in result.stdout + result.stderr:
            fail()


def _verify(host, value, certificate=None):
    _directory(host)
    _directory(host / "bin")
    paths = tuple(host / name for name in value["artifacts"])
    before = {path: _identity(path) for path in paths}
    parents = {parent: _identity(parent, False) for path in paths for parent in path.parents}
    for path in paths:
        record = value["artifacts"][path.relative_to(host).as_posix()]
        data = _read(path, record["size"])
        if (len(data) != record["size"] or stat.S_IMODE(path.lstat().st_mode) != record["mode"]
                or hashlib.sha256(data).hexdigest() != record["sha256"]):
            fail()
    for path in paths:
        _signature(path, certificate)
    if (any(_identity(path) != identity for path, identity in before.items())
            or any(_identity(parent, False) != identity for parent, identity in parents.items())):
        fail()
    return value


def verify_host(host, *, certificate=None):
    """Verify both fixed members before/after distribution signing."""
    try:
        return _verify(host, manifest(), certificate)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        fail()


def _copy(host, payloads, value):
    _directory(host)
    _directory(host / "bin")
    targets = tuple(host / name for name in value["artifacts"])
    if any(path.exists() or path.is_symlink() for path in targets):
        fail()
    parents = {parent: _identity(parent, False) for path in targets for parent in path.parents}
    for path in targets:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(descriptor, "wb") as target:
            target.write(payloads[path.relative_to(host).as_posix()])
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o700)
    if any(_identity(parent, False) != identity for parent, identity in parents.items()):
        fail()
    _verify(host, value)
    return targets


def admit_archive(archive, destination=None):
    """Validate the whole bounded archive before creating any output member."""
    try:
        value = manifest()
        payloads = _payloads(_read(archive, MAX_ARCHIVE_BYTES), value)
        if destination is not None:
            if (type(destination) is not type(Path()) or not destination.is_absolute()
                    or destination.exists() or destination.is_symlink()):
                fail()
            _directory(destination.parent)
            destination.mkdir(mode=0o700)
            (destination / "bin").mkdir(mode=0o700)
            _copy(destination, payloads, value)
        return value
    except (OSError, ValueError, TypeError, KeyError, EOFError, tarfile.TarError, zlib.error, subprocess.SubprocessError):
        fail()


def bundle(host, archive):
    """Copy to two absent fixed host/bin members in the producer's fresh stage."""
    try:
        value = manifest()
        payloads = _payloads(_read(archive, MAX_ARCHIVE_BYTES), value)
        return _copy(host, payloads, value)
    except (OSError, ValueError, TypeError, KeyError, EOFError, tarfile.TarError, zlib.error, subprocess.SubprocessError):
        fail()


def create_archive(host, output):
    """Reconstruct the exact reviewed archive from the approved signed pair."""
    try:
        if type(output) is not type(Path()):
            fail()
        value = verify_host(host)
        payloads = {name: _read(host / name, record["size"]) for name, record in value["artifacts"].items()}
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, contents in sorted(payloads.items()):
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(contents), 0o700
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = ""
                    archive.addfile(member, io.BytesIO(contents))
        data = buffer.getvalue()
        _payloads(data, value)
        _directory(output.parent)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        verify_host(host)
        return value
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        fail()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-host", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    try:
        create_archive(args.source_host, args.archive)
    except (Exception, KeyboardInterrupt):
        print("signed_command_helpers_invalid")
        return 1
    print("signed_command_helpers_created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
