"""Exact bundled MCP SDK overlay for ordinary Python applications.

The original application project and lock are preserved. Only the installed
host's manifest-verified MCP dependency closure is added; no resolution, source
execution or unreviewed network access occurs here.
"""
from __future__ import annotations

import csv
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .preparation_builder import MCP_VERSION, _copy, _digest
from .preparation_contracts import PreparationError
from .recipes import _read

MAX_METADATA = 1024 * 1024
LICENSE_NAME = re.compile(r"(?i)^(?:licen[cs]e|copying|notice|thirdpartynotices)(?:[._-]|$)")


def _records(distribution):
    data = _read(distribution / "RECORD", 4 * MAX_METADATA).decode("utf-8")
    result = []
    for row in csv.reader(io.StringIO(data)):
        if len(row) != 3:
            raise PreparationError("agent_sdk_closure_invalid")
        path = PurePosixPath(row[0])
        # Wheel console entrypoints are not imported by the MCP wrapper.
        if path.is_absolute() or ".." in path.parts or "\\" in row[0]:
            continue
        if str(path) != row[0] or len(row[0]) > 512:
            raise PreparationError("agent_sdk_closure_invalid")
        result.append(row[0])
    if not result or len(result) > 20000:
        raise PreparationError("agent_sdk_closure_invalid")
    return result


def _distributions(site):
    result = {}
    for path in sorted(site.glob("*.dist-info")):
        value = BytesParser().parsebytes(_read(path / "METADATA", MAX_METADATA))
        name, version = value.get("Name"), value.get("Version")
        if not name or not version or len(name + version) > 512:
            raise PreparationError("agent_sdk_closure_invalid")
        key = canonicalize_name(name)
        if key in result:
            raise PreparationError("agent_sdk_dependency_conflict")
        result[key] = (path, value)
    if len(result) > 512:
        raise PreparationError("agent_sdk_closure_invalid")
    return result


def _closure(distributions):
    pending = [Requirement("mcp==" + MCP_VERSION)]
    seen, selected = set(), {}
    environment = default_environment()
    environment.update(python_version="3.12", python_full_version="3.12.13", extra="")
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        if name not in distributions:
            raise PreparationError("agent_sdk_closure_invalid")
        path, metadata = distributions[name]
        if metadata["Version"] not in requirement.specifier or requirement.url is not None:
            raise PreparationError("agent_sdk_dependency_conflict")
        identity = (name, tuple(sorted(requirement.extras)))
        if identity in seen:
            continue
        seen.add(identity); selected[name] = (path, metadata)
        for raw in metadata.get_all("Requires-Dist", []):
            dependency = Requirement(raw)
            if dependency.marker is None or any(dependency.marker.evaluate({**environment, "extra": extra})
                    for extra in ("", *requirement.extras)):
                pending.append(dependency)
        if len(seen) > 512:
            raise PreparationError("agent_sdk_closure_invalid")
    return selected


def complete_agent_runtime(host_root, destination, source_root, check):
    """Called after the normal locked wheel install and before notice checks."""
    check()
    host_root, destination, source_root = map(Path, (host_root, destination, source_root))
    host_site, site = host_root / "python/site-packages", destination / "python/site-packages"
    build_bytes = _read(host_root / "build.json", 8 * MAX_METADATA)
    build = json.loads(build_bytes)
    artifacts = build.get("artifact_sha256")
    if not isinstance(artifacts, dict):
        raise PreparationError("agent_sdk_closure_invalid")
    available, installed = _distributions(host_site), _distributions(site)
    existing_sdk = installed.get("mcp")
    if existing_sdk is not None and existing_sdk[1]["Version"] != MCP_VERSION:
        raise PreparationError("unsupported_mcp_sdk_version")
    # A project which already locks the supported SDK keeps its own complete
    # wheel closure. The overlay is only needed by ordinary Python apps.
    selected = {} if existing_sdk is not None else _closure(available)
    copied, distributions = [], []
    total = 0
    for name, (distribution, metadata) in sorted(selected.items()):
        check()
        current = installed.get(name)
        if current is not None and current[1]["Version"] != metadata["Version"]:
            raise PreparationError("agent_sdk_dependency_conflict")
        distributions.append({"name": name, "version": metadata["Version"]})
        for relative in _records(distribution):
            check()
            source = host_site / relative
            expected = artifacts.get("python/site-packages/" + relative)
            # RECORD may name bytecode excluded from the immutable app bundle.
            if relative.endswith(".pyc") and not source.exists():
                continue
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PreparationError("agent_sdk_closure_invalid")
            if _digest(source, check) != expected:
                raise PreparationError("agent_sdk_closure_invalid")
            target = site / relative
            if target.exists():
                # Same-version installed wheels retain their own metadata and
                # RECORD layout. Their frozen wheel identity is checked by the
                # ordinary builder; never overwrite an application dependency.
                if current is not None:
                    continue
                if _digest(target, check) != expected:
                    raise PreparationError("agent_sdk_dependency_conflict")
                continue
            if current is not None:
                continue
            total += source.stat().st_size
            if total > 128 * 1024**2 or len(copied) >= 20000:
                raise PreparationError("agent_sdk_closure_invalid")
            _copy(source, target, check)
            if _digest(target, check) != expected:
                raise PreparationError("agent_sdk_closure_invalid")
            copied.append({"path": "python/site-packages/" + relative, "sha256": expected})
    if _read(host_root / "build.json", 8 * MAX_METADATA) != build_bytes:
        raise PreparationError("agent_sdk_closure_invalid")
    notices = []
    for name, (distribution, metadata) in sorted(_distributions(site).items()):
        check()
        declared = metadata.get_all("License-File", [])
        existing = [path for path in distribution.rglob("*") if path.is_file() and LICENSE_NAME.match(path.name)]
        if declared or existing:
            continue
        # Some frozen wheels put their actual texts in the import package.
        wheel_notices = [site / path for path in _records(distribution)
            if LICENSE_NAME.match(PurePosixPath(path).name) and (site / path).is_file()]
        source_notice = source_root / ("LICENSE-" + name.upper())
        candidates = [(path, "wheel") for path in wheel_notices]
        if source_notice.is_file():
            candidates.append((source_notice, "original-source"))
        for index, (path, origin) in enumerate(candidates):
            data = _read(path, 4 * MAX_METADATA)
            if not data:
                continue
            target = distribution / ("LICENSE-mcpme-supplement-" + str(index) + ".txt")
            if target.exists():
                raise PreparationError("agent_sdk_closure_invalid")
            _copy(path, target, check)
            notices.append({"distribution": name, "origin": origin, "source": path.relative_to(site if origin == "wheel" else source_root).as_posix(),
                "path": target.relative_to(destination).as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
    origin = {"schema_version": 1, "host_build_sha256": hashlib.sha256(build_bytes).hexdigest(),
        "mcp_version": MCP_VERSION, "sdk_source": "original-lock" if existing_sdk is not None else "bundled-host",
        "distributions": distributions, "files": copied, "supplemented_notices": notices}
    data = json.dumps(origin, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > 4 * MAX_METADATA:
        raise PreparationError("agent_sdk_closure_invalid")
    (destination / "host-sdk-origin.json").write_bytes(data)
    check()
    return origin
