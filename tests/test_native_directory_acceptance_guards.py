"""Compiled native directory entry consumes only a private, fresh context."""
import os
import json
import subprocess
import time
import uuid

import pytest

from test_recipe_native_management_acceptance import file_identity, native_recipe_driver, private_json


def context(tmp_path):
    workspace = tmp_path.resolve() / ("mcpme-native-onboarding-" + str(uuid.uuid4()))
    root = workspace / "directory_guard"
    state, source, home, temporary = (root / name for name in ("state", "source", "home", "tmp"))
    for path in (workspace, root, state, source, home, temporary):
        path.mkdir(mode=0o700)
    manager = state / "manager-connection.json"
    private_json(manager, {"synthetic": True})
    nonce = str(uuid.uuid4())
    marker = state / ("native-directory-" + nonce + ".json")
    details = source.stat(follow_symlinks=False)
    proof = {"schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 120,
        "manager_file": file_identity(manager), "source_directory": {"device": details.st_dev, "inode": details.st_ino}}
    fixture, output = root / "case.json", root / "receipt.json"
    case = {"schema_version": 1, "fixture_nonce": nonce, "state_dir": str(state),
        "output_path": str(output), "source_directory": str(source), "answers_by_label": {}}
    return root, state, source, home, temporary, manager, marker, fixture, output, nonce, proof, case


def run(binary, fixture, state, home, temporary):
    child = subprocess.Popen([str(binary), "--directory-native-acceptance", str(fixture)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary), "MCPME_STATE_DIR": str(state)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        printed, error = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill(); child.communicate()
        pytest.fail("native_directory_guard_timeout", pytrace=False)
    assert child.returncode == 1 and printed == b"directory_native_acceptance_failed\n" and error == b""


@pytest.mark.parametrize("fault", ["unknown_field", "wrong_nonce", "expired", "manager_replaced",
    "source_replaced", "source_link", "marker_hardlink", "fixture_mode", "claimed_nonce",
    "existing_output", "output_link", "outside_source"])
def test_unowned_or_stale_directory_is_rejected_before_claim(native_recipe_driver, tmp_path, fault):
    root, state, source, home, temporary, manager, marker, fixture, output, nonce, proof, case = context(tmp_path)
    if fault == "unknown_field":
        case["descriptor"] = {}
    elif fault == "wrong_nonce":
        proof["fixture_nonce"] = str(uuid.uuid4())
    elif fault == "expired":
        proof["expires_at"] = int(time.time()) - 1
    elif fault == "outside_source":
        case["source_directory"] = str(root.parent)
    elif fault == "source_link":
        linked = root / "linked"
        linked.symlink_to(source, target_is_directory=True)
        case["source_directory"] = str(linked)
    private_json(marker, proof); private_json(fixture, case)
    claimed = state / ("native-directory-" + nonce + ".claimed")
    if fault == "manager_replaced":
        replacement = state / "replacement.json"
        private_json(replacement, {"synthetic": True}); replacement.replace(manager)
    elif fault == "source_replaced":
        source.rename(root / "old_source"); source.mkdir(mode=0o700)
    elif fault == "marker_hardlink":
        os.link(marker, state / "second-marker.json")
    elif fault == "fixture_mode":
        fixture.chmod(0o644)
    elif fault == "claimed_nonce":
        private_json(claimed, {"consumed": True})
    elif fault == "existing_output":
        private_json(output, {"existing": True})
    elif fault == "output_link":
        output.symlink_to(root / "missing-output")
    before_claim = claimed.read_bytes() if claimed.exists() else None
    before_output = output.read_bytes() if output.exists() else None
    run(native_recipe_driver, fixture, state, home, temporary)
    assert (claimed.read_bytes() if claimed.exists() else None) == before_claim
    assert (output.read_bytes() if output.exists() else None) == before_output


def test_positive_python_fixture_claims_once_before_invalid_connection(native_recipe_driver, tmp_path):
    root, state, source, home, temporary, manager, marker, fixture, output, nonce, proof, case = context(tmp_path)
    private_json(marker, proof); private_json(fixture, case)
    claimed = state / ("native-directory-" + nonce + ".claimed")
    identity = None
    output_identity = None
    for _ in range(2):
        run(native_recipe_driver, fixture, state, home, temporary)
        assert claimed.read_bytes() == b"claimed\n"
        receipt = json.loads(output.read_bytes())
        assert receipt["passed"] is False and receipt["stage"] == "connection" and receipt["routes"] == []
        observed = file_identity(claimed)
        assert identity is None or observed == identity
        identity = observed
        observed_output = file_identity(output)
        assert output_identity is None or observed_output == output_identity
        output_identity = observed_output
