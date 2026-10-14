"""Narrow baseline admission for hardware-dependent native test cases."""

import pytest


def require_metal_device(result):
    # The independent control returns 75 only for a nil Metal device. Failures
    # to launch, crashes, unexpected output, and other statuses are failures.
    if result.stdout or result.stderr:
        pytest.fail("metal_device_control_output_invalid", pytrace=False)
    if result.returncode == 75:
        pytest.skip("host has no Metal device; hardware compute acceptance requires a GPU")
    if result.returncode != 0:
        pytest.fail("metal_device_control_failed", pytrace=False)
