"""Reverse memory admission accounts for an independently running Pi probe."""

import pytest

from meecp.preparation_capture import CapturePolicyV1
from meecp.preparation_resolution import ResolutionPolicyV1
from meecp.supervisor import HostError, Supervisor
from test_artifact_capture_api import body as capture_body
from test_meecp_preparation_host import host_for, managed_job
from test_meecp_preparations import foundation, policy as preparation_policy
from test_preparation_resolution import policy as resolution_policy
from test_resolution_host import MetadataResolutionRunner, job_for


def hold_probe_at_budget(host, candidate_memory, gpu):
    # The new work fits exactly alongside an installed CPU app once the probe
    # settles. The extra probe charge must block it until that settlement.
    host.reservations["existing-app"] = {"memory_mb": 64, "gpu": False}
    host.resource_policy["memory_budget_mb"] = candidate_memory + 64
    probe = {"memory_mb": 32, "gpu": gpu}
    host.pi_setup.probe_reservations["held-probe"] = probe
    return probe


@pytest.mark.parametrize("gpu", [False, True])
async def test_preparation_admission_includes_held_pi_probe(tmp_path, foundation, gpu):
    imports, _, _, draft, descriptor = foundation
    host = host_for(tmp_path, imports.root)
    policy = preparation_policy()
    job = managed_job(host, draft, descriptor, policy)
    expected = host.preparation_reservation(policy)
    probe = hold_probe_at_budget(host, expected["memory_mb"], gpu)
    try:
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.reserve_preparation(job["job_id"], policy)
        assert job["job_id"] not in host.preparation_reservations
        assert host.pi_setup.probe_reservations == {"held-probe": probe}
        host.pi_setup.probe_reservations.pop("held-probe")
        await host.reserve_preparation(job["job_id"], policy)
        assert host.preparation_reservations[job["job_id"]] == expected
        assert set(expected) == {"memory_mb", "disk_bytes", "policy_digest"}
        assert host.reservations == {"existing-app": {"memory_mb": 64, "gpu": False}}
    finally:
        await host.close()


@pytest.mark.parametrize("gpu", [False, True])
async def test_resolution_admission_includes_held_pi_probe(tmp_path, gpu):
    host = Supervisor(tmp_path / "state", tmp_path / "unused", resolution_runner_factory=MetadataResolutionRunner)
    policy = ResolutionPolicyV1.model_validate(resolution_policy())
    job, _ = job_for(host, tmp_path, policy.model_dump(mode="json"))
    resource_id = "resolution:" + job["job_id"]
    expected = host.resolution_reservation(policy)
    probe = hold_probe_at_budget(host, expected["memory_mb"], gpu)
    try:
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.reserve_resolution(resource_id, policy)
        assert host.pi_setup.probe_reservations == {"held-probe": probe}
        host.pi_setup.probe_reservations.pop("held-probe")
        await host.reserve_resolution(resource_id, policy)
        assert host.resolution_reservations[job["job_id"]] == expected
        assert set(expected) == {"memory_mb", "disk_bytes", "policy_digest"}
        assert host.reservations == {"existing-app": {"memory_mb": 64, "gpu": False}}
    finally:
        await host.close()


@pytest.mark.parametrize("gpu", [False, True])
async def test_capture_admission_includes_held_pi_probe(tmp_path, gpu):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    payload = capture_body(tmp_path / "selected")
    queued = host.captures.begin(payload["request"], payload["policy"], payload["target"],
        payload["idempotency_key"], host.validate_capture_target)
    policy = CapturePolicyV1.model_validate(payload["policy"])
    expected = {"memory_mb": policy.memory_mb + host.resource_policy["headroom_mb"],
        "disk_bytes": policy.disk_ceiling_bytes, "policy_digest": policy.digest}
    probe = hold_probe_at_budget(host, expected["memory_mb"], gpu)
    try:
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.reserve_capture(queued["capture_id"], policy)
        assert host.pi_setup.probe_reservations == {"held-probe": probe}
        host.pi_setup.probe_reservations.pop("held-probe")
        await host.reserve_capture(queued["capture_id"], policy)
        assert host.capture_reservations[queued["capture_id"]] == expected
        assert set(expected) == {"memory_mb", "disk_bytes", "policy_digest"}
        assert host.reservations == {"existing-app": {"memory_mb": 64, "gpu": False}}
    finally:
        await host.close()
