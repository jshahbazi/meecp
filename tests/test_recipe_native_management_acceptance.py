"""Compiled production driver rejects fixtures before any owner request."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest


@pytest.fixture(scope="module")
def native_recipe_driver(tmp_path_factory):
    if sys.platform != "darwin":
        pytest.skip("native AppKit acceptance requires macOS")
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path_factory.mktemp("native-recipe-build").resolve()
    inputs = [root / "Package.swift"] + sorted(path for folder in ("Sources", "native")
        for path in (root / folder).rglob("*") if path.is_file())
    def hashes():
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    before = hashes()
    child = subprocess.Popen(["/usr/bin/swift", "build", "--package-path", str(root),
        "--scratch-path", str(scratch), "-c", "release", "--product", "McpmeMenuBar"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, error = child.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail("native_recipe_compile_timeout", pytrace=False)
    if child.returncode != 0:
        pytest.fail("native_recipe_compile_failed", pytrace=False)
    binary = scratch / "arm64-apple-macosx" / "release" / "McpmeMenuBar"
    assert binary.is_file()
    assert hashes() == before
    private_json(scratch / "native-build-receipt.json", {"schema_version": 1,
        "compiler_exit_code": child.returncode, "compiler_joined": True,
        "compiler_stdout_bytes": len(output), "compiler_stderr_bytes": len(error),
        "source_inputs_before_after": before, "binary_path": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()})
    # Compiler output stays in memory and is never emitted, including on failure.
    del output, error
    return binary


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(value, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())


def file_identity(path):
    record = path.stat(follow_symlinks=False)
    return {"device": record.st_dev, "inode": record.st_ino, "bytes": record.st_size,
        "mode": record.st_mode & 0o777,
        "modified_seconds": record.st_mtime_ns // 1_000_000_000,
        "modified_nanoseconds": record.st_mtime_ns % 1_000_000_000,
        "changed_seconds": record.st_ctime_ns // 1_000_000_000,
        "changed_nanoseconds": record.st_ctime_ns % 1_000_000_000}


@pytest.mark.parametrize("fault", ["unknown_field", "unsupported_action", "extra_recipe_field",
    "wrong_nonce", "expired", "manager_replaced", "marker_hardlink", "fixture_mode",
    "symlink_folder", "claimed_nonce", "existing_output"])
def test_compiled_driver_rejects_unowned_or_stale_fixture(native_recipe_driver, tmp_path, fault):
    workspace = tmp_path.resolve() / ("mcpme-native-onboarding-" + str(uuid.uuid4()))
    root = workspace / "native_management"
    state, folder, home, temporary = (root / name for name in ("state", "recipe", "home", "tmp"))
    for directory in (workspace, root, state, folder, home, temporary):
        directory.mkdir(mode=0o700)
    manager = state / "manager-connection.json"
    # This is an invalid synthetic connection, never a real key or production state.
    # Every fault must be rejected before connection parsing or nonce consumption.
    private_json(manager, {"synthetic": True})
    nonce = str(uuid.uuid4())
    marker = state / ("native-recipes-" + nonce + ".json")
    proof = {"schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 120,
        "manager_file": file_identity(manager)}
    output, fixture = root / "receipt.json", root / "case.json"
    row = {"folder": str(folder), "recipe": {"recipe_id": "local.native-control",
        "version": "1.0.0", "digest": "a" * 64},
        "operations": [{"action": "add", "expected_state": "proposed"}]}
    case = {"schema_version": 1, "state_dir": str(state), "output_path": str(output),
        "fixture_nonce": nonce, "recipes": [row], "forms": []}
    if fault == "unknown_field":
        case["unsupported"] = True
    elif fault == "unsupported_action":
        row["operations"][0]["action"] = "install"
    elif fault == "extra_recipe_field":
        row["recipe"]["enabled"] = True
    elif fault == "wrong_nonce":
        proof["fixture_nonce"] = str(uuid.uuid4())
    elif fault == "expired":
        proof["expires_at"] = int(time.time()) - 1
    elif fault == "symlink_folder":
        linked = root / "linked-recipe"
        linked.symlink_to(folder, target_is_directory=True)
        row["folder"] = str(linked)
    private_json(marker, proof)
    private_json(fixture, case)
    claimed = state / ("native-recipes-" + nonce + ".claimed")
    if fault == "manager_replaced":
        replacement = state / "replacement.json"
        private_json(replacement, {"synthetic": True})
        replacement.replace(manager)
    elif fault == "marker_hardlink":
        os.link(marker, state / "second-marker.json")
    elif fault == "fixture_mode":
        fixture.chmod(0o644)
    elif fault == "claimed_nonce":
        private_json(claimed, {"consumed": True})
    elif fault == "existing_output":
        private_json(output, {"existing": True})
    before_claim = claimed.read_bytes() if claimed.exists() else None
    before_output = output.read_bytes() if output.exists() else None
    child = subprocess.Popen([str(native_recipe_driver), "--recipes-native-acceptance", str(fixture)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary),
             "MCPME_STATE_DIR": str(state)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        printed, error = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail("native_recipe_guard_timeout", pytrace=False)
    assert child.returncode == 1
    assert printed == b"recipes_native_acceptance_failed\n" and error == b""
    assert (claimed.read_bytes() if claimed.exists() else None) == before_claim
    assert (output.read_bytes() if output.exists() else None) == before_output


def test_compiled_context_checks_replay_and_same_byte_inode_substitution(native_recipe_driver, tmp_path):
    home, temporary = tmp_path / "home", tmp_path / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    child = subprocess.Popen([str(native_recipe_driver), "--recipes-native-context-self-test"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        printed, error = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail("native_recipe_context_timeout", pytrace=False)
    assert child.returncode == 0
    assert printed == b"recipes_native_context_passed\n" and error == b""
    assert not list(temporary.iterdir())
