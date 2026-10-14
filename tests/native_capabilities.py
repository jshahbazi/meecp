"""Narrow baseline admission for hardware-dependent native test cases."""

import re

import pytest


def require_metal_compute(result):
    # Only a fixed, unconfined test control can call this admission function.
    # Target helpers and sandboxed executions must never turn their failures
    # into a skip. Unknown statuses, crashes, output, and incorrect arithmetic
    # in the independent control are failures too.
    if result.returncode == 11 and not result.stderr and result.stdout:
        identifier = re.fullmatch(rb"residency_error:([A-Za-z0-9_.]{1,128}):(-?[0-9]{1,5})\n", result.stdout)
        if identifier and abs(int(identifier[2])) <= 65535:
            pytest.fail("metal_compute_control_failed_residency_" + identifier[1].decode("ascii")
                + "_" + identifier[2].decode("ascii"), pytrace=False)
    if result.stdout or result.stderr:
        pytest.fail("metal_compute_control_output_invalid", pytrace=False)
    if result.returncode == 75:
        pytest.skip("host has no Metal device; hardware compute acceptance requires a GPU")
    if result.returncode == 76:
        pytest.skip("host Metal device lacks the required residency-set feature family")
    if result.returncode == 77:
        pytest.skip("host Metal device explicitly reports residency sets unsupported")
    if result.returncode != 0:
        stages = {11: "residency", 12: "buffer", 13: "shader_library", 14: "function",
                  15: "pipeline", 16: "queue", 17: "command_buffer", 18: "encoder",
                  19: "execution", 20: "arithmetic"}
        stage = stages.get(result.returncode, "status")
        pytest.fail("metal_compute_control_failed_" + stage, pytrace=False)
