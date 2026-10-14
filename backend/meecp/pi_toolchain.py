"""Select and provision Apple's developer tools without changing global selection.

Only fixed, host-owned inspection/acquisition commands execute outside the
workspace sandbox. Their output is transient and becomes allowlisted facts.
Selected build commands stay inside the ordinary workspace sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .processes import _await_ownership, _watch_ownership

APPLE_COMPONENT_DOC = (
    "https://developer.apple.com/documentation/xcode/downloading-and-installing-additional-xcode-components"
)
_CLT = Path("/Library/Developer/CommandLineTools")
_METAL_SOURCE = (
    "#include <metal_stdlib>\nusing namespace metal;\nkernel void probe(device uint *v [[buffer(0)]]) { v[0] = 1; }\n"
)
_VERSION = re.compile(r"^Xcode ([0-9]+(?:\.[0-9]+){0,2})\nBuild version ([A-Za-z0-9]+)\s*$")
_MAX_HOST_OUTPUT = 256 * 1024


def _error(code):
    from .pi_tools import PiToolError

    return PiToolError(code)


def _trusted_path(value, *, directory=False):
    """Resolve Apple's links, then pin only a non-writable owned file tree."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or ".." in Path(value).parts
    ):
        raise _error("developer_tools_untrusted")
    try:
        path = Path(value).resolve(strict=True)
        for ancestor in (path, *path.parents):
            details = ancestor.stat(follow_symlinks=False)
            # macOS ships these exact root-owned containers with group write.
            # Pi's sandbox cannot write there. Developer/asset roots themselves
            # still require immutable ownership and no group/world write.
            standard_group = {Path("/Applications"): 80, Path("/private/var/run"): 1}.get(ancestor)
            system_container = (
                standard_group is not None
                and ancestor != path
                and details.st_uid == 0
                and details.st_gid == standard_group
                and stat.S_IMODE(details.st_mode) == 0o775
            )
            if details.st_uid not in (0, os.getuid()) or details.st_mode & 0o022 and not system_container:
                raise ValueError()
        details = path.stat(follow_symlinks=False)
        if (
            directory
            and not stat.S_ISDIR(details.st_mode)
            or not directory
            and (not stat.S_ISREG(details.st_mode) or not details.st_mode & 0o111)
        ):
            raise ValueError()
        return path
    except (OSError, ValueError):
        raise _error("developer_tools_untrusted") from None


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class _Directory:
    path: Path
    identity: tuple

    @classmethod
    def pin(cls, path):
        path = _trusted_path(str(path), directory=True)
        details = path.stat()
        return cls(path, (details.st_dev, details.st_ino, details.st_uid, stat.S_IMODE(details.st_mode)))

    def validate(self):
        current = _Directory.pin(self.path)
        if current != self:
            raise _error("developer_tools_changed")


async def owned_host_command(broker, argv, environment, *, timeout=15):
    """Fixed system-tool execution with owned descendants and bounded capture."""
    process = None
    try:
        async with asyncio.timeout(timeout):
            try:
                process = await broker.owner.launch(
                    Path(argv[0]),
                    argv[1:],
                    broker.workspace,
                    environment,
                    owned_group=True,
                    expected_reaper=broker.owner.reaper,
                    startup_timeout=min(timeout, 10),
                    stop_grace=0.2,
                    admit=broker._launch_admitted,
                )
                assert process.stdin is not None and process.stdout is not None
                process.stdin.close()
                await process.stdin.wait_closed()

                async def consume():
                    data = bytearray()
                    while chunk := await process.stdout.read(65536):
                        if len(data) + len(chunk) > _MAX_HOST_OUTPUT:
                            raise _error("developer_tools_output_limit")
                        data.extend(chunk)
                    return await process.wait(), bytes(data)

                return await _watch_ownership(process, consume())
            finally:
                if process is not None:
                    await _await_ownership(process.stop())
    except TimeoutError:
        raise _error("developer_tools_timeout") from None


class ToolchainManager:
    def __init__(self, broker, *, runner=None, candidates=None):
        self.broker = broker
        self._runner = runner or (lambda argv, env, timeout=15: owned_host_command(broker, argv, env, timeout=timeout))
        self._candidates = candidates
        self._lock = asyncio.Lock()
        self._selected = None
        self._reads = ()
        self.snapshot = {"status": "unchecked", "available": False, "required": False}

    @property
    def read_roots(self):
        self.validate()
        return tuple(root.path for root in self._reads)

    def validate(self):
        for root in self._reads:
            root.validate()

    def _environment(self, developer=None, *, acquisition=False):
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.broker.home),
            "TMPDIR": str(self.broker.temporary),
            "LANG": "C",
            "LC_ALL": "C",
        }
        if developer is not None:
            environment["DEVELOPER_DIR"] = str(developer)
        if acquisition:
            # Apple's asset service installs for this login and selected Xcode.
            # Never inherit agent environment, credentials, or a proxy setting.
            environment["HOME"] = str(Path.home())
        return environment

    async def _run(self, argv, developer=None, *, acquisition=False, timeout=15):
        return await self._runner(argv, self._environment(developer, acquisition=acquisition), timeout=timeout)

    async def _find(self, name, developer):
        code, data = await self._run(["/usr/bin/xcrun", "--find", name], developer)
        if code != 0:
            return None
        try:
            return _trusted_path(data.decode("utf-8").strip())
        except (UnicodeError, ValueError):
            return None

    async def _developer_candidates(self):
        if self._candidates is not None:
            choices = list(self._candidates)
        else:
            code, data = await self._run(["/usr/bin/xcode-select", "--print-path"])
            choices = []
            if code == 0:
                try:
                    choices.append(Path(data.decode("utf-8").strip()))
                except UnicodeError:
                    pass
            # Read public application metadata only; do not launch applications.
            for root in (Path("/Applications"), Path.home() / "Applications"):
                if root.is_dir():
                    choices.extend(
                        path / "Contents/Developer"
                        for path in sorted(root.glob("*.app"))
                        if (path / "Contents/Developer/usr/bin/xcodebuild").is_file()
                    )
            choices.append(_CLT)
        accepted = []
        for candidate in choices[:16]:
            try:
                path = _trusted_path(str(candidate), directory=True)
            except ValueError:
                continue
            if path.is_relative_to(self.broker.workspace.resolve()):
                # The agent may populate its workspace with a fake developer
                # directory. It never selects code for the host control plane.
                continue
            if path not in accepted:
                accepted.append(path)
        return accepted

    async def _inspect(self, developer):
        full_xcode = (developer / "usr/bin/xcodebuild").is_file()
        signature_target = developer / "usr/bin/xcodebuild" if full_xcode else developer / "usr/bin/clang"
        code, _ = await self._run(
            ["/usr/bin/codesign", "--verify", "--strict", "-R", "=anchor apple", str(signature_target)], timeout=60
        )
        if code != 0:
            return None
        clang = await self._find("clang", developer)
        sdk = None
        code, data = await self._run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], developer)
        if code == 0:
            try:
                sdk = _trusted_path(data.decode("utf-8").strip(), directory=True)
            except (UnicodeError, ValueError):
                pass
        version, build = None, None
        if full_xcode:
            code, data = await self._run(["/usr/bin/xcodebuild", "-version"], developer)
            if code == 0:
                match = _VERSION.fullmatch(data.decode("utf-8", "replace").strip())
                if match:
                    version, build = match.groups()
        first_launch_ready = clang is not None and sdk is not None and (not full_xcode or version is not None)
        if not full_xcode and not first_launch_ready:
            return None
        # A signed installed Xcode can still require its native first-launch or
        # license flow. Keep its trusted identity so that installing a second
        # copy is never suggested as the remedy for missing clang/SDK lookup.
        metal = await self._find("metal", developer) if first_launch_ready else None
        roots = [developer, *([sdk] if sdk is not None else [])]
        for binary in (clang, metal):
            if binary is not None:
                roots.extend(path for path in binary.parents if path.name.endswith(".xctoolchain"))
                # The installed Metal compiler reads support files beside its
                # .xctoolchain within the exact mounted Apple asset bundle.
                # Do not grant the mount parent or unrelated cryptex assets.
                roots.extend(
                    path
                    for path in binary.parents
                    if path.parent == Path("/private/var/run/com.apple.security.cryptexd/mnt")
                    and path.name.startswith("com.apple.MobileAsset.MetalToolchain-")
                )
        return {
            "developer": developer,
            "clang": clang,
            "sdk": sdk,
            "metal": metal,
            "full_xcode": full_xcode,
            "first_launch_ready": first_launch_ready,
            "apple_signature_verified": True,
            "xcode_version": version,
            "xcode_build": build,
            "roots": tuple(dict.fromkeys(roots)),
        }

    def _select(self, selected):
        self._reads = tuple(_Directory.pin(path) for path in selected["roots"])
        self._selected = selected
        self.broker.env["DEVELOPER_DIR"] = str(selected["developer"])
        if selected["sdk"] is not None:
            self.broker.env["SDKROOT"] = str(selected["sdk"])
        else:
            self.broker.env.pop("SDKROOT", None)

    async def _compiler_usable(self, selected):
        if not selected["first_launch_ready"] or selected["metal"] is None:
            return False
        self._select(selected)
        # xcrun finding a shim is insufficient. Compile a fixed tiny shader
        # inside the same filesystem confinement that selected builds use.
        self.broker._write(".toolchain/probe.metal", _METAL_SOURCE)
        source = self.broker.workspace / ".toolchain/probe.metal"
        target = self.broker.workspace / ".toolchain/probe.air"
        try:
            result = await self.broker._run(
                {
                    "argv": [str(selected["metal"]), "-c", str(source), "-o", str(target)],
                    "timeout_seconds": 30,
                    "credentials": {},
                }
            )
            if not result["success"]:
                return False
            details = target.stat(follow_symlinks=False)
            return stat.S_ISREG(details.st_mode) and 0 < details.st_size <= 1024 * 1024
        finally:
            # These are fixed public synthetic bytes, never selected model data.
            for path in (source, target):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    async def _facts(self, selected, *, available, required, status=None, acquired=False, action=None):
        value = {
            "status": status or ("ready" if available else "missing"),
            "available": available,
            "required": required,
            "acquired": acquired,
            "documentation": APPLE_COMPONENT_DOC,
        }
        if selected is not None:
            value.update(
                developer_dir=str(selected["developer"]),
                full_xcode=selected["full_xcode"],
                first_launch_ready=selected["first_launch_ready"],
                acquisition_ready=selected["full_xcode"] and selected["first_launch_ready"],
                apple_signature_verified=selected["apple_signature_verified"],
                xcode_version=selected["xcode_version"],
                xcode_build=selected["xcode_build"],
            )
            if selected["sdk"] is not None:
                value["sdk_path"] = str(selected["sdk"])
            if selected["clang"] is not None:
                value.update(
                    clang_path=str(selected["clang"]), clang_sha256=await asyncio.to_thread(_digest, selected["clang"])
                )
            if selected["full_xcode"]:
                value["xcodebuild_sha256"] = await asyncio.to_thread(
                    _digest, selected["developer"] / "usr/bin/xcodebuild"
                )
            if selected["metal"] is not None:
                value.update(
                    metal_path=str(selected["metal"]), metal_sha256=await asyncio.to_thread(_digest, selected["metal"])
                )
            readiness = {
                key: item
                for key, item in value.items()
                if key not in {"status", "available", "required", "acquired", "documentation"}
            }
            readiness["root_identities"] = [(_Directory.pin(root).identity) for root in selected["roots"]]
            value["readiness_fingerprint"] = hashlib.sha256(
                json.dumps(readiness, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        if action:
            value["action_required"] = action
            value["continuation"] = "ensure_metal_toolchain"
        self.snapshot = value
        return dict(value)

    async def ensure(self, args):
        self.broker._arguments(args, (), ("required",))
        required = args.get("required", True)
        if type(required) is not bool:
            raise _error("invalid_arguments")
        async with self._lock:
            choices = await self._developer_candidates()
            selected = None
            acquisition = None
            installed_xcode = None
            for developer in choices:
                candidate = await self._inspect(developer)
                if candidate is None:
                    continue
                selected = selected or candidate
                if candidate["full_xcode"]:
                    installed_xcode = installed_xcode or candidate
                    if candidate["first_launch_ready"] and acquisition is None:
                        acquisition = candidate
                if await self._compiler_usable(candidate):
                    self._select(candidate)
                    return await self._facts(candidate, available=True, required=required)
            selected = acquisition or installed_xcode or selected
            if selected is not None:
                self._select(selected)
            open_xcode = (
                {
                    "code": "xcode_components_or_license_required",
                    "kind": "open_xcode",
                    "application_path": str(selected["developer"].parent.parent),
                }
                if selected is not None and selected["full_xcode"]
                else None
            )
            if not required:
                return await self._facts(
                    selected,
                    available=False,
                    required=False,
                    action=open_xcode if selected is not None and not selected["first_launch_ready"] else None,
                )
            if acquisition is None:
                return await self._facts(
                    selected,
                    available=False,
                    required=True,
                    status="action_required",
                    action=open_xcode
                    or {
                        "code": "full_xcode_required",
                        "kind": "install_xcode",
                        "url": "https://developer.apple.com/xcode/",
                    },
                )
            # Fixed, documented Apple command. Never select globally or accept a
            # license on the user's behalf. Failure supports a fresh recheck.
            code, _ = await self._run(
                ["/usr/bin/xcodebuild", "-downloadComponent", "MetalToolchain"],
                acquisition["developer"],
                acquisition=True,
                timeout=1800,
            )
            refreshed = await self._inspect(acquisition["developer"])
            if refreshed is not None and await self._compiler_usable(refreshed):
                self._select(refreshed)
                return await self._facts(refreshed, available=True, required=True, acquired=code == 0)
            acquisition = refreshed or acquisition
            self._select(acquisition)
            return await self._facts(
                acquisition,
                available=False,
                required=True,
                status="action_required",
                action={
                    "code": "xcode_components_or_license_required",
                    "kind": "open_xcode",
                    "application_path": str(acquisition["developer"].parent.parent),
                },
            )
