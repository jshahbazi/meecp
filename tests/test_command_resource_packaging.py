"""Signed resource artifacts remain inert and retain their source identities."""
import hashlib
import json
from pathlib import Path
import platform
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_meecp_host as builder


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64", reason="Requires Apple silicon signing tools")
def test_actual_compile_sign_and_resign_refresh_only_artifact_hash(tmp_path):
    (tmp_path / "bin").mkdir()
    builder.bundle_command_helper(tmp_path)
    legacy_inventory = (tmp_path / "command-execution.json").read_bytes()
    legacy_helper = (tmp_path / "bin/mcpme-command-reap").read_bytes()
    builder.bundle_command_resource_helper(tmp_path)
    inventory = tmp_path / "command-resource-execution.json"
    before = json.loads(inventory.read_bytes())
    helper = tmp_path / before["helper"]
    assert before["helper_sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    for key, relative in (("helper_source_sha256", "scripts/meecp-command-resource-reap.c"),
                          ("helper_header_sha256", "scripts/meecp-command-resource-v1.h")):
        assert before[key] == hashlib.sha256((builder.ROOT / relative).read_bytes()).hexdigest()
    builder.run("/usr/bin/codesign", "--force", "--sign", "-", "--identifier", "local.meecp.resource.packaging-check", str(helper))
    builder.run("/usr/bin/codesign", "--verify", "--strict", str(helper))
    assert hashlib.sha256(helper.read_bytes()).hexdigest() != before["helper_sha256"]
    builder.refresh_command_resource_inventory(tmp_path)
    assert json.loads(inventory.read_bytes()) == {**before, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
    assert (tmp_path / "command-execution.json").read_bytes() == legacy_inventory
    assert (tmp_path / "bin/mcpme-command-reap").read_bytes() == legacy_helper


@pytest.mark.parametrize("field,value", [
    ("production_admission", "enabled"), ("helper", "../another-helper"),
    ("lifetime_coverage_complete", True), ("hard_aggregate_enforced", True), ("result_admissible", True),
    ("hard_aggregate_enforced", 0), ("receipt_format", 3), ("helper_header_sha256", "invalid"),
    ("unknown_authority", True),
])
def test_distribution_refresh_rejects_changed_contract_without_writing(tmp_path, field, value):
    inventory = builder._resource_inventory_contract()
    inventory.update(helper_sha256="a" * 64, helper_source_sha256="b" * 64, helper_header_sha256="c" * 64)
    inventory[field] = value
    path = tmp_path / "command-resource-execution.json"
    path.write_text(json.dumps(inventory))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="^command_resource_inventory_invalid$"):
        builder.refresh_command_resource_inventory(tmp_path)
    assert path.read_bytes() == original
