"""Missing hardware is distinct from control and confinement failures."""

import subprocess

import pytest
from native_capabilities import require_metal_device


def test_explicit_absent_metal_device_is_the_only_skipped_control_status():
    result = subprocess.CompletedProcess(["fixed-device-control"], 75, b"", b"")
    with pytest.raises(pytest.skip.Exception, match="host has no Metal device"):
        require_metal_device(result)


def test_present_metal_device_admits_compute_acceptance():
    require_metal_device(subprocess.CompletedProcess(["fixed-device-control"], 0, b"", b""))


@pytest.mark.parametrize("returncode", [-9, 1, 10, 11, 13, 127])
def test_failed_device_control_cannot_be_skipped(returncode):
    result = subprocess.CompletedProcess(["fixed-device-control"], returncode, b"", b"")
    with pytest.raises(pytest.fail.Exception, match="^metal_device_control_failed$"):
        require_metal_device(result)


@pytest.mark.parametrize("returncode", [0, 75])
@pytest.mark.parametrize("streams", [(b"unexpected", b""), (b"", b"unexpected")])
def test_unexpected_device_control_output_cannot_claim_presence_or_absence(returncode, streams):
    result = subprocess.CompletedProcess(["fixed-device-control"], returncode, *streams)
    with pytest.raises(pytest.fail.Exception, match="^metal_device_control_output_invalid$"):
        require_metal_device(result)
