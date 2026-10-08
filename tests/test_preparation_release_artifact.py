"""Synthetic distribution-shape controls, with codesign metadata simulated.

Real signed installed acceptance remains a separate release gate.
"""
import hashlib
import json
import os
import plistlib
from types import SimpleNamespace

import pytest

from meecp import preparation_release_artifact as module
from meecp.preparation_contracts import PreparationError
from meecp.preparation_release_policy import POLICY_PATH


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    app = tmp_path / "Synthetic.app"
    host = app / "Contents/Resources/host"
    source = host / "backend/meecp/preparation_release_artifact.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic module member")
    policy = host / POLICY_PATH; policy.parent.mkdir(parents=True); policy.write_bytes(b"{}")
    native = app / "Contents/MacOS/meecp"; native.parent.mkdir(); native.write_bytes(b"synthetic native member")
    seal_file = app / "Contents/_CodeSignature/CodeResources"; seal_file.parent.mkdir(); seal_file.write_bytes(b"synthetic seal")
    info = {"CFBundleIdentifier": "com.jshahbazi.mcpme", "CFBundleExecutable": "meecp", "CFBundleShortVersionString": "1.0.0"}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    build = {"host_version": "1.0.0", "source_commit": "a"*40, "source_tree_dirty": False,
        "signing": "developer_id", "distribution": "bundled_native_app_host", "python": "3.12", "toolchain": "private",
        "artifact_sha256": {p.relative_to(host).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, policy)}}
    def seal():
        data = json.dumps(build).encode(); (host / "build.json").write_bytes(data)
        receipt = {"signing": "developer_id", "distribution": "developer_id_native_app", "hardened_runtime": True,
            "secure_timestamp": True, "source_tree_dirty": False, "source_commit": "a"*40, "version": "1.0.0",
            "host_receipt_sha256": hashlib.sha256(data).hexdigest()}
        (app / "Contents/Resources/app-build.json").write_text(json.dumps(receipt))
    seal()
    calls = []
    def signature(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=("flags=0x10000(runtime)\nTimestamp=synthetic\nTeamIdentifier=" + module.TEAM_ID + "\n").encode())
    monkeypatch.setattr(module, "__file__", str(source))
    monkeypatch.setattr(module.subprocess, "run", signature)
    return app, host, policy, build, seal, calls


def test_exact_member_signature_and_policy_binding(artifact):
    app, host, policy, build, seal, calls = artifact
    module.verify_release_artifact(host, POLICY_PATH, b"{}")
    assert len(calls) == 2 and all(call[0] == "/usr/bin/codesign" and call[-1] == str(app) for call in calls)
    assert "--deep" in calls[0] and "--strict" in calls[0]
    assert module.CERTIFICATE_SHA1 in calls[0][-2]


@pytest.mark.parametrize("damage", ["policy", "build", "seal", "member", "samebytes", "membership"])
def test_held_proof_never_recaptures_replacement_artifact(artifact, damage):
    app, host, policy, build, seal, calls = artifact
    proof = module.verify_release_artifact(host, POLICY_PATH, b"{}")
    if damage == "policy": policy.write_bytes(b"[]")
    elif damage == "build": (host/"build.json").write_bytes(b"{}")
    elif damage == "seal": (app/"Contents/_CodeSignature/CodeResources").write_bytes(b"changed")
    elif damage == "member": (host/"backend/meecp/preparation_release_artifact.py").write_bytes(b"changed")
    elif damage == "samebytes": policy.unlink(); policy.write_bytes(b"{}")
    else: (host/"backend/meecp/foreign").write_bytes(b"changed")
    with pytest.raises(PreparationError): proof.check_current(full=True)
    if damage not in {"member", "membership"}:
        with pytest.raises(PreparationError): proof.check_current()


@pytest.mark.parametrize("damage", ["unlisted", "missing", "bytes", "policy", "manifest", "dirty", "adhoc", "fifo", "link", "hardlink", "writable"])
def test_inexact_artifact_denies_before_signature(artifact, damage):
    app, host, policy, build, seal, calls = artifact
    if damage == "unlisted": (host / "foreign").write_bytes(b"synthetic")
    elif damage == "missing": policy.unlink()
    elif damage == "bytes": policy.write_bytes(b"[]")
    elif damage == "policy": build["artifact_sha256"][POLICY_PATH] = "0"*64; seal()
    elif damage == "manifest": del build["artifact_sha256"][POLICY_PATH]; seal()
    elif damage == "dirty": build["source_tree_dirty"] = True; seal()
    elif damage == "adhoc": build["signing"] = "adhoc"; seal()
    elif damage == "fifo": os.mkfifo(host / "foreign")
    elif damage == "link": (host / "foreign").symlink_to(policy)
    elif damage == "hardlink": os.link(policy, host / "foreign")
    else: policy.chmod(0o666)
    with pytest.raises(PreparationError): module.verify_release_artifact(host, POLICY_PATH, b"{}")
    assert not calls


@pytest.mark.parametrize("damage", ["failure", "team", "runtime", "timestamp", "mutation", "samebytes_replacement"])
def test_signature_failure_or_midcheck_mutation_denies(artifact, monkeypatch, damage):
    app, host, policy, build, seal, calls = artifact
    def signature(argv, **kwargs):
        detail = "flags=0x10000(runtime)\nTimestamp=synthetic\nTeamIdentifier=" + module.TEAM_ID + "\n"
        if damage == "team": detail = detail.replace(module.TEAM_ID, "AAAAAAAAAA")
        elif damage == "runtime": detail = detail.replace("(runtime)", "")
        elif damage == "timestamp": detail = detail.replace("Timestamp=synthetic\n", "")
        elif damage == "mutation": policy.write_bytes(b"[]")
        elif damage == "samebytes_replacement": policy.unlink(); policy.write_bytes(b"{}")
        return SimpleNamespace(returncode=1 if damage == "failure" else 0, stdout=b"", stderr=detail.encode())
    monkeypatch.setattr(module.subprocess, "run", signature)
    with pytest.raises(PreparationError): module.verify_release_artifact(host, POLICY_PATH, b"{}")


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'{}' + b' '* (8*1024**2)])
def test_inventory_parser_is_bounded_and_unambiguous(data):
    with pytest.raises(PreparationError): module.load_artifact_json(data)


def test_realistic_inventory_size_exceeds_contract_limit_but_is_bounded():
    value = {"artifact_sha256": {"synthetic/" + str(i) + "x"*30: "a"*64 for i in range(6000)}}
    data = json.dumps(value).encode()
    assert 512*1024 < len(data) < 8*1024**2
    assert len(module.load_artifact_json(data)["artifact_sha256"]) == 6000
