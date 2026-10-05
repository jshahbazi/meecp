"""Static command runtime closure checks do not execute selected programs."""
from __future__ import annotations

import hashlib
import platform
import struct
import subprocess

import pytest

from mcpme.preparation_command_source import inspect_native_executable
from mcpme.preparation_contracts import PreparationError


def command(kind, prefix, text):
    name = text.encode() + b"\0"
    length = (len(prefix) + 8 + len(name) + 7) // 8 * 8
    return struct.pack("<II", kind, length) + prefix + name + b"\0" * (length - len(prefix) - 8 - len(name))


def image(*extras):
    commands = [command(0xE, struct.pack("<I", 12), "/usr/lib/dyld"),
                command(0xC, struct.pack("<4I", 24, 0, 0, 0), "/usr/lib/libSystem.B.dylib"),
                struct.pack("<6I", 0x32, 24, 1, 0x001A0000, 0x001A0000, 0), *extras]
    body = b"".join(commands)
    return struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, len(commands), len(body), 4, 0) + body


def test_system_only_loader_contract_is_bound_to_exact_bytes():
    data = image()
    value = inspect_native_executable(data)
    assert value["architecture"] == "arm64" and value["minimum_version"] == "26.0.0"
    assert value["system_libraries"] == ["/usr/lib/libSystem.B.dylib"]
    assert value["sha256"] == hashlib.sha256(data).hexdigest() and value["size"] == len(data)


@pytest.mark.parametrize("kind,path,prefix", [
    (0xC, "/opt/homebrew/lib/libexternal.dylib", 24),
    (0x80000018, "@rpath/libexternal.dylib", 24),
    (0x80000023, "/usr/lib/../external.dylib", 24),
    (0x8000001F, "/tmp/extra.dylib", 24),
    (0x8000001C, "/usr/lib", 12),
    (0x27, "DYLD_INSERT_LIBRARIES=/tmp/extra", 12),
])
def test_unknown_or_external_runtime_authority_is_rejected(kind, path, prefix):
    extra = command(kind, struct.pack("<I", prefix) + b"\0" * (prefix - 12), path)
    with pytest.raises(PreparationError):
        inspect_native_executable(image(extra))


@pytest.mark.parametrize("field,value", [(0, 0xCAFEBABE), (1, 7), (2, 2), (3, 6),
    (4, 129), (5, 65537), (6, 0), (7, 1)])
def test_unsupported_or_unbounded_header_is_rejected(field, value):
    data = bytearray(image()); struct.pack_into("<I", data, field * 4, value)
    with pytest.raises(PreparationError):
        inspect_native_executable(bytes(data))


@pytest.mark.parametrize("change", ["truncated", "command_size", "duplicate_loader", "ios", "future_macos", "unknown_command"])
def test_load_command_inconsistency_is_rejected(change):
    data = bytearray(image())
    if change == "truncated":
        data.pop()
    elif change == "command_size":
        struct.pack_into("<I", data, 36, 9)
    elif change == "duplicate_loader":
        data = image(command(0xE, struct.pack("<I", 12), "/usr/lib/dyld"))
    elif change in {"ios", "future_macos"}:
        struct.pack_into("<I", data, len(data) - (16 if change == "ios" else 12), 2 if change == "ios" else 0x001B0000)
    else:
        data = image(struct.pack("<II", 0xFFFF, 8))
    with pytest.raises(PreparationError):
        inspect_native_executable(bytes(data))


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64", reason="native arm64 compiler")
def test_actual_newly_compiled_command_has_supported_static_closure(tmp_path):
    source = tmp_path / "command.c"
    source.write_text('#include <stdio.h>\nint main(void) { return puts("synthetic") < 0; }\n')
    executable = tmp_path / "command"
    subprocess.run(["/usr/bin/cc", "-mmacosx-version-min=26.0", str(source), "-o", str(executable)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    value = inspect_native_executable(executable.read_bytes())
    assert value["system_libraries"] == ["/usr/lib/libSystem.B.dylib"]
