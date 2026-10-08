"""Static, bounded native command compatibility inspection; never executes input.

The initial component admits thin arm64 macOS executables using only system
dynamic libraries. Additional native runtimes require a separate closure policy.
Load-command constants/layouts follow Apple's mach-o/loader.h in the macOS SDK.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import struct

from .preparation_contracts import PreparationError

MAX_EXECUTABLE_BYTES = 16 * 1024**2
MAX_LOAD_COMMANDS = 128
MAX_LOAD_COMMAND_BYTES = 65536
_DYLIB = {0xC, 0x80000018, 0x8000001F, 0x80000023}
_PASSIVE = {0x2, 0xB, 0x19, 0x1B, 0x1D, 0x22, 0x80000022,
            0x26, 0x29, 0x2A, 0x80000028, 0x80000033, 0x80000034}


def _reject(code="unsupported_command_runtime"):
    raise PreparationError(code)


def _name(command: bytes, minimum: int) -> str:
    if len(command) < minimum:
        _reject()
    offset = struct.unpack_from("<I", command, 8)[0]
    if not minimum <= offset < len(command):
        _reject()
    end = command.find(b"\0", offset)
    if end < 0 or not 1 <= end - offset <= 1024:
        _reject()
    try:
        name = command[offset:end].decode("ascii")
    except UnicodeError:
        _reject()
    path = PurePosixPath(name)
    if (not path.is_absolute() or ".." in path.parts or str(path) != name
            or any(ord(character) < 32 for character in name)):
        _reject()
    return name


def inspect_native_executable(data: bytes) -> dict:
    """Return content-free compatibility facts for an exact, bounded byte string.

    This validates the supported loader contract, not program behavior. Schemas,
    effects, synthetic fixtures and owned execution need independent admission.
    """
    if type(data) is not bytes or not 32 <= len(data) <= MAX_EXECUTABLE_BYTES:
        _reject("command_executable_size_limit")
    magic, cpu, subtype, kind, count, total, flags, reserved = struct.unpack_from("<8I", data)
    if (magic != 0xFEEDFACF or cpu != 0x0100000C or subtype != 0 or kind != 2
            or reserved != 0 or not 1 <= count <= MAX_LOAD_COMMANDS
            or not count * 8 <= total <= MAX_LOAD_COMMAND_BYTES or 32 + total > len(data)):
        _reject()
    offset, libraries, loader, minimum = 32, [], None, None
    for _ in range(count):
        if offset + 8 > 32 + total:
            _reject()
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 8 or offset + size > 32 + total:
            _reject()
        value = data[offset:offset + size]
        if command in _DYLIB:
            name = _name(value, 24)
            if (not name.startswith(("/usr/lib/", "/System/Library/Frameworks/")) or name in libraries):
                _reject("command_external_runtime_unsupported")
            libraries.append(name)
        elif command == 0xE:  # LC_LOAD_DYLINKER
            if loader is not None or _name(value, 12) != "/usr/lib/dyld":
                _reject("command_external_runtime_unsupported")
            loader = "/usr/lib/dyld"
        elif command == 0x32:  # LC_BUILD_VERSION
            if size < 24 or minimum is not None:
                _reject()
            platform_id, version, sdk, tools = struct.unpack_from("<4I", value, 8)
            if platform_id != 1 or tools > 8 or size != 24 + tools * 8:
                _reject()
            minimum = version
        elif command == 0x24:  # LC_VERSION_MIN_MACOSX
            if size != 16 or minimum is not None:
                _reject()
            minimum = struct.unpack_from("<I", value, 8)[0]
        elif command not in _PASSIVE:
            # Includes RPATH, dyld environment, lazy external loaders, dylib IDs,
            # encrypted images and future commands whose authority is unknown.
            _reject("command_external_runtime_unsupported")
        offset += size
    if (offset != 32 + total or loader is None or minimum is None or
            not 0x000B0000 <= minimum <= 0x001A0000 or not flags & 0x4):
        _reject()
    return {"schema_version": 1, "format": "macho64", "architecture": "arm64", "os": "macos",
            "minimum_version": f"{minimum >> 16}.{(minimum >> 8) & 255}.{minimum & 255}",
            "loader": loader, "system_libraries": sorted(libraries),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
