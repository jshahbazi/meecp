"""Static CLI questions from a closed packaged helper inventory.

An inventory supplies an immutable helper identity for descriptor construction.
It is never execution authority: neither the preparation runner nor installed
HostCommands receives an admission capability from this module.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import platform
import re

from .preparation_command import CommandFamily
from .preparation_contracts import PreparationError, canonical_bytes, load_contract
from .recipes import _read


def _platform_eligible() -> bool:
    return (platform.system() == "Darwin" and platform.machine() == "arm64"
            and platform.mac_ver()[0].split(".")[0] == "26")


def shipped_command_family(host_root: Path) -> CommandFamily | None:
    """Register static forms only when the fixed inventory matches its helper.

    Unbundled checkouts and missing, corrupt or broadened inventories omit the
    provider. Existing family availability is independent of this optional
    inventory. No compiler, source executable or signature tool is invoked.
    """
    if not _platform_eligible():
        return None
    expected = {
        "schema_version": 1, "package_schema": 2, "profile": "host-command",
        "component": "macos26-arm64-command-v3", "receipt_format": 3,
        "helper": "bin/mcpme-command-reap",
        "platform": {"os": "macos", "major": 26, "architecture": "arm64"},
        "production_admission": "disabled", "configuration_authority": "inventory-only",
        "open_gates": ["aggregate-process-memory-enforcement", "signed-relocated-native-app"],
    }
    hashes = {"helper_sha256", "helper_source_sha256"}
    try:
        value = load_contract(_read(host_root / "command-execution.json", 32 * 1024))
        if (type(value) is not dict or set(value) != set(expected) | hashes
                or any(type(value[key]) is not type(item) or canonical_bytes(value[key]) != canonical_bytes(item)
                       for key, item in expected.items())
                or any(type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
                       for key in hashes)):
            return None
        helper = host_root / expected["helper"]
        if not helper.stat().st_mode & 0o111:
            return None
        digest = hashlib.sha256(_read(helper, 16 * 1024**2)).hexdigest()
        if digest != value["helper_sha256"]:
            return None
        return CommandFamily(digest)
    except (OSError, ValueError, PreparationError):
        return None
