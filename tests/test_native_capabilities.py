"""Missing hardware is distinct from control and confinement failures."""

import subprocess
import sys
from pathlib import Path

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


def test_explicit_unsupported_residency_error_skips_the_independent_control():
    result = subprocess.CompletedProcess(["fixed-compute-control"], 77, b"", b"")
    with pytest.raises(pytest.skip.Exception, match="explicitly reports residency sets unsupported"):
        require_metal_compute(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="native Metal classifier requires macOS")
def test_native_residency_error_classifier_rejects_unrecognized_errors(tmp_path):
    # Exercise the exact native classifier with real NSError instances. Domain,
    # code, and missing-error cases must not collapse into a hardware skip.
    control = Path(__file__).with_name("native_metal_control.m").resolve()
    source = tmp_path / "classifier.m"
    source.write_text('#define main metalControlMain\n'
        f'#include "{control.as_posix()}"\n'
        '#undef main\n'
        'int main(void) { @autoreleasepool {\n'
        '    if (residencyFailureStatus(nil) != 11) return 1;\n'
        '    if (residencyFailureStatus([NSError errorWithDomain:@"unknown" code:1 userInfo:nil]) != 11) return 2;\n'
        '#if __MAC_OS_X_VERSION_MAX_ALLOWED >= 260400\n'
        '    if (@available(macOS 26.4, *)) {\n'
        '        if (residencyFailureStatus([NSError errorWithDomain:MTLDeviceErrorDomain code:MTLDeviceErrorNotSupported userInfo:nil]) != 77) return 3;\n'
        '        if (residencyFailureStatus([NSError errorWithDomain:MTLDeviceErrorDomain code:MTLDeviceErrorNone userInfo:nil]) != 11) return 4;\n'
        '        if (residencyFailureStatus([NSError errorWithDomain:MTLDeviceErrorDomain code:999 userInfo:nil]) != 11) return 5;\n'
        '        return 0;\n'
        '    }\n'
        '#endif\n'
        '    return 78;\n'
        '} }\n')
    binary = tmp_path / "classifier"
    compiled = subprocess.run(["/usr/bin/clang", "-fobjc-arc", "-O2", "-Wall", "-Wextra", "-Werror",
        "-framework", "Foundation", "-framework", "Metal", str(source), "-o", str(binary)],
        capture_output=True, timeout=30)
    assert compiled.returncode == 0, "metal_residency_classifier_compile_failed"
    result = subprocess.run([str(binary)], capture_output=True, timeout=30)
    assert not result.stdout and not result.stderr
    if result.returncode == 78:
        pytest.skip("documented Metal device errors require macOS 26.4 and its SDK")
    assert result.returncode == 0, "metal_residency_classifier_failed"


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


@pytest.mark.parametrize("returncode", [0, 75, 76, 77])
@pytest.mark.parametrize("streams", [(b"unexpected", b""), (b"", b"unexpected")])
def test_unexpected_compute_control_output_cannot_claim_capability_or_absence(returncode, streams):
    result = subprocess.CompletedProcess(["fixed-compute-control"], returncode, *streams)
    with pytest.raises(pytest.fail.Exception, match="^metal_compute_control_output_invalid$"):
        require_metal_compute(result)
