"""Host-selected native preparation and content-free Metal runtime preflight."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

MAX_NATIVE_MEMORY_MB = 65536
NATIVE_FIELDS = frozenset(
    {"argv", "cwd", "network", "env", "credentials", "memory_mb", "timeout_seconds", "require_metal_compiler"}
)
PROBE_FIELDS = frozenset({"schema_version", "device_available", "residency_available", "compute_passed"})
PROBE_RELATIVE = "bin/meecp-metal-probe"


def validate_native(broker, args):
    from .pi_tools import PiToolError

    broker._arguments(args, ("argv", "memory_mb"), NATIVE_FIELDS - {"argv", "memory_mb"})
    broker._argv(args["argv"])
    if type(args["memory_mb"]) is not int or not 1 <= args["memory_mb"] <= MAX_NATIVE_MEMORY_MB:
        raise PiToolError("invalid_memory_limit")
    if type(args.get("timeout_seconds", 60)) is not int or not 1 <= args.get("timeout_seconds", 60) <= 1800:
        raise PiToolError("invalid_timeout")
    for field in ("network", "require_metal_compiler"):
        if type(args.get(field, False)) is not bool:
            raise PiToolError("invalid_arguments")
    # Validate paths and credentials before admission, without launching a process.
    broker._path(args.get("cwd", "."), write=True, directory=True)
    broker._command_environment(args)


def verified_probe(broker):
    """Bind the shipped helper to the app's frozen public build receipt."""
    from .pi_tools import PiToolError

    if broker.host_root is None:
        raise PiToolError("metal_probe_unavailable")
    root = Path(broker.host_root)
    try:
        receipt = json.loads(broker._read(str(root / "native-metal-probe.json")))
        helper = broker._path(str(root / PROBE_RELATIVE))
        details = helper.stat(follow_symlinks=False)
        if (
            type(receipt) is not dict
            or set(receipt) != {"schema_version", "helper", "helper_sha256", "helper_source_sha256"}
            or receipt["schema_version"] != 1
            or type(receipt["schema_version"]) is not int
            or receipt["helper"] != PROBE_RELATIVE
            or not stat.S_ISREG(details.st_mode)
            or not details.st_mode & 0o111
            or details.st_mode & 0o022
            or details.st_uid not in (0, os.getuid())
            or details.st_size > 16 * 1024**2
        ):
            raise ValueError()
        for key in ("helper_sha256", "helper_source_sha256"):
            if (
                not isinstance(receipt[key], str)
                or len(receipt[key]) != 64
                or any(char not in "0123456789abcdef" for char in receipt[key])
            ):
                raise ValueError()
        # The source is shipped to keep the runtime probe's provenance inspectable.
        source = broker._read(str(root / "native-metal-probe.m"))
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != receipt["helper_source_sha256"]:
            raise ValueError()
        fd = os.open(helper, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            digest = hashlib.sha256()
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError()
        finally:
            os.close(fd)
        if digest.hexdigest() != receipt["helper_sha256"]:
            raise ValueError()
        return helper, receipt
    except (OSError, ValueError, TypeError, KeyError):
        raise PiToolError("metal_probe_unavailable") from None


def parse_probe(result):
    """Accept only the fixed helper protocol; never return raw process output."""
    from .pi_tools import PiToolError

    try:
        envelope = json.loads(result["output"])
        value = json.loads(envelope["stdout"])
        if (
            set(value) != PROBE_FIELDS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or any(type(value[field]) is not bool for field in PROBE_FIELDS - {"schema_version"})
            or type(envelope["returncode"]) is not int
            or envelope["returncode"] not in (0, 1)
            or result["success"] is not (envelope["returncode"] == 0)
            or value["compute_passed"] is not (envelope["returncode"] == 0)
            or value["compute_passed"]
            and not (value["device_available"] and value["residency_available"])
            or value["residency_available"]
            and not value["device_available"]
        ):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise PiToolError("metal_probe_invalid") from None


async def preflight(broker):
    from .pi_tools import PiToolError

    compiler = await broker.toolchain_manager.ensure({"required": False})
    runtime = {"status": "unavailable", "device_available": None, "residency_available": None, "compute_passed": None}
    if broker.native_runner is not None:
        try:
            helper, receipt = verified_probe(broker)
            result = await broker.native_runner(
                {
                    "argv": [str(helper)],
                    "memory_mb": 256,
                    "timeout_seconds": 30,
                    "network": False,
                    "require_metal_compiler": False,
                }
            )
            probe = parse_probe(result)
            runtime.update({key: value for key, value in probe.items() if key != "schema_version"})
            runtime.update(
                status="ready" if probe["compute_passed"] else "failed",
                helper_sha256=receipt["helper_sha256"],
                helper_source_sha256=receipt["helper_source_sha256"],
            )
        except PiToolError as error:
            runtime["status"] = error.code
    return {
        "schema_version": 1,
        "metal_compiler": compiler,
        "metal_runtime": runtime,
        "native_command": {
            "available": broker.native_runner is not None,
            "gpu_profile": "metal-agx-iosurface-compiler-service-v1",
            "exclusive_admission": "host",
            "max_memory_mb": MAX_NATIVE_MEMORY_MB,
            "max_timeout_seconds": 1800,
        },
    }
