"""Real filesystem identity controls; enclosing signatures are synthetic."""
from dataclasses import replace
import os
import stat
import subprocess
import time

import pytest

from meecp import preparation_release_artifact as artifact_module
from meecp import preparation_release_command_admission as command
from meecp.preparation_contracts import PreparationError
from meecp.preparation_release_policy import POLICY_PATH
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
    monkeypatch.setattr(artifact_module, "__file__", str(packaged.host / "backend/meecp/preparation_release_artifact.py"))
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
        result = NATIVE_RUN(["/usr/bin/xattr", "-w", "com.meecp.synthetic-review", "synthetic", str(app)], capture_output=True, timeout=5)
        assert result.returncode == 0
        assert artifact_module._identity(app)[:-1] == before[:-1]
        assert artifact_module._identity(app)[-1] != before[-1]
        for _ in range(10): proof.check_current(full=True)
        assert proof._app_root_metadata is metadata
    finally:
        result = NATIVE_RUN(["/usr/bin/xattr", "-d", "com.meecp.synthetic-review", str(app)], capture_output=True, timeout=5)
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
        "acl_get_fd_np", "acl_valid_fd_np", "acl_valid", "acl_dup", "acl_get_entry", "acl_size", "acl_copy_ext", "acl_free")})
    if failure in {"unreadable", "unsupported"}:
        def unavailable(*args):
            ctypes.set_errno(errno.EACCES if failure == "unreadable" else errno.ENOTSUP)
            return None
        fake.acl_get_fd_np = unavailable
    elif failure == "oversized": fake.acl_size = lambda _: 4097
    elif failure == "copy-error": fake.acl_copy_ext = lambda *args: -1
    elif failure == "invalid":
        def invalid(*args):
            ctypes.set_errno(errno.EINVAL)
            return -1
        fake.acl_valid_fd_np = invalid
    else: fake.acl_get_entry = lambda *args: 0
    monkeypatch.setattr(artifact_module, "_acl_api", lambda: fake)
    try:
        with pytest.raises(PreparationError): artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    finally: set_acl(app)


@pytest.mark.parametrize("boundary", ["shared", "command", "acl-read"])
def test_owned_root_group_switch_revokes(packaged, monkeypatch, boundary):
    # /private/tmp may inherit a group the owner cannot restore after chown.
    # Establish a restorable owned-fixture baseline before taking the proof.
    os.chown(packaged.app, -1, os.getgid())
    original_gid = packaged.app.stat().st_gid
    alternatives = sorted(set(os.getgroups()) - {original_gid})
    if not alternatives:
        pytest.skip("no supplementary group available; explicit stat controls remain mandatory")
    proof = shared_proof(packaged, monkeypatch)
    before = artifact_module._identity(packaged.app)
    def mutate(*args, **kwargs):
        os.chown(packaged.app, -1, alternatives[0])
        assert artifact_module._identity(packaged.app)[:-1] == before[:-1]
    try:
        if boundary == "command":
            monkeypatch.setattr(command, "_signature", mutate)
            with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
                command.command_release_admission(policy(), packaged.host, artifact=proof)
        else:
            if boundary == "acl-read":
                original_acl = artifact_module._acl_identity
                def during(fd):
                    value = original_acl(fd)
                    mutate()
                    return value
                monkeypatch.setattr(artifact_module, "_acl_identity", during)
            else: mutate()
            with pytest.raises(PreparationError): proof.check_current()
    finally:
        os.chown(packaged.app, -1, original_gid)


@pytest.mark.parametrize("boundary", ["fd-before", "fd-after", "path-before", "path-after"])
def test_root_group_stat_fixture_revokes_without_other_metadata_change(artifact, monkeypatch, boundary):
    from pathlib import Path
    from types import SimpleNamespace
    app, host, *_ = artifact
    proof = artifact_module.verify_release_artifact(host, POLICY_PATH, b"{}")
    fd = proof._app_root_metadata.fd
    original_fstat, original_lstat = os.fstat, Path.lstat
    changed = not boundary.endswith("after")
    def observed(value):
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
        fields["st_gid"] += 1
        return SimpleNamespace(**fields)
    def fstat(selected):
        value = original_fstat(selected)
        return observed(value) if changed and selected == fd and boundary.startswith("fd") else value
    def lstat(path):
        value = original_lstat(path)
        return observed(value) if changed and path == app and boundary.startswith("path") else value
    original_acl = artifact_module._acl_identity
    def acl(selected):
        nonlocal changed
        value = original_acl(selected)
        changed = True
        return value
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(artifact_module, "_acl_identity", acl)
    with pytest.raises(PreparationError): proof.check_current(full=True)


def ordered_acl(path, reverse=False):
    set_acl(path)
    entries = ["everyone allow readattr", "everyone deny add_file"]
    if reverse: entries.reverse()
    for index, entry in enumerate(entries):
        result = NATIVE_RUN(["/bin/chmod", "+a#", str(index), entry, str(path)], capture_output=True, timeout=5)
        assert result.returncode == 0


@pytest.mark.parametrize("boundary", ["shared", "command"])
def test_real_root_acl_entry_order_is_bound(packaged, monkeypatch, boundary):
    ordered_acl(packaged.app)
    proof = shared_proof(packaged, monkeypatch)
    original = proof._app_root_metadata.acl
    def reorder(*args, **kwargs): ordered_acl(packaged.app, reverse=True)
    try:
        for _ in range(5): proof.check_current(full=True)
        if boundary == "command":
            monkeypatch.setattr(command, "_signature", reorder)
            with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
                command.command_release_admission(policy(), packaged.host, artifact=proof)
        else:
            reorder()
            changed = artifact_module._acl_identity(proof._app_root_metadata.fd)
            assert changed != original
            with pytest.raises(PreparationError): proof.check_current()
    finally: set_acl(packaged.app)


@pytest.mark.parametrize("mode", ["fd-valid", "copy-mutates", "fd-error", "copy-invalid", "duplicate-null", "duplicate-alias", "free-error"])
def test_acl_validation_copy_never_becomes_identity_and_is_freed(artifact, monkeypatch, mode):
    import ctypes
    import errno
    import hashlib
    from types import SimpleNamespace
    app, *_ = artifact
    ordered_acl(app)
    fd = os.open(app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    api = artifact_module._acl_api()
    original = api.acl_get_fd_np(fd, 0x100)
    size = api.acl_size(original)
    buffer = ctypes.create_string_buffer(size)
    assert api.acl_copy_ext(buffer, original, size) == size
    expected = hashlib.sha256(buffer.raw).digest()
    api.acl_free(original)
    fake = SimpleNamespace(**{name: getattr(api, name) for name in (
        "acl_get_fd_np", "acl_valid_fd_np", "acl_valid", "acl_dup", "acl_get_entry", "acl_size", "acl_copy_ext", "acl_free")})
    acquired, duplicated, freed, generic = [], [], [], []
    def acquire(*args):
        value = api.acl_get_fd_np(*args)
        acquired.append(value)
        return value
    def duplicate(value):
        if mode == "duplicate-null": return None
        if mode == "duplicate-alias": return value
        result = api.acl_dup(value)
        duplicated.append(result)
        return result
    def validate_fd(selected, kind, value):
        assert selected == fd and kind == 0x100 and value == duplicated[0]
        if mode == "fd-valid": return 0
        ctypes.set_errno(errno.EACCES if mode == "fd-error" else errno.ENOTSUP)
        return -1
    def validate(value):
        generic.append(value)
        assert value == duplicated[0] and value != acquired[0]
        if mode == "copy-invalid": return -1
        if mode == "copy-mutates":
            # Model the documented reordering/mutation permission by deleting
            # an entry from only the disposable validation copy.
            entry = ctypes.c_void_p()
            assert api.acl_get_entry(value, 0, ctypes.byref(entry)) == 0
            api.acl_delete_entry.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            api.acl_delete_entry.restype = ctypes.c_int
            assert api.acl_delete_entry(value, entry) == 0
        return api.acl_valid(value)
    def free(value):
        freed.append(value)
        result = api.acl_free(value)
        return -1 if mode == "free-error" and value in duplicated else result
    fake.acl_get_fd_np, fake.acl_dup, fake.acl_valid_fd_np = acquire, duplicate, validate_fd
    fake.acl_valid, fake.acl_free = validate, free
    monkeypatch.setattr(artifact_module, "_acl_api", lambda: fake)
    try:
        if mode in {"fd-valid", "copy-mutates"}:
            assert artifact_module._acl_identity(fd) == expected
        else:
            with pytest.raises(PreparationError): artifact_module._acl_identity(fd)
        assert freed == [*duplicated, *acquired]
        if mode in {"fd-valid", "fd-error", "duplicate-null", "duplicate-alias"}: assert not generic
    finally:
        os.close(fd)
        set_acl(app)
