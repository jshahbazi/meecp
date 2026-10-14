"""Compiled production driver rejects fixtures before any owner request."""
import json
import os
import subprocess
import time
import uuid

import pytest


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
@pytest.mark.skip(reason="Native manual/external-agent setup workflow retired; historical acceptance only")
def test_compiled_driver_rejects_unowned_or_stale_fixture(native_recipe_driver, tmp_path, fault):
    workspace = tmp_path.resolve() / ("meecp-native-onboarding-" + str(uuid.uuid4()))
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


@pytest.mark.skip(reason="Native manual/external-agent setup workflow retired; historical acceptance only")
def test_python_canonical_fixture_reaches_exact_once_claim(native_recipe_driver, tmp_path):
    workspace = tmp_path.resolve() / ("meecp-native-onboarding-" + str(uuid.uuid4()))
    root = workspace / "native_management"
    state, home, temporary = (root / name for name in ("state", "home", "tmp"))
    for directory in (workspace, root, state, home, temporary):
        directory.mkdir(mode=0o700)
    manager = state / "manager-connection.json"
    private_json(manager, {"synthetic": True})
    nonce = str(uuid.uuid4())
    marker = state / ("native-recipes-" + nonce + ".json")
    private_json(marker, {"schema_version": 1, "fixture_nonce": nonce,
        "expires_at": int(time.time()) + 120, "manager_file": file_identity(manager)})
    output, fixture = root / "receipt.json", root / "case.json"
    private_json(fixture, {"schema_version": 1, "state_dir": str(state), "output_path": str(output),
        "fixture_nonce": nonce, "recipes": [{"folder": None, "recipe": {
            "recipe_id": "local.native-control", "version": "1.0.0", "digest": "a" * 64},
            "operations": [{"action": "inspect", "expected_state": "proposed"}]}], "forms": []})
    claimed = state / ("native-recipes-" + nonce + ".claimed")
    identity = None
    for _ in range(2):
        child = subprocess.Popen([str(native_recipe_driver), "--recipes-native-acceptance", str(fixture)],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary),
                 "MCPME_STATE_DIR": str(state)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            printed, error = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail("native_recipe_positive_admission_timeout", pytrace=False)
        # A valid Python-created context is consumed exactly once. The deliberately
        # invalid synthetic connection then prevents any owner request; actual
        # socket acceptance is a separate independently authored live control.
        assert child.returncode == 1
        assert printed == b"recipes_native_acceptance_failed\n" and error == b""
        assert claimed.read_bytes() == b"claimed\n" and not output.exists()
        observed = file_identity(claimed)
        assert identity is None or observed == identity
        identity = observed
