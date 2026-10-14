"""Shared native build and hardware fixtures for the acceptance suite."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest
from native_capabilities import require_metal_device


@pytest.fixture(scope="session")
def metal_device(tmp_path_factory):
    if not (sys.platform == "darwin" and platform.machine() == "arm64"
            and platform.mac_ver()[0].startswith("26.")):
        pytest.skip("native Metal requires macOS 26 arm64")
    root = tmp_path_factory.mktemp("metal-device-control")
    source, binary = root / "device.m", root / "device"
    source.write_text("#import <Metal/Metal.h>\nint main(void) { @autoreleasepool {\n"
                      "return MTLCreateSystemDefaultDevice() ? 0 : 75;\n} }\n")
    # This unconfined fixed control checks device presence independently of
    # Pi's sandbox and compute helper. A confined denial must still fail when
    # the host exposes a device, and compute failures are never skip reasons.
    try:
        compiled = subprocess.run(["/usr/bin/clang", "-fobjc-arc", "-O2", "-Wall", "-Wextra", "-Werror",
            "-framework", "Foundation", "-framework", "Metal", str(source), "-o", str(binary)],
            capture_output=True, timeout=30)
        if compiled.returncode != 0:
            pytest.fail("metal_device_control_compile_failed", pytrace=False)
        result = subprocess.run([str(binary)], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("metal_device_control_unavailable", pytrace=False)
    require_metal_device(result)


@pytest.fixture(scope="session")
def native_recipe_driver(tmp_path_factory):
    # Register once here so all modules reuse the same compiled binary while
    # retaining their own test-local state, homes, and input fixtures.
    if sys.platform != "darwin":
        pytest.skip("native AppKit acceptance requires macOS")
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path_factory.mktemp("native-recipe-build").resolve()
    inputs = [root / "Package.swift"] + sorted(path for folder in ("Sources", "native")
        for path in (root / folder).rglob("*") if path.is_file())
    def hashes():
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    before = hashes()
    child = subprocess.Popen(["/usr/bin/swift", "build", "--package-path", str(root),
        "--scratch-path", str(scratch), "-c", "release", "--product", "MeecpMenuBar"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, error = child.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail("native_recipe_compile_timeout", pytrace=False)
    if child.returncode != 0:
        pytest.fail("native_recipe_compile_failed", pytrace=False)
    binary = scratch / "arm64-apple-macosx" / "release" / "MeecpMenuBar"
    assert binary.is_file()
    assert hashes() == before
    receipt = {"schema_version": 1,
        "compiler_exit_code": child.returncode, "compiler_joined": True,
        "compiler_stdout_bytes": len(output), "compiler_stderr_bytes": len(error),
        "source_inputs_before_after": before, "binary_path": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    descriptor = os.open(scratch / "native-build-receipt.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(receipt, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    # Compiler output stays in memory and is never emitted, including on failure.
    del output, error
    return binary
