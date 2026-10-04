"""Closed signed-helper producer controls; synthetic signatures are explicit."""
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("signed_command_helpers_tested", ROOT / "scripts/signed_command_helpers.py")
asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asset)


def archive_bytes(records):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for member, data in records:
                archive.addfile(member, io.BytesIO(data) if member.isreg() else None)
    return output.getvalue()


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "source"
    metadata = root / "templates/command-native-v1"
    metadata.mkdir(parents=True)
    contents = {name: b"Synthetic nonexecutable test member " + name.encode() for name in asset.ARTIFACTS}
    artifacts = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o700}
        for name, data in contents.items()}
    value = copy.deepcopy(asset.manifest())
    value["artifacts"] = artifacts
    for name, field in (("LICENSE", "license_sha256"), ("provenance.json", "provenance_sha256")):
        data = b"Synthetic test metadata only"
        (metadata / name).write_bytes(data)
        value[field] = hashlib.sha256(data).hexdigest()
    def records():
        result = []
        for name, data in sorted(contents.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o700
            result.append((member, data))
        return result
    archive = tmp_path.resolve() / "synthetic.tar.gz"
    def write(data):
        archive.write_bytes(data)
        archive.chmod(0o600)
        value["archive"] = {"format": "tar-gzip", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        (metadata / "manifest.json").write_text(json.dumps(value))
    write(archive_bytes(records()))
    monkeypatch.setattr(asset, "ROOT", root)
    monkeypatch.setattr(asset, "ARTIFACTS", copy.deepcopy(artifacts))
    signatures = []
    monkeypatch.setattr(asset, "_signature", lambda path, certificate: signatures.append(path))
    return SimpleNamespace(root=root, metadata=metadata, contents=contents, value=value, archive=archive,
        write=write, records=records, signatures=signatures)


def test_reviewed_manifest_pins_original_pair_sources_and_license():
    value = asset.manifest()
    assert value["component"] == asset.COMPONENT
    for path, digest in value["sources"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
    assert value["archive"]["sha256"] == "30539c050891beec398a8ecb69b959092f6897c2b495dc66b73e9cfb3de9b2cd"
    assert value["archive"]["bytes"] == 24938


def test_complete_archive_admission_then_fresh_fixed_copy(synthetic, tmp_path):
    destination = tmp_path.resolve() / "admitted"
    asset.admit_archive(synthetic.archive)
    assert not synthetic.signatures and not destination.exists()
    value = asset.admit_archive(synthetic.archive, destination)
    assert len(synthetic.signatures) == 2
    for name, data in synthetic.contents.items():
        assert (destination / name).read_bytes() == data
        assert stat.S_IMODE((destination / name).stat().st_mode) == 0o700
    assert asset.verify_host(destination) == value
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.admit_archive(synthetic.archive, destination)


def test_bundle_preserves_unrelated_bin_members_and_requires_absent_pair(synthetic, tmp_path):
    host = tmp_path.resolve() / "host"
    (host / "bin").mkdir(parents=True)
    unrelated = host / "bin/other-owner"
    unrelated.write_bytes(b"Existing unrelated producer member")
    paths = asset.bundle(host, synthetic.archive)
    assert paths == tuple(host / name for name in synthetic.value["artifacts"])
    assert unrelated.read_bytes() == b"Existing unrelated producer member"
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.bundle(host, synthetic.archive)
    assert unrelated.read_bytes() == b"Existing unrelated producer member"


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "absolute", "traversal", "symlink", "hardlink",
    "fifo", "directory", "mode", "uid", "gid", "mtime", "uname", "gname", "bytes", "oversize", "bomb"])
def test_rebound_bad_archive_still_denied_before_output(synthetic, tmp_path, fault):
    records = synthetic.records()
    member, data = records[0]
    if fault == "missing": records.pop()
    elif fault == "extra": records.append((tarfile.TarInfo("extra"), b""))
    elif fault == "duplicate": records.append(records[0])
    elif fault == "absolute": member.name = "/outside"
    elif fault == "traversal": member.name = "../outside"
    elif fault in {"symlink", "hardlink", "fifo", "directory"}:
        member.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
            "fifo": tarfile.FIFOTYPE, "directory": tarfile.DIRTYPE}[fault]
        member.size = 0
        if fault in {"symlink", "hardlink"}: member.linkname = "outside"
    elif fault == "mode": member.mode = 0o755
    elif fault in {"uid", "gid", "mtime"}: setattr(member, fault, 1)
    elif fault in {"uname", "gname"}: setattr(member, fault, "unknown")
    elif fault == "bytes": records[0] = member, b"x" * len(data)
    elif fault == "oversize":
        member.size += 1
        records[0] = member, data + b"x"
    data = gzip.compress(b"0" * (asset.MAX_UNPACKED_BYTES + 1)) if fault == "bomb" else archive_bytes(records)
    synthetic.write(data)
    destination = tmp_path.resolve() / "must-not-exist"
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.admit_archive(synthetic.archive, destination)
    assert not destination.exists() and not synthetic.signatures


@pytest.mark.parametrize("fault", ["changed", "truncated", "symlink", "hardlink", "fifo", "writable"])
def test_unknown_or_unsafe_archive_cannot_create_output(synthetic, tmp_path, fault):
    path = synthetic.archive
    if fault == "changed": path.write_bytes(path.read_bytes() + b"x")
    elif fault == "truncated": path.write_bytes(path.read_bytes()[:-1])
    elif fault == "symlink":
        held = path.with_suffix(".held"); path.rename(held); path.symlink_to(held)
    elif fault == "hardlink": os.link(path, tmp_path / "alias")
    elif fault == "fifo": path.unlink(); os.mkfifo(path)
    else: path.chmod(0o666)
    destination = tmp_path.resolve() / "absent"
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.admit_archive(path, destination)
    assert not destination.exists() and not synthetic.signatures


@pytest.mark.parametrize("fault", ["unknown", "boolean-schema", "pair", "component", "source", "signer", "license", "provenance"])
def test_fixed_manifest_has_no_alternate_pair_or_provenance_selector(synthetic, fault):
    value = synthetic.value
    if fault == "unknown": value["path"] = "/outside"
    elif fault == "boolean-schema": value["schema_version"] = True
    elif fault == "pair": value["artifacts"][next(iter(value["artifacts"]))] = {"sha256": "f"*64, "size": 1, "mode": 0o700}
    elif fault == "component": value["component"]["version"] = "1.0.2"
    elif fault == "source": value["sources"][next(iter(value["sources"]))] = "f"*64
    elif fault == "signer": value["signing"]["certificate_sha1"] = "F"*40
    elif fault == "license": (synthetic.metadata / "LICENSE").write_bytes(b"Changed")
    else: (synthetic.metadata / "provenance.json").write_bytes(b"Changed")
    (synthetic.metadata / "manifest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.manifest()


@pytest.mark.parametrize("fault", ["file-bytes", "file-mode", "file-link", "same-bytes-replace", "parent-replace"])
def test_final_host_verification_rejects_changed_pair_or_held_identity(synthetic, tmp_path, monkeypatch, fault):
    host = tmp_path.resolve() / "host"
    asset.admit_archive(synthetic.archive, host)
    path = host / next(iter(synthetic.contents))
    if fault == "file-bytes": path.write_bytes(b"Changed")
    elif fault == "file-mode": path.chmod(0o755)
    elif fault == "file-link":
        original = path.with_suffix(".held"); path.rename(original); path.symlink_to(original)
    else:
        calls = []
        def change(*args):
            calls.append(True)
            if len(calls) != 1: return
            if fault == "same-bytes-replace":
                replacement = path.with_suffix(".new"); replacement.write_bytes(path.read_bytes()); replacement.chmod(0o700); replacement.replace(path)
            else:
                old = host / "old"; (host / "bin").rename(old); (host / "bin").mkdir(mode=0o700)
                for p in old.iterdir():
                    (host / "bin" / p.name).write_bytes(p.read_bytes()); (host / "bin" / p.name).chmod(0o700)
        monkeypatch.setattr(asset, "_signature", change)
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.verify_host(host)


@pytest.mark.parametrize("fault", [None, "invalid", "team", "hardened", "timestamp", "entitlements", "timeout", "certificate"])
def test_signature_policy_never_signs_or_uses_ambient_identity(tmp_path, monkeypatch, fault):
    calls = []
    detail = b"flags=0x10000(runtime)\nTimestamp=synthetic\nTeamIdentifier=" + asset.TEAM_ID.encode() + b"\n"
    if fault == "team": detail = detail.replace(asset.TEAM_ID.encode(), b"OTHERTEAM00")
    elif fault == "hardened": detail = detail.replace(b"(runtime)", b"(other)")
    elif fault == "timestamp": detail = detail.replace(b"Timestamp=", b"Other=")
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if fault == "timeout": raise subprocess.TimeoutExpired(args, 30)
        return SimpleNamespace(returncode=1 if fault == "invalid" else 0,
            stderr=detail if "--verbose=4" in args else b"",
            stdout=b"<key>synthetic</key>" if fault == "entitlements" and "--entitlements" in args else b"")
    monkeypatch.setattr(asset.subprocess, "run", run)
    certificate = "F"*40 if fault == "certificate" else asset.CERTIFICATE_SHA1
    if fault is None: asset._signature(tmp_path / "helper", certificate)
    elif fault == "timeout":
        with pytest.raises(subprocess.TimeoutExpired): asset._signature(tmp_path / "helper", certificate)
    else:
        with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"): asset._signature(tmp_path / "helper", certificate)
    assert all("--sign" not in args and "--force" not in args for args, _ in calls)
    assert all(kwargs == {"capture_output": True, "timeout": 30,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}} for _, kwargs in calls)
    if calls:
        assert calls[0][0][0] == "/usr/bin/codesign" and asset.CERTIFICATE_SHA1 in calls[0][0][4]


def test_reconstruction_is_exact_and_output_is_exclusive(synthetic, tmp_path):
    host = tmp_path.resolve() / "host"
    asset.admit_archive(synthetic.archive, host)
    output = tmp_path.resolve() / "reconstructed.tar.gz"
    asset.create_archive(host, output)
    assert output.read_bytes() == synthetic.archive.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="^signed_command_helpers_invalid$"):
        asset.create_archive(host, output)
    assert output.read_bytes() == synthetic.archive.read_bytes()
