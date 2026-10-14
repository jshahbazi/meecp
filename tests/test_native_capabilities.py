"""Missing hardware is distinct from control and confinement failures."""

import subprocess

import pytest
from native_capabilities import require_metal_compute


def test_explicit_absent_metal_device_skips_the_independent_control():
    result = subprocess.CompletedProcess(["fixed-device-control"], 75, b"", b"")
    with pytest.raises(pytest.skip.Exception, match="host has no Metal device"):
        require_metal_compute(result)


def test_explicit_unsupported_residency_family_skips_the_independent_control():
    result = subprocess.CompletedProcess(["fixed-compute-control"], 76, b"", b"")
    with pytest.raises(pytest.skip.Exception, match="host Metal device lacks"):
        require_metal_compute(result)


def test_successful_independent_compute_admits_confined_acceptance():
    require_metal_compute(subprocess.CompletedProcess(["fixed-compute-control"], 0, b"", b""))


@pytest.mark.parametrize("returncode", [-9, 1, *range(10, 21), 127])
def test_failed_compute_control_cannot_be_skipped(returncode):
    result = subprocess.CompletedProcess(["fixed-compute-control"], returncode, b"", b"")
    with pytest.raises(pytest.fail.Exception, match="^metal_compute_control_failed_"):
        require_metal_compute(result)


@pytest.mark.parametrize("returncode,stage", [(11, "residency"), (13, "shader_library"),
    (16, "queue"), (19, "execution"), (20, "arithmetic")])
def test_fatal_compute_control_status_preserves_failure_stage(returncode, stage):
    result = subprocess.CompletedProcess(["fixed-compute-control"], returncode, b"", b"")
    with pytest.raises(pytest.fail.Exception, match="^metal_compute_control_failed_" + stage + "$"):
        require_metal_compute(result)


@pytest.mark.parametrize("returncode", [0, 75, 76])
@pytest.mark.parametrize("streams", [(b"unexpected", b""), (b"", b"unexpected")])
def test_unexpected_compute_control_output_cannot_claim_capability_or_absence(returncode, streams):
    result = subprocess.CompletedProcess(["fixed-compute-control"], returncode, *streams)
    with pytest.raises(pytest.fail.Exception, match="^metal_compute_control_output_invalid$"):
        require_metal_compute(result)
