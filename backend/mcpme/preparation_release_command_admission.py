"""Source-approved signed command admission; inventories remain inert.

This gate supplies the existing installed and measured-preparation capabilities.
It changes no command component, receipt or same-user resource guarantee.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess

from .command_execution_v2 import CommandExecutionError as InstalledCommandError, _platform_policy as installed_platform_policy
from .command_resource_execution_v1 import CommandExecutionError as ResourceCommandError, _platform_policy as resource_platform_policy
from .command_resource_policy_v1 import ResourcePolicyV1
from .host_command import HostCommandAdmission, HostCommands
from .preparation_builder import _opened
from .preparation_command_owner import CommandPreparationResourceAdmission
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest, load_contract
from .preparation_release_artifact import CERTIFICATE_SHA1, TEAM_ID, ReleaseArtifactProof, _identity_matches, load_artifact_json
from .processes import _await_ownership
from .recipes import _read


COMMAND_COMPONENT = ("host-command-json-v1", "1.0.1",
    "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607")
# Accepted product/kernel/build/architecture matrix, never the observed digest.
# macOS 26.7.1; Darwin 25.6.0; xnu-12377.161.15.700.19~2/RELEASE_ARM64_T6050.
INSTALLED_PLATFORM_POLICY = "3898759c85d08268db02963c0e822e2bd3c9657d337e1e5c5708e0bb0738c83b"
RESOURCE_PLATFORM_POLICY = "03a5e27b4931b1eaa705f0a4be373bc202ac7dd3bb4d71a438c2eb6ffeae1b8a"
HELPER_SOURCE_SHA256 = "b99f87541598427df51a39486d384a5855813ea3decac55a1d091cafb5e17933"
RESOURCE_SOURCE_SHA256 = "09b455941c0a2890e2bdbd2ca91b6d8e47538a4e5f1a4115e7885412df89c400"
RESOURCE_HEADER_SHA256 = "a30d8a2acbdc15d704ed3d2480cad749d78f2647a98171c55b930ae0a9072c55"
COMPONENT_FILES = ("preparation_command.py", "preparation_command_source.py", "preparation_command_forms.py",
    "preparation_command_owner.py", "preparation_command_verifier.py", "preparation_application_source.py",
    "command_execution_v2.py", "command_resource_execution_v1.py", "command_resource_policy_v1.py", "host_command.py")
MAX_FILES = 16384
MAX_BYTES = 2 * 1024**3
POLICY_PATH = "templates/release-policies/phase3-v1.json"


def _deny():
    raise PreparationError("command_release_not_admitted")


@dataclass(frozen=True)
class CommandReleasePolicyV1:
    """Closed trusted release record; source/API/config cannot select authority."""

    component: tuple[str, str, str]
    installed_platform_policy: str
    resource_platform_policy: str

    def __post_init__(self):
        if (type(self.component) is not tuple or len(self.component) != 3
                or any(type(part) is not str for part in self.component) or self.component != COMMAND_COMPONENT
                or type(self.installed_platform_policy) is not str
                or self.installed_platform_policy != INSTALLED_PLATFORM_POLICY
                or type(self.resource_platform_policy) is not str
                or self.resource_platform_policy != RESOURCE_PLATFORM_POLICY):
            _deny()


def _identity(path, *, full=True):
    value = path.lstat()
    fields = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
    return fields + ((value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if full else ())


def _members(root):
    files, directories, total = {}, {root}, 0
    for path in root.rglob("*"):
        value = path.lstat()
        if value.st_uid != os.getuid() or value.st_mode & 0o6022:
            _deny()
        if stat.S_ISDIR(value.st_mode):
            directories.add(path)
        elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
            total += value.st_size
            files[path.relative_to(root).as_posix()] = path
        else:
            _deny()
        if len(files) > MAX_FILES or len(directories) > MAX_FILES + 4096 or total > MAX_BYTES:
            _deny()
    return files, directories


def _hash(path):
    with _opened(path) as stream:
        value = os.fstat(stream.fileno())
        if value.st_uid != os.getuid() or value.st_mode & 0o6022:
            _deny()
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fixed(value, expected, extra=()):
    if (type(value) is not dict or set(value) != set(expected) | set(extra)
            or any(type(value[key]) is not type(item) or canonical_bytes(value[key]) != canonical_bytes(item)
                for key, item in expected.items())
            or any(type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None for key in extra)):
        _deny()


def _signature(path, *, deep=False):
    requirement = ('=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        ' and certificate leaf = H"' + CERTIFICATE_SHA1 + '"')
    verification = ["--verify", "--strict", *(["--deep"] if deep else []), "-R", requirement]
    results = [subprocess.run(["/usr/bin/codesign", *arguments, str(path)], capture_output=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        for arguments in (verification, ["--display", "--verbose=4"], ["--display", "--entitlements", ":-"])]
    if any(result.returncode != 0 for result in results):
        _deny()
    details = results[1].stderr.decode("utf-8", errors="replace")
    if ("(runtime)" not in details or not re.search(r"^Timestamp=.+$", details, re.M)
            or not re.search(r"^TeamIdentifier=" + re.escape(TEAM_ID) + r"$", details, re.M)
            or b"<key>" in results[2].stdout + results[2].stderr):
        _deny()


def _accepted_platform():
    try:
        observed = (installed_platform_policy(), resource_platform_policy())
    except (InstalledCommandError, ResourceCommandError) as error:
        if error.code == "command_platform_unsupported":
            return False
        raise
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in observed):
        _deny()
    return observed == (INSTALLED_PLATFORM_POLICY, RESOURCE_PLATFORM_POLICY)


def command_release_admission(policy: CommandReleasePolicyV1 | None, host_root: Path, *, artifact=None):
    """Return both exact capabilities, or no capability for a closed release.

    The caller derives host_root from its installed module location. This gate
    additionally requires that same module root in the enclosing signed app.
    All reads/signature inspections are bounded; no packaged executable runs.
    The release loader runs this synchronous operation off the event loop.
    """
    if policy is None:
        return None, None
    if type(policy) is not CommandReleasePolicyV1:
        _deny()
    policy.__post_init__()
    try:
        if type(artifact) is not ReleaseArtifactProof or artifact.policy != host_root / POLICY_PATH:
            _deny()
        artifact.check_current(full=True)
        if (not isinstance(host_root, Path) or not host_root.is_absolute() or host_root.resolve(strict=True) != host_root
                or host_root != Path(__file__).absolute().parents[2]
                or host_root.parts[-3:] != ("Contents", "Resources", "host")):
            _deny()
        app = host_root.parents[2]
        if not app.name.endswith(".app"):
            _deny()
        files, directories = _members(host_root)
        app_files = {app / "Contents/Info.plist", app / "Contents/Resources/app-build.json", app / "Contents/MacOS/mcpme"}
        all_files = set(files.values()) | app_files
        all_directories = directories | {parent for path in all_files for parent in path.parents}
        if not (all_files | all_directories) <= {record[0] for record in artifact.identities}:
            _deny()
        for path in all_files:
            value = path.lstat()
            if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                    or value.st_uid != os.getuid() or value.st_mode & 0o6022):
                _deny()
        for path in all_directories:
            if not stat.S_ISDIR(path.lstat().st_mode):
                _deny()
            if path == app or app in path.parents:
                value = path.lstat()
                if value.st_uid != os.getuid() or value.st_mode & 0o6022:
                    _deny()
        baseline = {path: _identity(path, full=path in all_files or path == app or app in path.parents)
            for path in all_files | all_directories}
        def current():
            artifact._check_app_root_metadata()
            if (host_root.resolve(strict=True) != host_root or any(
                    not _identity_matches(path, identity, path in all_files or path == app or app in path.parents,
                        app_root=app)
                    for path, identity in baseline.items())):
                _deny()
        build_data = _read(host_root / "build.json", 8 * 1024**2)
        build = load_artifact_json(build_data)
        if (type(build) is not dict or set(build) != {"host_version", "artifact_sha256", "source_commit",
                "source_tree_dirty", "python", "toolchain", "signing", "distribution"}
                or build["signing"] != "developer_id" or build["distribution"] != "bundled_native_app_host"
                or build["source_tree_dirty"] is not False or type(build["source_commit"]) is not str
                or re.fullmatch(r"[0-9a-f]{40}", build["source_commit"]) is None
                or any(type(build[key]) is not str or not 0 < len(build[key]) <= 512 for key in ("host_version", "python", "toolchain"))
                or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", build["host_version"]) is None):
            _deny()
        artifact_hashes = build["artifact_sha256"]
        if (type(artifact_hashes) is not dict or not 1 <= len(artifact_hashes) <= MAX_FILES
                or set(artifact_hashes) != set(files) - {"build.json"}
                or "backend/mcpme/preparation_release_command_admission.py" not in artifact_hashes):
            _deny()
        for name, expected in artifact_hashes.items():
            relative = PurePosixPath(name)
            if (type(name) is not str or relative.as_posix() != name or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts) or "\\" in name or "\0" in name
                    or type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None
                    or _hash(files[name]) != expected):
                _deny()
        info = plistlib.loads(_read(app / "Contents/Info.plist", 64 * 1024))
        receipt = load_contract(_read(app / "Contents/Resources/app-build.json", 64 * 1024))
        required = {"schema_version", "version", "distribution", "signing", "hardened_runtime", "secure_timestamp",
            "signed_native_files", "notarization", "source_commit", "source_tree_dirty", "native_input_sha256",
            "swift_source_sha256", "branding_source_sha256", "app_icon_sha256", "starter_packages", "host_receipt_sha256", "legacy_handoff"}
        if (type(info) is not dict or info.get("CFBundleIdentifier") != "com.jshahbazi.mcpme"
                or info.get("CFBundleExecutable") != "mcpme" or info.get("CFBundleShortVersionString") != build["host_version"]
                or type(receipt) is not dict or not required <= set(receipt)
                or set(receipt) - required - {"textgen_signed_profile"}
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["signing"] != "developer_id" or receipt["distribution"] != "developer_id_native_app"
                or receipt["hardened_runtime"] is not True or receipt["secure_timestamp"] is not True
                or receipt["source_tree_dirty"] is not False or receipt["source_commit"] != build["source_commit"]
                or receipt["version"] != build["host_version"]
                or receipt["host_receipt_sha256"] != hashlib.sha256(build_data).hexdigest()):
            _deny()
        if (type(receipt["signed_native_files"]) is not int or not 1 <= receipt["signed_native_files"] <= 4096
                or receipt["notarization"] != "handled_by_dmg_builder" or type(receipt["legacy_handoff"]) is not bool
                or type(receipt["starter_packages"]) is not list or not 1 <= len(receipt["starter_packages"]) <= 16
                or any(type(receipt[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", receipt[key]) is None for key in
                    ("native_input_sha256", "swift_source_sha256", "branding_source_sha256", "app_icon_sha256", "host_receipt_sha256"))):
            _deny()
        if "textgen_signed_profile" in receipt:
            _fixed(receipt["textgen_signed_profile"], {"profile": "textgen-signed-cpu-v1",
                "archive_sha256": "fabbfa2b2960a5ea0d6d54866f2ced4e806269b5b84eb6a133e41d273839c92e",
                "preserved_without_resigning": True})
        for starter in receipt["starter_packages"]:
            if (type(starter) is not dict or set(starter) != {"path", "package_id", "version", "package_digest", "lock_sha256"}
                    or any(type(starter[key]) is not str or not 0 < len(starter[key]) <= 512 for key in ("path", "package_id", "version"))
                    or not starter["path"].startswith("starter-packages/")
                    or PurePosixPath(starter["path"]).as_posix() != starter["path"]
                    or any(part in {"", ".", ".."} for part in PurePosixPath(starter["path"]).parts)
                    or "\\" in starter["path"] or "\0" in starter["path"]
                    or any(type(starter[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", starter[key]) is None
                        for key in ("package_digest", "lock_sha256"))):
                _deny()
        if not (app / "Contents/MacOS/mcpme").lstat().st_mode & 0o111:
            _deny()
        # The current complete component bytes must match the reviewed 1.0.1
        # implementation. New admission/loader modules are outside that closure.
        records = [{"path": name, "sha256": _hash(host_root / "backend/mcpme" / name)} for name in COMPONENT_FILES]
        if contract_digest("preparation-host-command-component-v1", records) != policy.component[2]:
            _deny()
        command = load_contract(_read(host_root / "command-execution.json", 32 * 1024))
        resource = load_contract(_read(host_root / "command-resource-execution.json", 32 * 1024))
        platform = {"os": "macos", "major": 26, "architecture": "arm64"}
        _fixed(command, {"schema_version": 1, "package_schema": 2, "profile": "host-command",
            "component": "macos26-arm64-command-v3", "receipt_format": 3, "helper": "bin/mcpme-command-reap",
            "helper_source_sha256": HELPER_SOURCE_SHA256, "platform": platform, "production_admission": "disabled",
            "configuration_authority": "inventory-only", "open_gates": ["aggregate-process-memory-enforcement", "signed-relocated-native-app"]},
            {"helper_sha256"})
        _fixed(resource, {"schema_version": 1, "component": "macos26-arm64-command-resource-v1", "receipt_format": 4,
            "helper": "bin/mcpme-command-resource-reap", "resource_policy": "held-group-windowed-usage-v1",
            "confinement_policy": "macos26-arm64-monitored-fork-exec-v2", "helper_source_sha256": RESOURCE_SOURCE_SHA256,
            "helper_header_sha256": RESOURCE_HEADER_SHA256, "platform": platform, "production_admission": "disabled",
            "configuration_authority": "inventory-only", "lifetime_coverage_complete": False, "hard_aggregate_enforced": False,
            "result_admissible": False, "open_gates": ["complete-lifetime-accounting", "aggregate-process-memory-enforcement",
                "release-platform-acceptance", "signed-relocated-native-app"]}, {"helper_sha256"})
        helpers = (host_root / "bin/mcpme-command-reap", host_root / "bin/mcpme-command-resource-reap")
        for helper, inventory in zip(helpers, (command, resource)):
            if (helper.relative_to(host_root).as_posix() not in artifact_hashes
                    or artifact_hashes[helper.relative_to(host_root).as_posix()] != inventory["helper_sha256"]
                    or not helper.lstat().st_mode & 0o111):
                _deny()
        current()
        _signature(app, deep=True)
        for helper in helpers:
            _signature(helper)
        current()
        platform_accepted = _accepted_platform()
        artifact.check_current(full=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError,
            InstalledCommandError, ResourceCommandError, PreparationError):
        _deny()
    # This distinct result is available only after every signed artifact check
    # and final held-proof fence. An integrity failure never becomes a partial
    # capability downgrade that the ordinary loader may continue past.
    if not platform_accepted:
        raise PreparationError("command_release_platform_unavailable")
    return (HostCommandAdmission(helpers[0], command["helper_sha256"]),
        CommandPreparationResourceAdmission(helpers[1], resource["helper_sha256"], policy.resource_platform_policy, ResourcePolicyV1()))


class ReleasedHostCommands(HostCommands):
    """Installed sessions retain the same release proof through native launch.

    Revocation denies new execution and result publication. Existing native
    handles, journals and reservations remain available for owned cleanup.
    """

    def __init__(self, host, admission, artifact):
        if (type(admission) is not HostCommandAdmission or type(artifact) is not ReleaseArtifactProof
                or type(admission.helper) is not type(Path())
                or artifact.policy != admission.helper.parents[1] / POLICY_PATH
                or admission.helper not in {record[0] for record in artifact.identities}):
            raise InstalledCommandError("host_command_not_admitted")
        self.artifact = artifact
        root = admission.helper.parents[1]
        required = {root / "bin/mcpme-command-reap", root / "bin/mcpme-command-resource-reap",
            root / "command-execution.json", root / "command-resource-execution.json"}
        required |= {root / "backend/mcpme" / name for name in (*COMPONENT_FILES,
            "preparation_release_command_admission.py", "preparation_release_artifact.py")}
        self._command_identities = tuple(record for record in artifact.identities if record[0] in required)
        if {record[0] for record in self._command_identities} != required:
            raise InstalledCommandError("host_command_not_admitted")
        if not self._release_current():
            raise InstalledCommandError("host_command_not_admitted")
        super().__init__(host, admission)

    def _release_current(self):
        try:
            self.artifact.check_current()
            for path, identity, complete in self._command_identities:
                if _identity(path, full=complete) != identity:
                    return False
            return True
        except (PreparationError, OSError, ValueError, TypeError):
            return False

    async def _full_current(self):
        task = asyncio.create_task(asyncio.to_thread(self.artifact.check_current, full=True))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _await_ownership(task)
            except Exception:
                pass
            raise
        except PreparationError:
            raise InstalledCommandError("command_authority_changed") from None

    def current(self, slot):
        return self._release_current() and super().current(slot)

    def admit(self, spec):
        return self._release_current() and super().admit(spec)

    def executor(self, slot_id):
        # Access to an existing executor remains available after revocation so
        # cancellation/recovery can join already owned native handles.
        if slot_id not in self.executors and not self._release_current():
            raise InstalledCommandError("command_authority_changed")
        executor = super().executor(slot_id)
        executor.before_native_create = self.admit
        return executor

    async def session(self, slot):
        await self._full_current()
        return await super().session(slot)

    async def invoke(self, session, name, arguments, request_id):
        await self._full_current()
        return await super().invoke(session, name, arguments, request_id)
