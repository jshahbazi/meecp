"""Distribution contracts; real Apple credentials are exercised only in release CI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_mcpme_app as app_builder  # noqa: E402
import build_mcpme_dmg as dmg_builder  # noqa: E402


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


def test_all_native_helpers_and_extensions_get_hardened_signatures(monkeypatch, tmp_path):
    native = tmp_path / "Contents/MacOS/mcpme"
    extension = tmp_path / "Contents/Resources/host/python/site-packages/native.so"
    reaper = tmp_path / "Contents/Resources/host/bin/mcpme-reap"
    for path in (native, extension, reaper):
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
    assert app_builder.sign_runtime(tmp_path, CERTIFICATE, True) == 3
    signatures = [call for call in calls if "--force" in call]
    assert {Path(call[-1]) for call in signatures} == {native, extension, reaper}
    assert signatures[-1][-1] == str(native)
    for call in signatures:
        assert "--deep" not in call
        assert "--timestamp" in call
        assert call[call.index("--options") + 1] == "runtime"
        assert call[call.index("--sign") + 1] == CERTIFICATE
    validations = [call for call in calls if "--verify" in call]
    assert len(validations) == 3
    assert all(CERTIFICATE in call[call.index("-R") + 1] for call in validations)


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
    monkeypatch.setattr(dmg_builder, "app_metadata", lambda *args: info)
    monkeypatch.setattr(dmg_builder, "verify_signature", lambda *args, **kwargs: None)

    def build_app(destination, sign_identity=None, keychain=None):
        resources = destination / "Contents/Resources"
        resources.mkdir(parents=True)
        (resources / "app-build.json").write_text(json.dumps({
            "signing": "developer_id" if sign_identity else "ad_hoc",
            "distribution": "developer_id_native_app" if sign_identity else "development_native_app",
        }))
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

    def verify(image, workspace, metadata, instructions, certificate, gatekeeper):
        assert (workspace / "contents/Install.md").read_text() == instructions
        assert gatekeeper == ("notarized by Apple" in instructions)
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
    assert receipt["disk_image_signed"] is signed
    assert receipt["notarization"] == ("accepted" if notarized else "not_requested")
    if notarized:
        assert receipt["app_build"]["notarization_ticket_stapled"] is True
        assert receipt["notarization_ticket_stapled"] is True
        submissions = [call for call in fake_packaging if "notarytool" in call]
        assert len(submissions) == 2
        assert Path(submissions[0][3]).suffix == ".zip"
        assert Path(submissions[1][3]).suffix == ".dmg"
        first_staple = next(i for i, call in enumerate(fake_packaging) if "staple" in call)
        image_creation = next(i for i, call in enumerate(fake_packaging) if call[:2] == ("/usr/bin/hdiutil", "create"))
        assert first_staple < image_creation
    else:
        assert not any("notarytool" in call or "stapler" in call for call in fake_packaging)


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
