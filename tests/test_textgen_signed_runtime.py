"""Closed A4 assets are admitted before copy or any executable inspection."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tarfile

import pytest

from mcpme import preparation_textgen_signed_runtime as runtime
from mcpme.preparation_contracts import PreparationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import signed_textgen_asset as assets
import build_mcpme_app as app_builder
import build_mcpme_host as host_builder
import build_mcpme_dmg as dmg_builder


def archive(tmp_path, *, change=None):
    contents = {"bin/textgen-engine": b"synthetic launcher", "bin/textgen-adapter": b"synthetic launcher",
                "python/bin/python3.12": b"synthetic python", "llama/llama-server.real": b"synthetic engine"}
    for name in ("origin.json", "uv.lock", "requirements.txt", "dependency-project.toml", "dependency-notices.json",
                 "python-runtime-origin.json", "licenses/DEPENDENCIES.md", "licenses/python-LICENSE.txt"):
        contents[name] = b"synthetic metadata"
    records = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                      "executable": name.startswith(("bin/", "llama/", "python/"))} for name, data in contents.items()}
    items = []
    for name, data in contents.items():
        item = tarfile.TarInfo(name); item.size = len(data); item.mode = 0o700 if records[name]["executable"] else 0o600
        items.append((item, data))
    if change is not None:
        change(items)
    target = tmp_path / "asset.tar.gz"
    with tarfile.open(target, "w:gz") as output:
        for item, data in items:
            output.addfile(item, io.BytesIO(data))
    value = {"schema_version": 1, "profile": runtime.PROFILE, "archive": {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size, "format": "tar-gzip"}, "artifacts": records,
        "signing": {"certificate_sha1": "A" * 40, "team_id": "TESTTEAM01", "hardened_runtime": True,
                    "secure_timestamp": True, "entitlements": {}}, "provenance_sha256": hashlib.sha256(b"{}").hexdigest()}
    return target, value


def test_full_archive_admission_copies_exact_members(tmp_path):
    path, value = archive(tmp_path)
    target = tmp_path / "runtime"
    assets.admit_archive(path, target, value=value)
    assert runtime.verify_runtime(target, value, exact=True) == value


def mutation(kind, items):
    item, data = items[-1]
    if kind == "missing": items.pop()
    elif kind == "duplicate": items.append((copy.copy(item), data))
    elif kind == "extra": item.name = "unreviewed"
    elif kind == "traversal": item.name = "../outside"
    elif kind == "absolute": item.name = "/outside"
    elif kind == "symlink": item.type = tarfile.SYMTYPE; item.linkname = "outside"; item.size = 0
    elif kind == "hardlink": item.type = tarfile.LNKTYPE; item.linkname = "bin/textgen-engine"; item.size = 0
    elif kind == "fifo": item.type = tarfile.FIFOTYPE; item.size = 0
    elif kind == "pax": item.pax_headers = {"comment": "unreviewed"}
    elif kind == "mode": item.mode = 0o777
    elif kind == "bytes": items[-1] = (item, b"x" * len(data))
    elif kind == "size": item.size -= 1


@pytest.mark.parametrize("kind", ["missing", "duplicate", "extra", "traversal", "absolute", "symlink", "hardlink", "fifo", "pax", "mode", "bytes", "size"])
def test_bad_last_member_denied_before_destination_exists(tmp_path, kind):
    path, value = archive(tmp_path, change=lambda items: mutation(kind, items))
    target = tmp_path / "runtime"
    with pytest.raises((ValueError, PreparationError)):
        assets.admit_archive(path, target, value=value)
    assert not target.exists()


@pytest.mark.parametrize("kind", ["bytes", "mode", "extra", "missing", "symlink", "hardlink", "fifo"])
def test_runtime_mutation_denies_copy_or_signature_probe(tmp_path, kind):
    path, value = archive(tmp_path)
    target = tmp_path / "runtime"
    assets.admit_archive(path, target, value=value)
    file = target / "bin/textgen-engine"
    if kind == "bytes": file.write_bytes(b"changed")
    elif kind == "mode": file.chmod(0o600)
    elif kind == "extra": (target / "python/extra").write_bytes(b"unexpected")
    elif kind == "missing": file.unlink()
    elif kind == "symlink": file.unlink(); file.symlink_to("textgen-adapter")
    elif kind == "hardlink": file.unlink(); file.hardlink_to(target / "bin/textgen-adapter")
    elif kind == "fifo":
        import os
        file.unlink(); os.mkfifo(file)
    with pytest.raises(PreparationError): runtime.verify_runtime(target, value, exact=True)


@pytest.mark.parametrize("entry", ["host", "app", "dmg"])
def test_invalid_archive_rejected_before_build_or_credentials(tmp_path, monkeypatch, entry):
    path, value = archive(tmp_path)
    path.write_bytes(path.read_bytes() + b"changed")
    monkeypatch.setattr(assets, "manifest", lambda: value)
    def forbidden(*args, **kwargs): pytest.fail("unreviewed bytes reached build or signing")
    for module in (app_builder, dmg_builder, host_builder): monkeypatch.setattr(module, "run", forbidden)
    monkeypatch.setattr(app_builder, "developer_identity", forbidden)
    monkeypatch.setattr(dmg_builder, "developer_identity", forbidden)
    with pytest.raises((ValueError, PreparationError)):
        if entry == "host": host_builder.build(tmp_path / "host", version="1.0.0", textgen_signed_asset=path)
        elif entry == "app": app_builder.build(tmp_path / "test.app", version="1.0.0", sign_identity="A" * 40, textgen_signed_asset=path)
        else: dmg_builder.build(tmp_path / "test.dmg", version="1.0.0", sign_identity="A" * 40, textgen_signed_asset=path)


def test_manifest_pins_closed_signed_identity_and_full_provenance():
    value = runtime.manifest()
    assert value["profile"] == runtime.PROFILE
    assert value["signing"]["entitlements"] == {}
    assert value["artifacts"]["bin/textgen-engine"] == value["artifacts"]["bin/textgen-adapter"]
    assert "llama/llama-server" not in value["artifacts"]


@pytest.mark.parametrize("kind", ["unknown", "identity", "entitlement", "missing", "executable", "provenance"])
def test_manifest_rejects_broader_or_incomplete_profile(tmp_path, kind):
    _, value = archive(tmp_path)
    if kind == "unknown": value["new_authority"] = True
    elif kind == "identity": value["signing"]["certificate_sha1"] = "-"
    elif kind == "entitlement": value["signing"]["entitlements"] = {"com.apple.security.cs.disable-library-validation": True}
    elif kind == "missing": value["artifacts"].pop("python/bin/python3.12")
    elif kind == "executable": value["artifacts"]["python/bin/python3.12"]["executable"] = False
    elif kind == "provenance": value["provenance_sha256"] = "0" * 64
    (tmp_path / "signed-runtime.json").write_text(json.dumps(value))
    (tmp_path / "signed-runtime-provenance.json").write_text("{}")
    with pytest.raises(PreparationError): runtime.manifest(tmp_path)


@pytest.fixture
def actual_runtime(tmp_path):
    selected = os.environ.get("MCPME_TEXTGEN_SIGNED_ASSET")
    if selected is None:
        pytest.skip("explicit frozen signed asset required")
    source = Path(selected)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "signed-runtime"
    value = assets.admit_archive(source, target)
    assert assets.verify_signatures(target, value) >= 4
    yield target, value
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before == value["archive"]["sha256"]


def test_actual_release_signer_preserves_all_frozen_members(actual_runtime):
    target, value = actual_runtime
    assert app_builder.sign_runtime(target.parent, value["signing"]["certificate_sha1"], True,
                                    signed_runtime=target) == 0
    runtime.verify_runtime(target, value, exact=True)


def test_actual_signed_private_python_loads_signed_native_wheels(actual_runtime):
    target, value = actual_runtime
    code = ("import importlib,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
            "sys.path.insert(0,str(root/'python/site-packages')); "
            "names=('pydantic_core._pydantic_core','cryptography.hazmat.bindings._rust','uvicorn','mcp','ssl'); "
            "modules=[importlib.import_module(n) for n in names]; "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) for m in modules)")
    result = subprocess.run([str(target / "python/bin/python3.12"), "-I", "-B", "-c", code, str(target)],
                            capture_output=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    runtime.verify_runtime(target, value, exact=True)


def test_actual_ad_hoc_replacement_fails_pinned_developer_identity(actual_runtime):
    target, value = actual_runtime
    executable = target / "bin/textgen-engine"
    result = subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(executable)], capture_output=True, timeout=30)
    assert result.returncode == 0
    with pytest.raises(PreparationError): runtime.verify_runtime(target, value, exact=True)
    # Even a hypothetical caller-updated hash cannot replace the compiled signer.
    with pytest.raises(ValueError): assets.verify_signatures(target, value)


def test_actual_other_signer_cannot_preserve_this_profile(actual_runtime):
    target, value = actual_runtime
    with pytest.raises(ValueError):
        assets.verify_signatures(target, value, certificate="0" * 40)


def test_actual_extra_native_member_rejected_before_copy(actual_runtime, monkeypatch):
    target, value = actual_runtime
    (target / "python/unreviewed.so").write_bytes(b"synthetic")
    called = []
    monkeypatch.setattr(runtime, "_copy", lambda *args: called.append(args))
    with pytest.raises(PreparationError): runtime.copy_runtime(target, target.parent / "package")
    assert called == []
