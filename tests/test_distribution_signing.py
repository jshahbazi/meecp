"""Distribution contracts; real Apple credentials are exercised only in release CI."""
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_mcpme_app as app_builder  # noqa: E402
import build_mcpme_dmg as dmg_builder  # noqa: E402
import build_mcpme_host as host_builder  # noqa: E402
from build_mcpme_host import BuildCommandError  # noqa: E402
from build_mcpme_host import project_version  # noqa: E402
from mcpme.contracts import PackageError, validate_package  # noqa: E402
from test_mcpme_packages import edit_manifest, make_package, relock  # noqa: E402


CERTIFICATE = "A" * 40
IDENTITY = "Developer ID Application: Distribution Test (TESTTEAM01)"
SUBMISSION = "12345678-1234-1234-1234-123456789abc"


def test_only_developer_id_application_identities_can_make_release(monkeypatch, tmp_path):
    keychain = tmp_path / "signing.keychain-db"
    keychain.touch()
    calls = []

    def identities(*arguments):
        calls.append(arguments)
        return (f'1) {"B" * 40} "Apple Development: Distribution Test (TESTTEAM01)"\n'
                f'2) {CERTIFICATE} "{IDENTITY}"\n')

    monkeypatch.setattr(app_builder, "run", identities)
    assert app_builder.developer_identity(IDENTITY, keychain) == CERTIFICATE
    assert app_builder.developer_identity(CERTIFICATE.lower(), keychain) == CERTIFICATE
    assert calls[0][-1] == str(keychain)
    for invalid in ("-", "", "Apple Development: Distribution Test (TESTTEAM01)", "B" * 40):
        with pytest.raises(SystemExit, match="developer_id_identity_unavailable"):
            app_builder.developer_identity(invalid, keychain)


def test_development_textgen_closure_is_verified_without_native_byte_rewrites(monkeypatch, tmp_path):
    preserved = tmp_path / "host/preparation-toolchain/textgen-runtime"
    preserved.mkdir(parents=True)
    engine = preserved / "engine.dylib"
    engine.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic signed engine")
    other = tmp_path / "other.dylib"
    other.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic private runtime")
    before = engine.read_bytes()
    calls = []
    def run(*args):
        calls.append(args)
        return "synthetic:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)" if args[0] == "/usr/bin/otool" else ""
    monkeypatch.setattr(app_builder, "run", run)
    assert app_builder.sign_runtime(tmp_path, immutable_runtime=preserved) == 1
    assert engine.read_bytes() == before
    assert ("/usr/bin/codesign", "--verify", "--strict", str(engine)) in calls
    assert not any(args[-1] == str(engine) and args[0] == "/usr/bin/install_name_tool" for args in calls)
    assert not any(args[-1] == str(engine) and "--force" in args for args in calls)


def test_signed_textgen_source_build_is_rejected_before_credential_lookup(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported exact engine reached signing credentials or tools")
    monkeypatch.setattr(app_builder, "developer_identity", forbidden)
    monkeypatch.setattr(dmg_builder, "developer_identity", forbidden)
    monkeypatch.setattr(app_builder, "run", forbidden)
    monkeypatch.setattr(dmg_builder, "run", forbidden)
    with pytest.raises(SystemExit, match="^signed_textgen_preparation_not_supported$"):
        app_builder.build(tmp_path / "mcpme.app", sign_identity=CERTIFICATE, textgen_engine=tmp_path / "engine")
    with pytest.raises(SystemExit, match="^signed_textgen_preparation_not_supported$"):
        dmg_builder.build(tmp_path / "mcpme.dmg", sign_identity=CERTIFICATE, textgen_engine=tmp_path / "engine")


def test_supplied_development_textgen_app_cannot_enter_signed_release_path(monkeypatch, tmp_path):
    app = tmp_path / "mcpme.app"
    (app / "Contents/Resources/host/preparation-toolchain/textgen-runtime").mkdir(parents=True)
    monkeypatch.setattr(dmg_builder, "developer_identity",
                        lambda *args, **kwargs: pytest.fail("development engine reached credential lookup"))
    with pytest.raises(SystemExit, match="^signed_textgen_preparation_not_supported$"):
        dmg_builder.build(tmp_path / "mcpme.dmg", app=app, sign_identity=CERTIFICATE)


@pytest.mark.parametrize("version", ["1", "1.2", "01.2.3", "1.2.3-alpha", "v1.2.3"])
def test_version_override_requires_strict_major_minor_patch(version):
    with pytest.raises(SystemExit, match="release_version_requires_major_minor_patch"):
        project_version(version)


def test_version_override_is_accepted_without_modifying_project_source(monkeypatch, tmp_path):
    monkeypatch.setattr(host_builder, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    before = (tmp_path / "pyproject.toml").read_bytes()
    assert project_version("2.4.6") == "2.4.6"
    assert project_version() == "0.1.0"
    assert (tmp_path / "pyproject.toml").read_bytes() == before


def test_app_rejects_invalid_override_before_external_build(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(app_builder, "run", lambda *args: called.append(args))
    with pytest.raises(SystemExit, match="release_version_requires_major_minor_patch"):
        app_builder.build(tmp_path / "mcpme.app", version="1.2")
    assert called == []


def test_dmg_rejects_invalid_override_before_external_build(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(dmg_builder, "developer_identity", lambda *args: called.append(args))
    with pytest.raises(SystemExit, match="release_version_requires_major_minor_patch"):
        dmg_builder.build(tmp_path / "mcpme.dmg", version="1.2")
    assert called == []


def synthetic_starter(destination):
    starter = make_package(destination)
    edit_manifest(starter, package_id="local.json-utilities", name="JSON Utilities")
    (starter / "bin/service").write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic starter binary")
    relock(starter)
    return starter


@pytest.mark.parametrize("release", [False, True])
def test_app_build_records_selected_version_and_validates_signed_starter(monkeypatch, tmp_path, release):
    source = tmp_path / "source"
    for relative in ("Sources/McpmeMenuBar/main.swift", "Sources/McpmeMenuBar/Brand.swift",
                     "README.md", "model-manifest.json", "THIRD_PARTY_NOTICES.md",
                     "assets/brand/source.svg", "pyproject.toml"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[project]\nversion = "0.1.0"\n' if relative == "pyproject.toml" else "synthetic")
    (source / "docs/reviews").mkdir(parents=True)
    review = source / "docs/reviews/DIRECTORY_ONBOARDING_REVIEW.md"
    review.write_text("# Directory onboarding review\n")
    (source / "docs/AUTOMATIC_PREPARATION.md").write_text(
        "[Review](reviews/DIRECTORY_ONBOARDING_REVIEW.md)\n")
    binary_directory = tmp_path / "native"
    binary_directory.mkdir()
    (binary_directory / "McpmeMenuBar").write_bytes(b"synthetic native app")
    before = (source / "pyproject.toml").read_bytes()
    host_versions = []
    signed_starter = None
    validated_digest = None

    def run(*arguments):
        nonlocal validated_digest
        if "--show-bin-path" in arguments:
            return str(binary_directory)
        if arguments[0] == "/usr/bin/iconutil":
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"synthetic icon")
        if "rev-parse" in arguments:
            return "a" * 40
        if "validate" in arguments:
            assert signed_starter is not None
            package = validate_package(Path(arguments[arguments.index("--package") + 1]))
            validated_digest = package.digest
            return json.dumps({"valid": True, "package_digest": package.digest,
                               "package": package.manifest.model_dump()})
        if arguments[0] == "/usr/bin/codesign":
            # The final app seal must include the post-signing package receipt.
            assert validated_digest is not None
            receipt = json.loads((Path(arguments[-1]) / "Contents/Resources/app-build.json").read_text())
            assert receipt["starter_packages"][0]["package_digest"] == validated_digest
        return ""

    def build_host(destination, *, version):
        host_versions.append(version)
        (destination / "bin").mkdir(parents=True)
        resource = host_builder._resource_inventory_contract()
        helper = destination / resource["helper"]
        helper.write_bytes(b"synthetic resource helper")
        resource.update(helper_sha256=app_builder.sha256(helper), helper_source_sha256="a" * 64,
                        helper_header_sha256="b" * 64)
        inventory = destination / "command-resource-execution.json"
        inventory.write_text(json.dumps(resource))
        (destination / "build.json").write_text(json.dumps({
            "host_version": version, "artifact_sha256": {
                resource["helper"]: app_builder.sha256(helper), "command-resource-execution.json": app_builder.sha256(inventory)},
        }))
        return destination

    def sign_runtime(staged, identity, signed_release, keychain):
        nonlocal signed_starter
        assert signed_release is release
        starter = staged / "Contents/Resources" / app_builder.STARTER_PACKAGE_PATH
        signed_starter = starter / "bin/service"
        signed_starter.write_bytes(signed_starter.read_bytes() + b" changed by codesign")
        helper = staged / "Contents/Resources/host/bin/mcpme-command-resource-reap"
        helper.write_bytes(helper.read_bytes() + b" changed by codesign")
        # The previously valid lock must fail until the app builder refreshes
        # the signed native artifact, as happens with actual codesign output.
        with pytest.raises(PackageError, match="artifact_(?:hash|size)_mismatch"):
            validate_package(starter)
        return 1

    monkeypatch.setattr(app_builder, "ROOT", source)
    monkeypatch.setattr(app_builder, "run", run)
    monkeypatch.setattr(app_builder, "build_host", build_host)
    monkeypatch.setattr(app_builder, "prepare_json_package", synthetic_starter)
    monkeypatch.setattr(app_builder, "developer_identity", lambda *args: CERTIFICATE)
    monkeypatch.setattr(app_builder, "sign_runtime", sign_runtime)
    monkeypatch.setattr(app_builder, "verify_signature", lambda *args, **kwargs: None)
    app = app_builder.build(tmp_path / "mcpme.app", sign_identity=IDENTITY if release else None, version="2.4.6")
    resources = app / "Contents/Resources"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    receipt = json.loads((resources / "app-build.json").read_text())
    host_receipt = json.loads((resources / "host/build.json").read_text())
    assert info["CFBundleShortVersionString"] == receipt["version"] == host_receipt["host_version"] == "2.4.6"
    assert host_versions == ["2.4.6"]
    assert receipt["source_commit"] == "a" * 40
    assert receipt["source_tree_dirty"] is False
    assert receipt["signing"] == host_receipt["signing"] == ("developer_id" if release else "ad_hoc")
    resource = json.loads((resources / "host/command-resource-execution.json").read_bytes())
    assert resource["helper_source_sha256"] == "a" * 64 and resource["helper_header_sha256"] == "b" * 64
    assert resource["helper_sha256"] == app_builder.sha256(resources / "host" / resource["helper"])
    assert all(resource[key] is False for key in ("lifetime_coverage_complete", "hard_aggregate_enforced", "result_admissible"))
    assert resource["production_admission"] == "disabled"
    for relative, digest in host_receipt["artifact_sha256"].items():
        assert digest == app_builder.sha256(resources / "host" / relative)
    starter = resources / app_builder.STARTER_PACKAGE_PATH
    verified = validate_package(starter)
    assert receipt["starter_packages"] == [{"path": "starter-packages/json-utilities",
        "package_id": "local.json-utilities", "version": "1.0.0", "package_digest": verified.digest,
        "lock_sha256": app_builder.sha256(starter / "mcpme.lock.json")}]
    assert verified.digest == validated_digest
    assert (resources / "docs/reviews/DIRECTORY_ONBOARDING_REVIEW.md").read_bytes() == review.read_bytes()
    assert (resources / "docs/AUTOMATIC_PREPARATION.md").read_bytes() == (source / "docs/AUTOMATIC_PREPARATION.md").read_bytes()
    assert (source / "pyproject.toml").read_bytes() == before


def test_final_starter_validation_rejects_unsigned_artifact_changes(monkeypatch, tmp_path):
    starter = synthetic_starter(tmp_path / "starter")
    (starter / "LICENSE").write_text("unexpected license change")
    calls = []

    def validate(*arguments):
        calls.append(arguments)
        package = validate_package(Path(arguments[arguments.index("--package") + 1]))
        return json.dumps({"valid": True, "package_digest": package.digest,
                           "package": package.manifest.model_dump()})

    monkeypatch.setattr(app_builder, "run", validate)
    with pytest.raises(PackageError, match="artifact_(?:hash|size)_mismatch"):
        app_builder.finalize_starter_package(starter, tmp_path / "host", release=False)
    assert calls[0][0] == str(tmp_path / "host/bin/mcpme")


def test_all_native_helpers_and_extensions_get_hardened_signatures(monkeypatch, tmp_path):
    native = tmp_path / "Contents/MacOS/mcpme"
    extension = tmp_path / "Contents/Resources/host/python/site-packages/native.so"
    reaper = tmp_path / "Contents/Resources/host/bin/mcpme-reap"
    resource_reaper = reaper.parent / "mcpme-command-resource-reap"
    for path in (native, extension, reaper, resource_reaper):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic native file")
    (reaper.parent / "mcpme").write_text("#!/bin/sh\n")
    calls = []

    def run(*arguments):
        calls.append(arguments)
        if arguments[0] == "/usr/bin/otool":
            return f"{arguments[-1]}:\n /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)"
        return ""

    monkeypatch.setattr(app_builder, "run", run)
    assert app_builder.sign_runtime(tmp_path, CERTIFICATE, True) == 4
    signatures = [call for call in calls if "--force" in call]
    assert {Path(call[-1]) for call in signatures} == {native, extension, reaper, resource_reaper}
    assert signatures[-1][-1] == str(native)
    for call in signatures:
        assert "--deep" not in call
        assert "--timestamp" in call
        assert call[call.index("--options") + 1] == "runtime"
        assert call[call.index("--sign") + 1] == CERTIFICATE
    validations = [call for call in calls if "--verify" in call]
    assert len(validations) == 4
    assert all(CERTIFICATE in call[call.index("-R") + 1] for call in validations)


@pytest.mark.skipif(platform.system() != "Darwin", reason="Requires macOS codesign")
def test_native_codesign_evaluates_literal_certificate_requirement():
    # An Apple platform binary must fail the requested Developer ID certificate
    # constraint, with the verification exit code rather than a missing-file or
    # requirement-parser error. This exercises the real tool's -R syntax.
    with pytest.raises(BuildCommandError) as failure:
        app_builder.verify_signature(Path("/usr/bin/true"), CERTIFICATE)
    assert failure.value.returncode == 3


def test_runtime_refuses_external_symlink_before_signing(tmp_path):
    (tmp_path / "escape").symlink_to("/usr/bin/python3")
    with pytest.raises(SystemExit, match="external symlink"):
        app_builder.sign_runtime(tmp_path)


def test_runtime_refuses_homebrew_native_dependency(monkeypatch, tmp_path):
    library = tmp_path / "native.so"
    library.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic native file")
    monkeypatch.setattr(app_builder, "run", lambda *args: (
        "native.so:\n /opt/homebrew/lib/libexample.dylib (compatibility version 1.0.0)"
    ))
    with pytest.raises(SystemExit, match="external native dependency"):
        app_builder.sign_runtime(tmp_path, CERTIFICATE, True)


@pytest.mark.parametrize("status", ["Invalid", "Rejected", "In Progress", None])
def test_notary_failures_never_become_accepted_or_log_response(monkeypatch, capsys, status):
    response = {"id": SUBMISSION, "status": status, "private": "do-not-log-this"}
    monkeypatch.setattr(dmg_builder, "run", lambda *args: json.dumps(response))
    with pytest.raises(SystemExit, match="notarization_not_accepted"):
        dmg_builder.notarize(Path("installer.dmg"), "release")
    assert capsys.readouterr() == ("", "")


def test_notary_uses_selected_keychain_and_records_only_submission(monkeypatch, tmp_path):
    calls = []

    def run(*args):
        calls.append(args)
        return json.dumps({"id": SUBMISSION, "status": "Accepted", "message": "private notary output"})

    monkeypatch.setattr(dmg_builder, "run", run)
    keychain = tmp_path / "temporary.keychain-db"
    assert dmg_builder.notarize(Path("installer.dmg"), "release", keychain) == {
        "status": "accepted", "submission_id": SUBMISSION,
    }
    assert calls[0][calls[0].index("--keychain") + 1] == str(keychain)
    assert "--wait" in calls[0]


@pytest.fixture
def fake_packaging(monkeypatch, tmp_path):
    calls = []
    info = {"CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1", "LSMinimumSystemVersion": "26.0"}
    monkeypatch.setattr(dmg_builder, "ROOT", tmp_path)
    monkeypatch.setattr(dmg_builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dmg_builder.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dmg_builder, "developer_identity", lambda *args: CERTIFICATE)
    def metadata(app, certificate_hash=None):
        plist = app / "Contents/Info.plist"
        if plist.exists():
            return plistlib.loads(plist.read_bytes())
        return info
    monkeypatch.setattr(dmg_builder, "app_metadata", metadata)
    monkeypatch.setattr(dmg_builder, "verify_signature", lambda *args, **kwargs: None)

    def build_app(destination, sign_identity=None, keychain=None, version=None):
        resources = destination / "Contents/Resources"
        resources.mkdir(parents=True)
        (resources / "app-build.json").write_text(json.dumps({
            "signing": "developer_id" if sign_identity else "ad_hoc",
            "distribution": "developer_id_native_app" if sign_identity else "development_native_app",
        }))
        (destination / "Contents").mkdir(exist_ok=True)
        (destination / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": version or "0.1.0", "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "26.0", "CFBundleIdentifier": "com.jshahbazi.mcpme",
            "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL"}))
        return destination

    def run(*arguments):
        calls.append(arguments)
        if arguments[:2] == ("/usr/bin/hdiutil", "create"):
            Path(arguments[-1]).write_bytes(b"synthetic image bytes")
        elif arguments[:2] == ("/usr/bin/ditto", "-c"):
            Path(arguments[-1]).write_bytes(b"synthetic zip")
        elif arguments[0] == "/usr/bin/ditto":
            shutil.copytree(arguments[1], arguments[2])
        elif "notarytool" in arguments:
            return json.dumps({"status": "Accepted", "id": SUBMISSION})
        return ""

    def verify(image, workspace, metadata, certificate, gatekeeper):
        assert {path.name for path in (workspace / "contents").iterdir()} == {"mcpme.app", "Applications"}
        return {"fake_contract_verification": True}

    monkeypatch.setattr(dmg_builder, "build_app", build_app)
    monkeypatch.setattr(dmg_builder, "run", run)
    monkeypatch.setattr(dmg_builder, "verify_image", verify)
    return calls


@pytest.mark.parametrize("signed,notarized,distribution", [
    (False, False, "development_drag_install_dmg"),
    (True, False, "developer_id_drag_install_dmg"),
    (True, True, "notarized_drag_install_dmg"),
])
def test_published_receipt_describes_actual_distribution(fake_packaging, tmp_path, signed, notarized, distribution):
    image = dmg_builder.build(tmp_path / "installer.dmg", sign_identity=IDENTITY if signed else None,
                              notary_profile="release" if notarized else None)
    receipt = json.loads(image.with_name(image.name + ".build.json").read_text())
    assert receipt["distribution"] == distribution
    assert receipt["sha256"] == dmg_builder.sha256(image)
    assert receipt["bytes"] == image.stat().st_size
    assert receipt["disk_image_signed"] is signed
    assert receipt["notarization"] == ("accepted" if notarized else "not_requested")
    if notarized:
        assert receipt["app_build"]["notarization_ticket_stapled"] is True
        assert receipt["notarization_ticket_stapled"] is True
        submissions = [call for call in fake_packaging if "notarytool" in call]
        assert len(submissions) == 2
        assert Path(submissions[0][3]).suffix == ".zip"
        assert Path(submissions[1][3]).suffix == ".dmg"
        archives = [call for call in fake_packaging if call[:2] == ("/usr/bin/ditto", "-c")]
        assert len(archives) == 1
        assert archives[0][-1] == submissions[0][3]
        assert not Path(archives[0][-1]).exists()
        first_staple = next(i for i, call in enumerate(fake_packaging) if "staple" in call)
        image_creation = next(i for i, call in enumerate(fake_packaging) if call[:2] == ("/usr/bin/hdiutil", "create"))
        assert first_staple < image_creation
    else:
        assert not any("notarytool" in call or "stapler" in call for call in fake_packaging)
        assert not any(call[:2] == ("/usr/bin/ditto", "-c") for call in fake_packaging)


def test_version_override_propagates_to_distribution_receipt(fake_packaging, tmp_path):
    image = dmg_builder.build(tmp_path / "installer.dmg", version="2.4.6")
    receipt = json.loads(image.with_name(image.name + ".build.json").read_text())
    assert receipt["version"] == "2.4.6"


def test_supplied_app_version_override_must_match(fake_packaging, tmp_path):
    supplied = tmp_path / "supplied.app"
    (supplied / "Contents").mkdir(parents=True)
    (supplied / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "26.0", "CFBundleIdentifier": "com.jshahbazi.mcpme",
        "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL"}))
    with pytest.raises(SystemExit, match="supplied_app_version_mismatch"):
        dmg_builder.build(tmp_path / "installer.dmg", app=supplied, version="2.4.6")


@pytest.mark.parametrize("reject_submission", [1, 2])
def test_rejected_notarization_does_not_publish_image_or_receipt(fake_packaging, monkeypatch, tmp_path, reject_submission):
    submissions = 0

    def reject(*args):
        nonlocal submissions
        submissions += 1
        if submissions == reject_submission:
            raise SystemExit("notarization_not_accepted")
        return {"status": "accepted", "submission_id": SUBMISSION}

    monkeypatch.setattr(dmg_builder, "notarize", reject)
    output = tmp_path / "installer.dmg"
    with pytest.raises(SystemExit, match="notarization_not_accepted"):
        dmg_builder.build(output, sign_identity=IDENTITY, notary_profile="release")
    assert not output.exists()
    assert not output.with_name(output.name + ".build.json").exists()
    if reject_submission == 1:
        assert not any("stapler" in call for call in fake_packaging)


def test_gatekeeper_rejection_does_not_publish(fake_packaging, monkeypatch, tmp_path):
    original_run = dmg_builder.run

    def run(*arguments):
        if arguments[0] == "/usr/sbin/spctl" and "open" in arguments:
            raise subprocess.CalledProcessError(1, arguments, stderr="private tool output")
        return original_run(*arguments)

    monkeypatch.setattr(dmg_builder, "run", run)
    output = tmp_path / "installer.dmg"
    with pytest.raises(subprocess.CalledProcessError):
        dmg_builder.build(output, sign_identity=IDENTITY, notary_profile="release")
    assert not output.exists()
    assert not output.with_name(output.name + ".build.json").exists()


def test_notarization_requires_explicit_release_identity(fake_packaging, tmp_path):
    with pytest.raises(SystemExit, match="requires_signing_identity"):
        dmg_builder.build(tmp_path / "installer.dmg", notary_profile="release")
    assert fake_packaging == []


@pytest.mark.parametrize("obstacle", ["image", "receipt", "suffix", "image_symlink", "receipt_symlink"])
def test_dmg_requires_fresh_output_and_receipt(fake_packaging, tmp_path, obstacle):
    image = tmp_path / "installer.dmg"
    receipt = image.with_name(image.name + ".build.json")
    if obstacle == "image":
        image.write_bytes(b"existing image")
    elif obstacle == "receipt":
        receipt.write_text("existing receipt")
    elif obstacle == "suffix":
        image = image.with_suffix(".tar")
    elif obstacle == "image_symlink":
        image.symlink_to(tmp_path / "missing")
    else:
        receipt.symlink_to(tmp_path / "missing")
    with pytest.raises(SystemExit, match="fresh"):
        dmg_builder.build(image)
    assert fake_packaging == []


def test_publication_failure_rolls_back_dmg_and_receipt(fake_packaging, monkeypatch, tmp_path):
    original_link = dmg_builder.os.link
    image = tmp_path / "installer.dmg"
    receipt = image.with_name(image.name + ".build.json")

    def link(staged, destination):
        if destination == receipt:
            raise OSError("publication failure")
        original_link(staged, destination)

    monkeypatch.setattr(dmg_builder.os, "link", link)
    with pytest.raises(OSError, match="publication failure"):
        dmg_builder.build(image)
    assert not image.exists()
    assert not receipt.exists()


def test_publication_collision_preserves_existing_file(fake_packaging, monkeypatch, tmp_path):
    original_link = dmg_builder.os.link
    image = tmp_path / "installer.dmg"
    receipt = image.with_name(image.name + ".build.json")

    def link(staged, destination):
        if destination == receipt:
            receipt.write_bytes(b"another publisher")
        original_link(staged, destination)

    monkeypatch.setattr(dmg_builder.os, "link", link)
    with pytest.raises(FileExistsError):
        dmg_builder.build(image)
    assert not image.exists()
    assert receipt.read_bytes() == b"another publisher"


def test_private_runtime_uses_isolated_home_and_minimal_environment(monkeypatch, tmp_path):
    app = tmp_path / "test Applications/mcpme.app"
    captured = {}

    def run(arguments, **kwargs):
        captured.update({"arguments": arguments, **kwargs})

    monkeypatch.setattr(dmg_builder.subprocess, "run", run)
    dmg_builder.verify_runtime(app, tmp_path)
    assert captured["arguments"] == [str(app / "Contents/Resources/host/bin/mcpme"), "--help"]
    assert captured["cwd"] == tmp_path / "test home"
    assert captured["env"] == {"HOME": str(tmp_path / "test home"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                               "LANG": "en_US.UTF-8", "TMPDIR": str(tmp_path)}
    assert captured["stdout"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.PIPE
    assert captured["check"] is True
    assert captured["timeout"] == 30
