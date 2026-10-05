import struct

import pytest

from mcpme.command_resource_policy_v1 import ResourcePolicyError, ResourcePolicyV1, ResourceReportV1


@pytest.mark.parametrize("field,value", [("footprint_bytes", True), ("footprint_bytes", 0),
    ("footprint_bytes", 2**40), ("group_members", 0), ("group_members", 65), ("cpu_ns", -1),
    ("interval_ns", 999999), ("read_window_ns", 0), ("sample_gap_ns", 1),
    ("identity_capacity", 2), ("identity_capacity", 1025), ("sample_capacity", 1000001)])
def test_closed_resource_policy_rejects_invalid_or_inconsistent_values(field, value):
    with pytest.raises(ResourcePolicyError):
        ResourcePolicyV1(**{field: value})


def test_policy_digest_binds_numeric_controls_and_wire_is_fixed():
    first = ResourcePolicyV1()
    assert len(first.wire()) == 64
    assert first.digest == ResourcePolicyV1().digest
    assert first.digest != ResourcePolicyV1(cpu_ns=1).digest


@pytest.mark.parametrize("index,value", [(0, 16), (1, 2), (1, 1), (2, 1), (3, 1000002), (4, 1), (5, 1), (6, 1), (7, 66), (9, 1025),
    (12, 2**32), (13, 2**32), (14, 1), (15, 1)])
def test_unknown_malformed_or_stronger_resource_claims_are_rejected(index, value):
    fields = [0] * 16
    fields[0] = 14
    fields[index] = value
    with pytest.raises(ResourcePolicyError):
        ResourceReportV1.from_wire(struct.pack("!16Q", *fields))
