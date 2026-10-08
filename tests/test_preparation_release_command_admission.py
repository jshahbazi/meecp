"""Closed release selection and signed-metadata identity controls; no execution."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
from types import SimpleNamespace
import asyncio
import threading

import pytest

from meecp import preparation_release_command_admission as release
from meecp.host_command import HostCommandAdmission
from meecp.preparation_command_owner import CommandPreparationResourceAdmission
from meecp.preparation_contracts import PreparationError, canonical_bytes, load_contract


ROOT = Path(__file__).resolve().parents[1]
SIGNATURE = release._signature


def policy(**changes):
    return release.CommandReleasePolicyV1(**dict(component=release.COMMAND_COMPONENT,
        installed_platform_policy=release.INSTALLED_PLATFORM_POLICY,
        resource_platform_policy=release.RESOURCE_PLATFORM_POLICY, **changes))


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    app = tmp_path.resolve() / "Synthetic metadata.app"
    host = app / "Contents/Resources/host"
    (host / "backend/meecp").mkdir(parents=True)
    (host / "bin").mkdir()
    (app / "Contents/MacOS").mkdir()
    (app / "Contents/_CodeSignature").mkdir()
    (app / "Contents/_CodeSignature/CodeResources").write_bytes(b"Synthetic seal; never signed.")
    policy_path = host / release.POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    policy_path.write_bytes(b"{}")
    native = app / "Contents/MacOS/meecp"
    native.write_bytes(b"Synthetic native metadata; never executed.")
    native.chmod(0o700)
    for name in (*release.COMPONENT_FILES, "preparation_release_command_admission.py", "preparation_release_artifact.py"):
        shutil.copyfile(ROOT / "backend/meecp" / name, host / "backend/meecp" / name)
    commands = []
    for name in ("mcpme-command-reap", "mcpme-command-resource-reap"):
        path = host / "bin" / name
        path.write_bytes(b"Synthetic helper metadata; never executed: " + name.encode())
        path.chmod(0o700)
        commands.append(path)
    platform = {"os": "macos", "major": 26, "architecture": "arm64"}
    installed = {"schema_version": 1, "package_schema": 2, "profile": "host-command",
        "component": "macos26-arm64-command-v3", "receipt_format": 3, "helper": "bin/mcpme-command-reap",
        "helper_sha256": hashlib.sha256(commands[0].read_bytes()).hexdigest(),
        "helper_source_sha256": release.HELPER_SOURCE_SHA256, "platform": platform,
        "production_admission": "disabled", "configuration_authority": "inventory-only",
        "open_gates": ["aggregate-process-memory-enforcement", "signed-relocated-native-app"]}
    resource = {"schema_version": 1, "component": "macos26-arm64-command-resource-v1", "receipt_format": 4,
        "helper": "bin/mcpme-command-resource-reap", "resource_policy": "held-group-windowed-usage-v1",
        "confinement_policy": "macos26-arm64-monitored-fork-exec-v2",
        "helper_sha256": hashlib.sha256(commands[1].read_bytes()).hexdigest(),
        "helper_source_sha256": release.RESOURCE_SOURCE_SHA256, "helper_header_sha256": release.RESOURCE_HEADER_SHA256,
        "platform": platform, "production_admission": "disabled", "configuration_authority": "inventory-only",
        "lifetime_coverage_complete": False, "hard_aggregate_enforced": False, "result_admissible": False,
        "open_gates": ["complete-lifetime-accounting", "aggregate-process-memory-enforcement",
            "release-platform-acceptance", "signed-relocated-native-app"]}
    for name, value in (("command-execution.json", installed), ("command-resource-execution.json", resource)):
        (host / name).write_bytes(canonical_bytes(value))
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.jshahbazi.mcpme",
        "CFBundleExecutable": "meecp", "CFBundleShortVersionString": "0.1.0"}))
    build = {"host_version": "0.1.0", "artifact_sha256": {}, "source_commit": "1" * 40,
        "source_tree_dirty": False, "python": "Synthetic private Python", "toolchain": "Synthetic compiler",
        "signing": "developer_id", "distribution": "bundled_native_app_host"}
    receipt = {"schema_version": 1, "version": "0.1.0", "distribution": "developer_id_native_app",
        "signing": "developer_id", "hardened_runtime": True, "secure_timestamp": True, "signed_native_files": 3,
        "notarization": "handled_by_dmg_builder", "source_commit": "1" * 40, "source_tree_dirty": False,
        "native_input_sha256": "a" * 64, "swift_source_sha256": "b" * 64, "branding_source_sha256": "c" * 64,
        "app_icon_sha256": "d" * 64, "starter_packages": [{"path": "starter-packages/synthetic",
            "package_id": "local.synthetic", "version": "1.0.0", "package_digest": "f" * 64, "lock_sha256": "a" * 64}],
        "host_receipt_sha256": "e" * 64,
        "legacy_handoff": False}
    def refresh():
        build["artifact_sha256"] = {p.relative_to(host).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in host.rglob("*") if p.is_file() and p.name != "build.json"}
        data = canonical_bytes(build)
        (host / "build.json").write_bytes(data)
        receipt["host_receipt_sha256"] = hashlib.sha256(data).hexdigest()
        (app / "Contents/Resources/app-build.json").write_bytes(canonical_bytes(receipt))
    refresh()
    monkeypatch.setattr(release, "__file__", str(host / "backend/meecp/preparation_release_command_admission.py"))
    monkeypatch.setattr(release, "installed_platform_policy", lambda: release.INSTALLED_PLATFORM_POLICY)
    monkeypatch.setattr(release, "resource_platform_policy", lambda: release.RESOURCE_PLATFORM_POLICY)
    checks = []
    monkeypatch.setattr(release, "_signature", lambda path, **options: checks.append((path, options)))
    def capture():
        files = {p for p in app.rglob("*") if not p.is_dir()}
        directories = {p for p in app.rglob("*") if p.is_dir()} | {p for path in files for p in path.parents}
        critical = {native, app / "Contents/Info.plist", app / "Contents/Resources/app-build.json",
            app / "Contents/_CodeSignature/CodeResources", host / "build.json", policy_path}
        critical |= {p for path in critical for p in path.parents}
        return release.ReleaseArtifactProof(tuple((p, release._identity(p, full=p in files or p == app or app in p.parents),
            p in files or p == app or app in p.parents) for p in files | directories), frozenset(critical),
            policy_path, policy_path.read_bytes(), hashlib.sha256((host / "build.json").read_bytes()).hexdigest())
    return SimpleNamespace(app=app, host=host, installed=installed, resource=resource, build=build,
        receipt=receipt, refresh=refresh, checks=checks, helpers=commands, capture=capture)


def admit(packaged, selected=None, host=None):
    return release.command_release_admission(policy() if selected is None else selected,
        packaged.host if host is None else host, artifact=packaged.capture())


def test_inventory_presence_cannot_grant_authority(packaged, monkeypatch):
    monkeypatch.setattr(release, "_members", lambda _: pytest.fail("closed release inspected an inventory"))
    assert release.command_release_admission(None, packaged.host) == (None, None)
    assert not packaged.checks


def test_exact_source_policy_constructs_both_capabilities_after_synthetic_signature_checks(packaged):
    installed, measured = admit(packaged)
    assert type(installed) is HostCommandAdmission and type(measured) is CommandPreparationResourceAdmission
    assert installed.helper == packaged.helpers[0] and installed.helper_sha256 == packaged.installed["helper_sha256"]
    assert measured.helper == packaged.helpers[1] and measured.helper_sha256 == packaged.resource["helper_sha256"]
    assert measured.platform_policy == release.RESOURCE_PLATFORM_POLICY and measured.policy == release.ResourcePolicyV1()
    assert packaged.checks == [(packaged.app, {"deep": True}), (packaged.helpers[0], {}), (packaged.helpers[1], {})]
    assert all(packaged.resource[field] is False for field in ("lifetime_coverage_complete", "hard_aggregate_enforced", "result_admissible"))
    assert packaged.installed["production_admission"] == packaged.resource["production_admission"] == "disabled"


def test_complete_build_inventory_larger_than_small_contract_limit_still_checks_every_member(packaged):
    # Actual signed hosts have build inventories over 512 KiB. All these inert
    # resource files must still be checked; no size-based signature-only shortcut.
    resources = packaged.host / "synthetic-resources"
    resources.mkdir()
    for index in range(1800):
        (resources / ("resource-" + "a" * 230 + str(index))).write_bytes(b"")
    packaged.build["artifact_sha256"] = {p.relative_to(packaged.host).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in packaged.host.rglob("*") if p.is_file() and p.name != "build.json"}
    data = json.dumps(packaged.build, sort_keys=True).encode()
    assert 512 * 1024 < len(data) < 8 * 1024**2
    (packaged.host / "build.json").write_bytes(data)
    packaged.receipt["host_receipt_sha256"] = hashlib.sha256(data).hexdigest()
    (packaged.app / "Contents/Resources/app-build.json").write_bytes(canonical_bytes(packaged.receipt))
    installed, measured = admit(packaged)
    assert type(installed) is HostCommandAdmission and type(measured) is CommandPreparationResourceAdmission
    first = resources / ("resource-" + "a" * 230 + "0")
    first.write_bytes(b"Changed member")
    packaged.checks.clear()
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        admit(packaged)
    assert not packaged.checks


@pytest.mark.parametrize("field,value", [
    ("component", [*release.COMMAND_COMPONENT]),
    ("component", ("host-command-json-v1", "9.0.0", release.COMMAND_COMPONENT[2])),
    ("component", ("host-command-json-v1", "1.0.1", "a" * 64)),
    ("installed_platform_policy", "a" * 64), ("resource_platform_policy", "a" * 64),
])
def test_source_policy_rejects_unknown_component_or_platform(field, value):
    values = dict(component=release.COMMAND_COMPONENT, installed_platform_policy=release.INSTALLED_PLATFORM_POLICY,
        resource_platform_policy=release.RESOURCE_PLATFORM_POLICY)
    values[field] = value
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        release.CommandReleasePolicyV1(**values)


@pytest.mark.parametrize("target,field,value", [
    ("installed", "schema_version", True), ("installed", "package_schema", 1), ("installed", "receipt_format", 4),
    ("installed", "helper", "../outside"), ("installed", "helper_source_sha256", "a" * 64),
    ("installed", "helper_sha256", "a" * 64), ("installed", "unknown", True),
    ("installed", "configuration_authority", "execution"), ("installed", "production_admission", "enabled"),
    ("installed", "platform", {"os": "macos", "major": 26.0, "architecture": "arm64"}),
    ("resource", "schema_version", True), ("resource", "receipt_format", 5),
    ("resource", "helper_header_sha256", "a" * 64), ("resource", "helper_source_sha256", "a" * 64),
    ("resource", "resource_policy", "future"), ("resource", "confinement_policy", "future"),
    ("resource", "lifetime_coverage_complete", True), ("resource", "hard_aggregate_enforced", True),
    ("resource", "result_admissible", True), ("resource", "open_gates", []),
    ("build", "signing", "ad_hoc"), ("build", "source_tree_dirty", True), ("build", "source_commit", "invalid"),
    ("build", "unknown", True), ("receipt", "schema_version", True),
    ("receipt", "hardened_runtime", False), ("receipt", "secure_timestamp", False),
    ("receipt", "signed_native_files", True), ("receipt", "source_commit", "2" * 40),
    ("receipt", "unknown", True), ("receipt", "legacy_handoff", 0),
    ("receipt", "starter_packages", [{"unknown": True}]),
    ("receipt", "textgen_signed_profile", {"profile": "future"}),
])
def test_rebound_inventory_or_build_metadata_cannot_broaden_source_policy(packaged, target, field, value):
    getattr(packaged, target)[field] = value
    if target in {"installed", "resource"}:
        filename = "command-execution.json" if target == "installed" else "command-resource-execution.json"
        (packaged.host / filename).write_bytes(canonical_bytes(getattr(packaged, target)))
    packaged.refresh()
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        admit(packaged)
    assert not packaged.checks


@pytest.mark.parametrize("fault", ["extra", "missing", "map-extra", "helper-bytes", "helper-mode", "helper-link",
    "helper-hardlink", "directory-link", "source-module", "own-module-missing", "app-executable-link", "writable", "wrong-owner"])
def test_closed_membership_source_and_file_identity_reject_before_signature(packaged, fault, monkeypatch):
    helper = packaged.helpers[0]
    if fault == "extra": (packaged.host / "extra").write_bytes(b"Synthetic extra")
    elif fault == "missing": helper.unlink()
    elif fault == "map-extra":
        packaged.build["artifact_sha256"]["missing"] = "a" * 64
        data = canonical_bytes(packaged.build); (packaged.host / "build.json").write_bytes(data)
        packaged.receipt["host_receipt_sha256"] = hashlib.sha256(data).hexdigest()
        (packaged.app / "Contents/Resources/app-build.json").write_bytes(canonical_bytes(packaged.receipt))
    elif fault == "helper-bytes": helper.write_bytes(b"Changed synthetic helper")
    elif fault == "helper-mode": helper.chmod(0o600)
    elif fault in {"helper-link", "helper-hardlink"}:
        other = packaged.host / "other"; helper.rename(other)
        helper.symlink_to(other) if fault == "helper-link" else helper.hardlink_to(other)
    elif fault == "directory-link":
        original = packaged.host / "bin"; other = packaged.host / "other-bin"; original.rename(other); original.symlink_to(other)
    elif fault == "source-module":
        (packaged.host / "backend/meecp/host_command.py").write_bytes(b"Changed source")
        packaged.refresh()
    elif fault == "own-module-missing":
        (packaged.host / "backend/meecp/preparation_release_command_admission.py").unlink(); packaged.refresh()
    elif fault == "app-executable-link":
        binary = packaged.app / "Contents/MacOS/meecp"; other = packaged.app / "Contents/MacOS/other"; binary.rename(other); binary.symlink_to(other)
    elif fault == "writable": helper.chmod(0o777)
    else:
        uid = os.getuid(); monkeypatch.setattr(release.os, "getuid", lambda: uid + 1)
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        admit(packaged)
    assert not packaged.checks


@pytest.mark.parametrize("fault", ["same-byte-helper", "restored-byte-helper", "ancestor", "late-extra", "late-kernel"])
def test_signature_inspection_cannot_hide_inode_or_current_platform_replacement(packaged, monkeypatch, fault):
    first = True
    def signature(path, **options):
        nonlocal first
        if not first: return
        first = False
        helper = packaged.helpers[0]
        if fault == "same-byte-helper":
            replacement = packaged.host / "replacement"; shutil.copyfile(helper, replacement); replacement.chmod(0o700); replacement.replace(helper)
        elif fault == "restored-byte-helper":
            data, before = helper.read_bytes(), helper.stat(); helper.write_bytes(data + b"changed"); helper.write_bytes(data)
            os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif fault == "ancestor":
            old = packaged.host.with_name("old-host"); packaged.host.rename(old); shutil.copytree(old, packaged.host)
        elif fault == "late-extra": (packaged.host / "late-extra").write_bytes(b"Late synthetic addition")
        else: monkeypatch.setattr(release, "resource_platform_policy", lambda: "a" * 64)
    monkeypatch.setattr(release, "_signature", signature)
    code = "command_release_platform_unavailable" if fault == "late-kernel" else "command_release_not_admitted"
    with pytest.raises(PreparationError, match="^" + code + "$"):
        admit(packaged)


@pytest.mark.parametrize("failure", ["unknown-kernel", "unsupported-os", "invalid-type", "unbundled", "signature"])
def test_release_closes_on_unaccepted_platform_or_artifact(packaged, monkeypatch, failure):
    selected, host = policy(), packaged.host
    if failure == "unknown-kernel": monkeypatch.setattr(release, "installed_platform_policy", lambda: "a" * 64)
    elif failure == "unsupported-os":
        def unsupported(): raise release.InstalledCommandError("command_platform_unsupported")
        monkeypatch.setattr(release, "installed_platform_policy", unsupported)
    elif failure == "invalid-type": selected = {"component": release.COMMAND_COMPONENT}
    elif failure == "unbundled": host = host.parent
    else:
        def invalid(*args, **kwargs): raise PreparationError("command_release_not_admitted")
        monkeypatch.setattr(release, "_signature", invalid)
    code = "command_release_platform_unavailable" if failure in {"unknown-kernel", "unsupported-os"} else "command_release_not_admitted"
    with pytest.raises(PreparationError, match="^" + code + "$"):
        admit(packaged, selected, host)
    if failure in {"unknown-kernel", "unsupported-os"}:
        assert packaged.checks == [(packaged.app, {"deep": True}), (packaged.helpers[0], {}), (packaged.helpers[1], {})]


@pytest.mark.parametrize("fault", ["helper", "source", "inventory", "membership", "signature", "held-policy"])
def test_platform_mismatch_cannot_hide_any_artifact_denial(packaged, monkeypatch, fault):
    proof = packaged.capture()
    queries = []
    monkeypatch.setattr(release, "installed_platform_policy", lambda: queries.append(True) or "a" * 64)
    if fault == "helper": packaged.helpers[0].write_bytes(b"Changed helper")
    elif fault == "source": (packaged.host / "backend/meecp/host_command.py").write_bytes(b"Changed source")
    elif fault == "inventory": (packaged.host / "command-execution.json").write_bytes(b"{}")
    elif fault == "membership": (packaged.host / "extra").write_bytes(b"Extra member")
    elif fault == "held-policy": proof.policy.write_bytes(b"Changed policy")
    else:
        def invalid(*args, **kwargs): raise PreparationError("command_release_platform_unavailable")
        monkeypatch.setattr(release, "_signature", invalid)
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        release.command_release_admission(policy(), packaged.host, artifact=proof)
    assert not queries


@pytest.mark.parametrize("fault", ["invalid-digest", "wrong-type", "installed-other-error", "resource-other-error", "resource-unsupported"])
def test_only_authentic_platform_unavailability_has_distinct_code(packaged, monkeypatch, fault):
    if fault == "invalid-digest": monkeypatch.setattr(release, "installed_platform_policy", lambda: "future")
    elif fault == "wrong-type": monkeypatch.setattr(release, "installed_platform_policy", lambda: True)
    else:
        kind = release.InstalledCommandError if fault == "installed-other-error" else release.ResourceCommandError
        code = "command_platform_unsupported" if fault == "resource-unsupported" else "command_helper_mismatch"
        def failed(): raise kind(code)
        monkeypatch.setattr(release, "installed_platform_policy" if fault == "installed-other-error" else "resource_platform_policy", failed)
    expected = "command_release_platform_unavailable" if fault == "resource-unsupported" else "command_release_not_admitted"
    with pytest.raises(PreparationError, match="^" + expected + "$"):
        admit(packaged)
    assert packaged.checks == [(packaged.app, {"deep": True}), (packaged.helpers[0], {}), (packaged.helpers[1], {})]


@pytest.mark.parametrize("fault", [None, "invalid", "team", "hardened", "timestamp", "entitlements", "timeout"])
def test_signature_policy_is_read_only_strict_and_has_no_entitlements(tmp_path, monkeypatch, fault):
    path = tmp_path / "synthetic-app"; calls = []
    details = b"CodeDirectory flags=0x10000(runtime)\nTimestamp=synthetic timestamp\nTeamIdentifier=" + release.TEAM_ID.encode() + b"\n"
    if fault == "team": details = details.replace(release.TEAM_ID.encode(), b"OTHERTEAM01")
    elif fault == "hardened": details = details.replace(b"(runtime)", b"(other)")
    elif fault == "timestamp": details = details.replace(b"Timestamp=", b"Other=")
    def run(args, **options):
        calls.append((args, options))
        if fault == "timeout": raise release.subprocess.TimeoutExpired(args, 30)
        return SimpleNamespace(returncode=1 if fault == "invalid" else 0,
            stderr=details if "--verbose=4" in args else b"", stdout=b"<key>synthetic</key>" if fault == "entitlements" and "--entitlements" in args else b"")
    monkeypatch.setattr(release.subprocess, "run", run)
    if fault is None: SIGNATURE(path, deep=True)
    elif fault == "timeout":
        with pytest.raises(release.subprocess.TimeoutExpired): SIGNATURE(path, deep=True)
    else:
        with pytest.raises(PreparationError, match="^command_release_not_admitted$"): SIGNATURE(path, deep=True)
    assert all(options == {"capture_output": True, "timeout": 30,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}} for _, options in calls)
    assert calls[0][0][:5] == ["/usr/bin/codesign", "--verify", "--strict", "--deep", "-R"]
    assert calls[0][0][5].startswith("=anchor apple generic") and release.CERTIFICATE_SHA1 in calls[0][0][5]
    assert all("--sign" not in args and "--force" not in args for args, _ in calls)


@pytest.mark.parametrize("fault", ["missing", "wrong-type", "wrong-root", "partial", "stale-policy", "stale-seal", "stale-module"])
def test_constructor_requires_original_full_artifact_proof(packaged, fault):
    proof = packaged.capture()
    if fault == "missing": proof = None
    elif fault == "wrong-type": proof = SimpleNamespace(check_current=lambda **_: None)
    elif fault == "wrong-root":
        proof = release.ReleaseArtifactProof(proof.identities, proof.critical, packaged.host / "other-policy", b"{}")
    elif fault == "partial":
        omitted = packaged.host / "backend/meecp/preparation_release_command_admission.py"
        proof = release.ReleaseArtifactProof(tuple(record for record in proof.identities if record[0] != omitted),
            proof.critical, proof.policy, proof.policy_bytes)
    elif fault == "stale-policy": proof.policy.write_bytes(b'{"changed":true}')
    elif fault == "stale-seal": (packaged.app / "Contents/_CodeSignature/CodeResources").write_bytes(b"Changed seal")
    else: (packaged.host / "backend/meecp/preparation_release_command_admission.py").write_bytes(b"Changed adapter")
    with pytest.raises(PreparationError, match="^command_release_not_admitted$"):
        release.command_release_admission(policy(), packaged.host, artifact=proof)
    assert not packaged.checks


def service(packaged, monkeypatch):
    from meecp.host_command import HostCommands
    proof = packaged.capture()
    admission, _ = release.command_release_admission(policy(), packaged.host, artifact=proof)
    monkeypatch.setattr(HostCommands, "current", lambda self, slot: True)
    monkeypatch.setattr(HostCommands, "admit", lambda self, spec: True)
    return release.ReleasedHostCommands(SimpleNamespace(), admission, proof)


@pytest.mark.parametrize("member", ["policy", "seal", "build", "native", "module"])
def test_installed_checks_retain_authority_at_publication_and_native_boundary(packaged, monkeypatch, member):
    owner = service(packaged, monkeypatch)
    executor = SimpleNamespace(before_native_create=None)
    monkeypatch.setattr(release.HostCommands, "executor", lambda self, slot: executor)
    owner.executors["slot"] = executor
    assert owner.artifact is not None and owner.current(None) and owner.admit(None)
    assert owner.executor("slot").before_native_create(None) is True
    path = {"policy": owner.artifact.policy, "seal": packaged.app / "Contents/_CodeSignature/CodeResources",
        "build": packaged.host / "build.json", "native": packaged.app / "Contents/MacOS/meecp",
        "module": packaged.host / "backend/meecp/preparation_release_command_admission.py"}[member]
    path.write_bytes(path.read_bytes() + b"changed")
    assert owner.current(None) is False and owner.admit(None) is False
    assert owner.executor("slot").before_native_create(None) is False
    with pytest.raises(release.InstalledCommandError, match="^command_authority_changed$"):
        owner.executor("new-slot")
    assert owner.executor("slot") is executor  # Existing handles remain joinable.


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["session", "invoke"])
async def test_joined_full_boundary_denies_changed_noncritical_payload(packaged, monkeypatch, operation):
    owner = service(packaged, monkeypatch)
    calls = []
    async def original(*args): calls.append(args); return "synthetic"
    monkeypatch.setattr(release.HostCommands, operation, original)
    arguments = (None,) if operation == "session" else (None, "probe", {}, 1)
    assert await getattr(owner, operation)(*arguments) == "synthetic"
    (packaged.host / "backend/meecp/preparation_release_command_admission.py").write_bytes(b"Changed payload")
    with pytest.raises(release.InstalledCommandError, match="^command_authority_changed$"):
        await getattr(owner, operation)(*arguments)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_canceled_full_boundary_joins_worker_before_return_and_never_enters_session(packaged, monkeypatch):
    owner = service(packaged, monkeypatch)
    entered, finish, settled = threading.Event(), threading.Event(), threading.Event()
    original = release.ReleaseArtifactProof.check_current
    def blocked(self, *, full=False):
        if full:
            entered.set()
            assert finish.wait(3)
            try: original(self, full=True)
            finally: settled.set()
        else: original(self)
    monkeypatch.setattr(release.ReleaseArtifactProof, "check_current", blocked)
    async def forbidden(*args): pytest.fail("canceled authority check entered installed session")
    monkeypatch.setattr(release.HostCommands, "session", forbidden)
    task = asyncio.create_task(owner.session(None))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert settled.is_set()


# These fixtures compile and execute original unsigned native helpers/targets.
# The enclosing metadata/signature replies remain explicitly synthetic.
from test_host_command import native, package, sdk_call
from test_meecp_host import install


@pytest.mark.asyncio
async def test_actual_installed_call_and_last_native_fence_retain_original_proof(packaged, tmp_path, native, monkeypatch):
    from meecp import command_execution_v2
    from meecp.supervisor import Supervisor
    shutil.copyfile(native[1], packaged.helpers[0])
    packaged.helpers[0].chmod(0o700)
    packaged.installed["helper_sha256"] = hashlib.sha256(packaged.helpers[0].read_bytes()).hexdigest()
    (packaged.host / "command-execution.json").write_bytes(canonical_bytes(packaged.installed))
    packaged.refresh()
    proof = packaged.capture()
    admitted, _ = release.command_release_admission(policy(), packaged.host, artifact=proof)
    host = Supervisor(tmp_path / "installed-state", native[0], command_admission=admitted)
    host.commands = release.ReleasedHostCommands(host, admitted, proof)
    try:
        slot = await install(host, package(tmp_path, native), "Held release fixture")
        await sdk_call(host, slot, {"ok": True})
        assert not host.commands.pending(slot.id)
        session = await host.commands.session(slot)
        original, calls = host.commands.admit, []
        def fence(spec):
            calls.append(True)
            if len(calls) == 2:
                (packaged.app / "Contents/_CodeSignature/CodeResources").write_bytes(b"Changed seal at native boundary")
            return original(spec)
        monkeypatch.setattr(host.commands, "admit", fence)
        host.commands.executor(slot.id).admit = fence
        def forbidden(*args, **kwargs): pytest.fail("revoked native boundary created a child")
        monkeypatch.setattr(command_execution_v2.subprocess, "Popen", forbidden)
        with pytest.raises(release.InstalledCommandError, match="^command_authority_changed$"):
            await host.commands.invoke(session, "probe", {"ok": True}, 91)
        assert len(calls) == 2 and not host.commands.pending(slot.id)
        assert host.commands.executor(slot.id).reconcile()["pending"] == 0
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_actual_queued_invocation_revocation_joins_owned_helper(packaged, tmp_path, native, monkeypatch):
    from meecp.command_execution_v2 import SEED_SIZE
    from meecp.supervisor import Supervisor
    shutil.copyfile(native[1], packaged.helpers[0])
    packaged.helpers[0].chmod(0o700)
    packaged.installed["helper_sha256"] = hashlib.sha256(packaged.helpers[0].read_bytes()).hexdigest()
    (packaged.host / "command-execution.json").write_bytes(canonical_bytes(packaged.installed))
    packaged.refresh()
    proof = packaged.capture()
    admitted, _ = release.command_release_admission(policy(), packaged.host, artifact=proof)
    host = Supervisor(tmp_path / "installed-state", native[0], command_admission=admitted)
    host.commands = release.ReleasedHostCommands(host, admitted, proof)
    tasks = []
    try:
        slot = await install(host, package(tmp_path, native, "hang", deadline=3000, smoke=False), "Queued release fixture")
        sessions = [await host.commands.session(slot), await host.commands.session(slot)]
        tasks = [asyncio.create_task(host.commands.invoke(sessions[0], "probe", {"ok": True}, 1))]
        async with asyncio.timeout(2):
            while host.commands.executor(slot.id)._active is None:
                await asyncio.sleep(0.001)
        owned = host.commands.executor(slot.id)._active
        runtime, queued = host.commands.executor(slot.id), asyncio.Event()
        execute = runtime.execute
        async def observed(spec, payload, *, instance_id):
            queued.set()
            return await execute(spec, payload, instance_id=instance_id)
        monkeypatch.setattr(runtime, "execute", observed)
        tasks.append(asyncio.create_task(host.commands.invoke(sessions[1], "probe", {"ok": True}, 2)))
        async with asyncio.timeout(2):
            await queued.wait()
        assert host.commands.counts.get(slot.id) == 2
        proof.policy.write_bytes(b'{"changed":true}')
        # Cancel and join the first owned call after revocation. The queued
        # call has ample time to reach its authority fence once that join ends.
        tasks[0].cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], release.InstalledCommandError)
        assert str(outcomes[1]) == "command_not_admitted"
        assert owned.poll() in (0, 64) and not host.commands.pending(slot.id)
        assert len(host.commands.executor(slot.id).journal["entries"]) == 1
        item = runtime.journal["entries"][0]
        assert runtime._proof(item)[0] and runtime.reconcile() == {"pending": 0, "reserved_units": 0}
        if owned.returncode == 64:
            assert runtime._receipt_path(item).read_bytes()[SEED_SIZE] == ord("N")
        for session in sessions: await session.close()
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()
