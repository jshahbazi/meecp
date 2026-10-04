"""Real filesystem identity controls; enclosing signatures are synthetic."""
from dataclasses import replace
import os
import stat
import subprocess
import time

import pytest

from mcpme import preparation_release_artifact as artifact_module
from mcpme import preparation_release_command_admission as command
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_release_policy import POLICY_PATH
from test_preparation_release_artifact import artifact
from test_preparation_release_command_admission import packaged, policy

NATIVE_RUN = subprocess.run


def ctime_only(path):
    before = artifact_module._identity(path)
    for _ in range(20):
        path.chmod(stat.S_IMODE(before[2]))
        after = artifact_module._identity(path)
        if after[-1] != before[-1]:
            assert after[:-1] == before[:-1]
            return
        time.sleep(0.001)
    pytest.fail("filesystem did not expose ctime mutation")


def shared_proof(packaged, monkeypatch):
    monkeypatch.setattr(artifact_module, "__file__", str(packaged.host / "backend/mcpme/preparation_release_artifact.py"))
    monkeypatch.setattr(artifact_module, "_signature", lambda _: None)
    return artifact_module.verify_release_artifact(packaged.host, POLICY_PATH, b"{}")


def test_verified_root_ctime_only_at_signature_and_held_checks(artifact, monkeypatch):
    app, host, *_ = artifact
    monkeypatch.setattr(artifact_module, "_signature", lambda _: ctime_only(app))
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    baseline = proof.identities
    ctime_only(app)
    proof.check_current()
    proof.check_current(full=True)
    assert proof.identities is baseline
    assert proof._validated_app_root == app


def test_command_root_ctime_only_at_local_signature_and_held_checks(packaged, monkeypatch):
    proof = shared_proof(packaged, monkeypatch)
    baseline = proof.identities
    monkeypatch.setattr(command, "_signature", lambda *args, **kwargs: ctime_only(packaged.app))
    installed, measured = command.command_release_admission(policy(), packaged.host, artifact=proof)
    assert installed.helper == packaged.helpers[0] and measured.helper == packaged.helpers[1]
    proof.check_current(full=True)
    assert proof.identities is baseline


def test_manual_proof_cannot_select_exemption_and_copy_is_strict(artifact):
    app, host, *_ = artifact
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    manual = replace(proof)
    assert manual._validated_app_root is None
    with pytest.raises(TypeError):
        replace(proof, validated_app_root=app)
    with pytest.raises(ValueError):
        replace(proof, _validated_app_root=app)
    ctime_only(app)
    with pytest.raises(PreparationError):
        manual.check_current(full=True)
    proof.check_current(full=True)


def damage_root(app, damage, monkeypatch):
    if damage == "inode":
        old = app.with_name("old-private-app")
        app.rename(old)
        app.mkdir(mode=stat.S_IMODE(old.stat().st_mode))
    elif damage == "mode":
        app.chmod(stat.S_IMODE(app.stat().st_mode) ^ stat.S_IRGRP)
    elif damage == "mtime":
        value = app.stat()
        os.utime(app, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000))
    elif damage == "structure":
        (app / "unexpected-directory").mkdir()
    else:
        # No privileged chown: substitute only the observed UID field.
        original = artifact_module._identity
        def observed(path, full=True):
            value = original(path, full)
            return (*value[:3], value[3] + 1, *value[4:]) if path == app else value
        monkeypatch.setattr(artifact_module, "_identity", observed)


@pytest.mark.parametrize("damage", ["inode", "mode", "uid", "mtime", "structure"])
def test_verified_root_protected_fields_still_deny(artifact, monkeypatch, damage):
    app, host, *_ = artifact
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    damage_root(app, damage, monkeypatch)
    with pytest.raises(PreparationError):
        proof.check_current()


@pytest.mark.parametrize("damage", ["inode", "mode", "uid", "mtime", "structure"])
def test_command_local_root_protected_fields_still_deny(packaged, monkeypatch, damage):
    proof = shared_proof(packaged, monkeypatch)
    mutated = False
    def signature(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            damage_root(packaged.app, damage, monkeypatch)
    monkeypatch.setattr(command, "_signature", signature)
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        command.command_release_admission(policy(), packaged.host, artifact=proof)
    assert mutated


@pytest.mark.parametrize("damage", ["directory-ctime", "named-app-ctime", "file-ctime", "file-inode", "file-mode", "file-restored-bytes"])
def test_descendant_identity_never_receives_root_exemption(artifact, damage):
    app, host, member, *_ = artifact
    nested = app / "Nested.app"
    nested.mkdir()
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    if damage == "directory-ctime": ctime_only(host)
    elif damage == "named-app-ctime": ctime_only(nested)
    elif damage == "file-ctime": ctime_only(member)
    elif damage == "file-inode":
        replacement = member.with_name("replacement")
        replacement.write_bytes(member.read_bytes())
        replacement.replace(member)
    elif damage == "file-mode": member.chmod(0o600 if member.stat().st_mode & 0o044 else 0o644)
    else:
        before = member.stat()
        data = member.read_bytes()
        member.write_bytes(b"changed")
        member.write_bytes(data)
        os.utime(member, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(PreparationError):
        proof.check_current(full=True)


def set_acl(path, entry=None):
    arguments = ["-N"] if entry is None else ["+a", entry]
    result = NATIVE_RUN(["/bin/chmod", *arguments, str(path)], capture_output=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.parametrize("boundary", ["shared", "command"])
@pytest.mark.parametrize("change", ["add", "remove", "replace", "flags"])
def test_real_root_acl_and_flags_changes_revoke(packaged, monkeypatch, boundary, change):
    if change in {"remove", "replace"}:
        set_acl(packaged.app, "everyone allow readattr")
    proof = shared_proof(packaged, monkeypatch)
    before = artifact_module._identity(packaged.app)
    original_flags = packaged.app.stat().st_flags
    def mutate(*args, **kwargs):
        if change == "add": set_acl(packaged.app, "everyone allow readattr")
        elif change == "remove": set_acl(packaged.app)
        elif change == "replace":
            set_acl(packaged.app)
            set_acl(packaged.app, "everyone allow readsecurity")
        else: os.chflags(packaged.app, original_flags ^ stat.UF_HIDDEN)
        assert artifact_module._identity(packaged.app)[:-1] == before[:-1]
    try:
        if boundary == "command":
            monkeypatch.setattr(command, "_signature", mutate)
            with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
                command.command_release_admission(policy(), packaged.host, artifact=proof)
        else:
            mutate()
            with pytest.raises(PreparationError): proof.check_current()
            with pytest.raises(PreparationError): proof.check_current(full=True)
    finally:
        os.chflags(packaged.app, original_flags)
        set_acl(packaged.app)


def test_cosmetic_root_xattr_preserves_held_acl_and_flags(artifact):
    app, host, *_ = artifact
    set_acl(app, "everyone allow readattr")
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    metadata = proof._app_root_metadata
    before = artifact_module._identity(app)
    try:
        result = NATIVE_RUN(["/usr/bin/xattr", "-w", "com.mcpme.synthetic-review", "synthetic", str(app)], capture_output=True, timeout=5)
        assert result.returncode == 0
        assert artifact_module._identity(app)[:-1] == before[:-1]
        assert artifact_module._identity(app)[-1] != before[-1]
        for _ in range(10): proof.check_current(full=True)
        assert proof._app_root_metadata is metadata
    finally:
        result = NATIVE_RUN(["/usr/bin/xattr", "-d", "com.mcpme.synthetic-review", str(app)], capture_output=True, timeout=5)
        assert result.returncode == 0
        set_acl(app)


def test_held_root_descriptor_released_with_proof(artifact):
    import gc
    app, host, *_ = artifact
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    fd = proof._app_root_metadata.fd
    assert os.fstat(fd).st_ino == app.stat().st_ino
    del proof
    gc.collect()
    with pytest.raises(OSError): os.fstat(fd)


@pytest.mark.parametrize("failure", ["unreadable", "unsupported", "oversized", "copy-error", "invalid", "entries"])
def test_acl_inspection_failures_never_become_absence(artifact, monkeypatch, failure):
    import ctypes
    import errno
    from types import SimpleNamespace
    app, host, *_ = artifact
    set_acl(app, "everyone allow readattr")
    real = artifact_module._acl_api()
    fake = SimpleNamespace(**{name: getattr(real, name) for name in (
        "acl_get_fd_np", "acl_valid", "acl_get_entry", "acl_size", "acl_copy_ext", "acl_free")})
    if failure in {"unreadable", "unsupported"}:
        def unavailable(*args):
            ctypes.set_errno(errno.EACCES if failure == "unreadable" else errno.ENOTSUP)
            return None
        fake.acl_get_fd_np = unavailable
    elif failure == "oversized": fake.acl_size = lambda _: 4097
    elif failure == "copy-error": fake.acl_copy_ext = lambda *args: -1
    elif failure == "invalid": fake.acl_valid = lambda _: -1
    else: fake.acl_get_entry = lambda *args: 0
    monkeypatch.setattr(artifact_module, "_acl_api", lambda: fake)
    try:
        with pytest.raises(PreparationError): artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    finally: set_acl(app)
