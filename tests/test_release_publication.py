"""Release publication fails closed before exposing unverified downloads."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


COMMIT = "a" * 40
SUBMISSION = "01234567-89ab-cdef-0123-456789abcdef"
VERSION = "0.1.0"
TAG = "v" + VERSION
RELEASE_ID = 401560592
DRAFT_TAG = "untagged-ab9043a45ed932b9fad7"
PRIVATE = "synthetic-authentication-secret"


@pytest.fixture
def publisher(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / ".github/scripts/publish_release.py"
    spec = importlib.util.spec_from_file_location("release_publication", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    monkeypatch.setattr(module, "ROOT", source)
    monkeypatch.setenv("GITHUB_REPOSITORY", module.REPOSITORY)
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return module


@pytest.fixture
def assets(publisher, tmp_path):
    directory = tmp_path / "dist"
    directory.mkdir()
    app = {
        "signing": "developer_id", "hardened_runtime": True,
        "secure_timestamp": True, "notarization": "accepted",
        "notarization_ticket_stapled": True, "notary_submission_id": SUBMISSION,
        "source_commit": COMMIT, "source_tree_dirty": False,
    }
    for kind, checks in (("dmg", publisher.DMG_CHECKS), ("zip", publisher.ZIP_CHECKS)):
        binary = directory / f"mcpme-{VERSION}-arm64.{kind}"
        binary.write_bytes(b"synthetic signed " + kind.encode())
        receipt = {
            "schema_version": 1,
            "distribution": "notarized_drag_install_dmg" if kind == "dmg" else "notarized_app_zip",
            "notarization": "accepted", "notary_submission_id": SUBMISSION,
            "version": VERSION, "build": "1", "minimum_macos": "26.0", "architecture": "arm64",
            "app_build": app, "checks": dict.fromkeys(checks, True),
            "sha256": publisher.digest(binary), "bytes": binary.stat().st_size,
        }
        if kind == "dmg":
            receipt.update({"disk_image_signed": True, "notarization_ticket_stapled": True})
        else:
            receipt["contained_app_notarization_ticket_stapled"] = True
        binary.with_name(binary.name + ".build.json").write_text(json.dumps(receipt))
    return directory


def change_receipt(directory, kind, change):
    path = directory / f"mcpme-{VERSION}-arm64.{kind}.build.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def http_result(status, body=None, *, returncode=None):
    return subprocess.CompletedProcess(
        ["gh"], (0 if 200 <= status < 300 else 1) if returncode is None else returncode,
        stdout=f"HTTP/2.0 {status} Status\nContent-Type: application/json\n\n{json.dumps(body or {})}",
        stderr=PRIVATE,
    )


def install_github(monkeypatch, publisher, directory, *, remote_change=None, tag_change=None,
                   existing_draft=False):
    calls = []
    drafted = existing_draft
    public = False
    tagged = existing_draft
    draft_readback = False

    def run(*arguments):
        nonlocal drafted, public, tagged, draft_readback
        calls.append(arguments)
        if arguments[0] == "api":
            endpoint = next(value for value in arguments if value.startswith("repos/"))
            if "POST" in arguments:
                assert not tagged
                assert "ref=refs/tags/" + TAG in arguments and "sha=" + COMMIT in arguments
                tagged = True
                return http_result(201)
            if "PATCH" in arguments:
                assert endpoint == f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}"
                assert drafted and not public and draft_readback
                # gh's typed field emits a JSON boolean rather than string "false".
                assert "draft=false" in arguments
                assert ("--field" in arguments or "-F" in arguments)
                public = True
                return http_result(200)
            if "git/ref/tags/" in endpoint and tagged:
                value = {"ref": "refs/tags/" + TAG, "object": {"type": "commit", "sha": COMMIT}}
                if drafted and tag_change:
                    tag_change(value)
                return http_result(200, value)
            if not drafted:
                return http_result(404)
            if endpoint == publisher.release_endpoint(TAG) and not public:
                # GitHub's tag endpoint cannot resolve an unpublished release.
                return http_result(404)
            assert endpoint == f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}"
            remote = {
                "id": RELEASE_ID,
                "tag_name": TAG, "target_commitish": COMMIT,
                "draft": not public, "prerelease": False,
                "html_url": f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG if public else DRAFT_TAG}",
                "assets": [
                    {"name": path.name, "size": path.stat().st_size, "state": "uploaded",
                     "digest": "sha256:" + publisher.digest(path)}
                    for path in directory.iterdir()
                ],
            }
            if remote_change:
                remote_change(remote)
            if not public:
                draft_readback = True
            return http_result(200, remote)
        assert arguments[0] == "release"
        if arguments[1] == "create":
            assert not drafted
            assert "--draft" in arguments
            assert "--verify-tag" in arguments
            assert arguments[arguments.index("--target") + 1] == COMMIT
            assert arguments[arguments.index("--title") + 1] == "mcpme " + VERSION
            notes = Path(arguments[arguments.index("--notes-file") + 1]).read_text()
            assert "DMG:" in notes and "ZIP:" in notes and COMMIT in notes
            drafted = True
        elif arguments[1] == "upload":
            assert drafted and not public
            assert "--clobber" not in arguments
            names = {Path(argument).name for argument in arguments[5:]}
            assert names == {path.name for path in directory.iterdir()}
        elif arguments[1] == "view":
            assert drafted
            assert arguments[2] == TAG
            assert arguments[arguments.index("--repo") + 1] == publisher.GH_REPOSITORY
            assert arguments[arguments.index("--json") + 1] == "databaseId"
            return subprocess.CompletedProcess(["gh"], 0, json.dumps({"databaseId": RELEASE_ID}), PRIVATE)
        else:
            pytest.fail("unexpected mutation")
        return subprocess.CompletedProcess(["gh"], 0, PRIVATE, PRIVATE)

    monkeypatch.setattr(publisher, "command", run)
    return calls


def test_prepare_outputs_canonical_version_only_after_preflight(publisher, monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(publisher, "command", lambda *args: calls.append(args) or http_result(404))
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    publisher.prepare()
    assert output.read_text() == "version=0.1.0\ntag=v0.1.0\n"
    assert len(calls) == 2 and all(call[0] == "api" for call in calls)
    assert capsys.readouterr() == ("release_preflight_ready\n", "")


@pytest.mark.parametrize("version", ["01.0.0", "0.1", "0.1.0-alpha", "v0.1.0", "0.1.0\nunsafe"])
def test_invalid_version_has_no_github_or_output_writes(publisher, monkeypatch, tmp_path, version):
    (publisher.ROOT / "pyproject.toml").write_text('[project]\nversion = ' + json.dumps(version) + '\n')
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_project_version_invalid"):
        publisher.prepare()
    assert not output.exists()


@pytest.mark.parametrize("variable,value", [
    ("GITHUB_REPOSITORY", "someone-else/mcpme"), ("GITHUB_SHA", "main"),
    ("GITHUB_SHA", "A" * 40), ("GITHUB_SHA", "a" * 39),
])
def test_invalid_source_context_is_blocked(publisher, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError):
        publisher.prepare()


@pytest.mark.parametrize("existing", ["release", "tag"])
def test_existing_version_never_overwritten(publisher, monkeypatch, existing):
    calls = []

    def run(*arguments):
        calls.append(arguments)
        present = existing == "release" or "git/ref/tags/" in arguments[-1]
        return http_result(200, {"existing": True}) if present else http_result(404)

    monkeypatch.setattr(publisher, "command", run)
    with pytest.raises(publisher.PublicationError, match="already_exists"):
        publisher.prepare()
    assert all(call[0] == "api" for call in calls)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failures_are_not_treated_as_absent(publisher, monkeypatch, status, capsys):
    monkeypatch.setattr(publisher, "command", lambda *args: http_result(status))
    with pytest.raises(publisher.PublicationError, match="release_github_api_failed"):
        publisher.prepare()
    assert capsys.readouterr() == ("", "")


def test_transport_failure_without_http_response_fails_closed(publisher, monkeypatch):
    monkeypatch.setattr(publisher, "command", lambda *args: subprocess.CompletedProcess(["gh"], 1, "", PRIVATE))
    with pytest.raises(publisher.PublicationError, match="release_github_api_failed"):
        publisher.prepare()


@pytest.mark.parametrize("kind,change", [
    ("dmg", lambda receipt: receipt.update(notarization="rejected")),
    ("zip", lambda receipt: receipt["app_build"].update(notarization="not_requested")),
    ("dmg", lambda receipt: receipt["app_build"].update(signing="ad_hoc")),
    ("zip", lambda receipt: receipt["app_build"].update(source_commit="b" * 40)),
    ("dmg", lambda receipt: receipt["app_build"].update(source_tree_dirty=True)),
    ("zip", lambda receipt: receipt["app_build"].update(notarization_ticket_stapled=False)),
    ("dmg", lambda receipt: receipt["app_build"].update(hardened_runtime=False)),
    ("zip", lambda receipt: receipt["app_build"].update(secure_timestamp=False)),
    ("dmg", lambda receipt: receipt["checks"].update(copied_app_gatekeeper_assessment=False)),
    ("zip", lambda receipt: receipt["checks"].update(extracted_app_signature=False)),
    ("dmg", lambda receipt: receipt.update(disk_image_signed=False)),
    ("zip", lambda receipt: receipt.update(contained_app_notarization_ticket_stapled=False)),
])
def test_invalid_receipts_block_before_any_remote_write(publisher, assets, monkeypatch, kind, change):
    change_receipt(assets, kind, change)
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_asset_receipt_invalid"):
        publisher.publish(assets)
    assert not (assets / "SHA256SUMS.txt").exists()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("bytes", 1), ("version", "0.2.0")])
def test_receipt_hash_size_and_version_must_match(publisher, assets, monkeypatch, field, value):
    change_receipt(assets, "dmg", lambda receipt: receipt.update({field: value}))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_asset_receipt"):
        publisher.publish(assets)


def test_two_archives_must_describe_same_app(publisher, assets, monkeypatch):
    change_receipt(assets, "zip", lambda receipt: receipt["app_build"].update(extra_native_hash="different"))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_asset_app_mismatch"):
        publisher.publish(assets)


@pytest.mark.parametrize("unexpected", ["other.dmg", "debug.log"])
def test_unrelated_files_cannot_be_published(publisher, assets, unexpected):
    (assets / unexpected).write_text("unrelated")
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)


def test_missing_or_symlinked_assets_are_rejected(publisher, assets, tmp_path):
    binary = assets / f"mcpme-{VERSION}-arm64.zip"
    target = tmp_path / "outside.zip"
    binary.rename(target)
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)
    binary.symlink_to(target)
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)


def test_checksum_manifest_covers_downloads_and_receipts(publisher, assets):
    paths = publisher.validated_assets(assets, VERSION, COMMIT)
    manifest = (assets / "SHA256SUMS.txt").read_text().splitlines()
    assert len(paths) == 5 and len(manifest) == 4
    for line in manifest:
        checksum, filename = line.split("  ")
        assert checksum == publisher.digest(assets / filename)
    assert publisher.validated_assets(assets, VERSION, COMMIT) == paths


def test_existing_checksum_manifest_cannot_be_overwritten(publisher, assets):
    sums = assets / "SHA256SUMS.txt"
    sums.write_text("existing invalid checksum file")
    with pytest.raises(publisher.PublicationError, match="release_checksum_manifest_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)
    assert sums.read_text() == "existing invalid checksum file"


def test_publish_is_draft_until_exact_assets_and_hashes_are_verified(publisher, assets, monkeypatch, capsys):
    calls = install_github(monkeypatch, publisher, assets)
    assert publisher.publish(assets) == f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    mutation = next(index for index, call in enumerate(calls) if "PATCH" in call)
    assert any(call[:2] == ("release", "view") for call in calls[:mutation])
    assert any(f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}" in call
               for call in calls[:mutation])
    assert any(f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}" in call
               for call in calls[mutation + 1:])
    assert not any(call[:2] == ("release", "edit") for call in calls)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("change,diagnostic", [
    (lambda release: release["assets"][0].update(digest="sha256:" + "0" * 64), "digest_mismatch"),
    (lambda release: release["assets"][0].update(size=0), "digest_mismatch"),
    (lambda release: release["assets"].pop(), "asset_set_invalid"),
    (lambda release: release["assets"][0].update(name="unrelated.zip"), "asset_set_invalid"),
    (lambda release: release.update(target_commitish="main"), "metadata_invalid"),
    (lambda release: release.update(draft=False), "metadata_invalid"),
])
def test_failed_remote_verification_keeps_draft_unpublished(publisher, assets, monkeypatch, change, diagnostic):
    calls = install_github(monkeypatch, publisher, assets, remote_change=change)
    with pytest.raises(publisher.PublicationError, match=diagnostic):
        publisher.publish(assets)
    assert any(call[:2] == ("release", "create") for call in calls)
    assert not any("PATCH" in call or call[:2] == ("release", "edit") for call in calls)
    assert not any("--clobber" in call or "delete" in call for call in calls)


def test_changed_tag_commit_keeps_draft_unpublished(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets,
                           tag_change=lambda value: value["object"].update(sha="b" * 40))
    with pytest.raises(publisher.PublicationError, match="release_tag_commit_mismatch"):
        publisher.publish(assets)
    assert any(call[:2] == ("release", "create") for call in calls)
    assert not any("PATCH" in call or call[:2] == ("release", "edit") for call in calls)


def test_draft_can_have_an_untagged_url_and_unresolvable_tag_endpoint(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True)
    assert publisher.api(publisher.release_endpoint(TAG), missing_allowed=True) is None
    assert publisher.resolve_release_id(TAG) == RELEASE_ID
    paths = publisher.validated_assets(assets, VERSION, COMMIT)
    assert publisher.verified_release(TAG, COMMIT, paths, draft=True, release_id=RELEASE_ID) == (
        f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    )
    assert all("POST" not in call and "PATCH" not in call for call in calls)


@pytest.mark.parametrize("database_id", [None, 0, -1, True, "401560592"])
def test_resolved_release_id_must_be_a_positive_integer(publisher, monkeypatch, database_id):
    monkeypatch.setattr(publisher, "command", lambda *args: subprocess.CompletedProcess(
        ["gh"], 0, json.dumps({"databaseId": database_id}), PRIVATE,
    ))
    with pytest.raises(publisher.PublicationError):
        publisher.resolve_release_id(TAG)


@pytest.mark.parametrize("change", [
    lambda value: value.update(id=RELEASE_ID + 1),
    lambda value: value.update(html_url=f"https://github.com/someone-else/mcpme/releases/tag/{DRAFT_TAG}"),
    lambda value: value.update(html_url="https://github.com/jshahbazi/mcpme/releases/tag/untagged-not-hex"),
])
def test_wrong_release_id_or_untrusted_draft_url_blocks_publication(publisher, assets, monkeypatch, change):
    calls = install_github(monkeypatch, publisher, assets, remote_change=change)
    with pytest.raises(publisher.PublicationError, match="metadata_invalid"):
        publisher.publish(assets)
    assert not any("PATCH" in call for call in calls)


def test_resume_validated_draft_publishes_by_id_without_creating_or_uploading(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True)
    assert publisher.resume(assets, RELEASE_ID) == f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    assert sum("PATCH" in call for call in calls) == 1
    assert not any("POST" in call or call[:2] in (("release", "create"), ("release", "upload")) for call in calls)


@pytest.mark.parametrize("change,diagnostic", [
    (lambda value: value["assets"].pop(), "asset_set_invalid"),
    (lambda value: value["assets"][0].update(digest="sha256:" + "0" * 64), "digest_mismatch"),
    (lambda value: value.update(id=RELEASE_ID + 1), "metadata_invalid"),
    (lambda value: value.update(draft=False), "metadata_invalid"),
])
def test_resume_cannot_publish_incomplete_or_mismatched_draft(publisher, assets, monkeypatch, change, diagnostic):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True, remote_change=change)
    with pytest.raises(publisher.PublicationError, match=diagnostic):
        publisher.resume(assets, RELEASE_ID)
    assert not any("PATCH" in call or "POST" in call or call[0] == "release" for call in calls)


def test_resume_rejects_local_checksum_mismatch_before_github_access(publisher, assets, monkeypatch):
    (assets / "SHA256SUMS.txt").write_text("invalid downloaded checksum manifest")
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_checksum_manifest_invalid"):
        publisher.resume(assets, RELEASE_ID)


def test_resume_rejects_wrong_tag_commit_without_mutating_draft(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True,
                           tag_change=lambda value: value["object"].update(sha="b" * 40))
    with pytest.raises(publisher.PublicationError, match="release_tag_commit_mismatch"):
        publisher.resume(assets, RELEASE_ID)
    assert not any("PATCH" in call or "POST" in call for call in calls)


def test_github_command_capture_never_emits_private_output(publisher, monkeypatch, capsys):
    def run(arguments, **kwargs):
        assert arguments == ["gh", "release", "create", TAG]
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["check"] is False
        assert kwargs["env"]["GH_HOST"] == "github.com"
        return subprocess.CompletedProcess(arguments, 1, PRIVATE, PRIVATE)

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublicationError, match="release_github_command_failed") as raised:
        publisher.checked_command("release", "create", TAG)
    assert PRIVATE not in repr(raised.value)
    assert capsys.readouterr() == ("", "")


def test_github_timeout_does_not_retain_command_output(publisher, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh", PRIVATE], 120, output=PRIVATE, stderr=PRIVATE)

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublicationError) as raised:
        publisher.command("release", "create", TAG)
    assert PRIVATE not in repr(raised.value)
    assert raised.value.__cause__ is None
