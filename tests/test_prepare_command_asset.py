"""Release archive preparation fails closed before installer signing."""
import base64
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_command_asset_tested", ROOT / ".github/scripts/prepare_command_asset.py")
preparer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparer)


@pytest.fixture
def synthetic(monkeypatch):
    contents = {"bin/mcpme-command-reap": b"Synthetic first helper",
                "bin/mcpme-command-resource-reap": b"Synthetic second helper"}
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(contents.items()):
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o700
                archive.addfile(member, io.BytesIO(data))
    data = buffer.getvalue()
    value = {"archive": {"format": "tar-gzip", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "artifacts": {name: {"size": len(contents), "mode": 0o700, "sha256": hashlib.sha256(contents).hexdigest()}
            for name, contents in contents.items()}}
    monkeypatch.setattr(preparer.signed_command_helpers, "manifest", lambda: value)
    signatures = []
    monkeypatch.setattr(preparer.signed_command_helpers, "_signature", lambda path, certificate: signatures.append(path))
    monkeypatch.setenv(preparer.ENVIRONMENT, base64.b64encode(data).decode("ascii"))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return SimpleNamespace(data=data, value=value, signatures=signatures)


@pytest.mark.parametrize("encoded", [None, ""])
def test_missing_archive_fails_without_output(tmp_path, monkeypatch, capsys, encoded):
    if encoded is None:
        monkeypatch.delenv(preparer.ENVIRONMENT, raising=False)
    else:
        monkeypatch.setenv(preparer.ENVIRONMENT, encoded)
    output = tmp_path / "assets/command.tar.gz"
    assert preparer.main(["--output", str(output)]) == 1
    assert capsys.readouterr() == ("", "command_signed_asset_missing\n")
    assert not output.parent.exists()


@pytest.mark.parametrize("encoded", ["%%%", "\u00e1", "YQ==\n", "YQ==="])
def test_invalid_encoding_is_not_admitted(synthetic, tmp_path, monkeypatch, capsys, encoded):
    monkeypatch.setenv(preparer.ENVIRONMENT, encoded)
    monkeypatch.setattr(preparer.signed_command_helpers, "admit_archive", lambda *args, **kwargs: pytest.fail("invalid input admitted"))
    output = tmp_path / "assets/command.tar.gz"
    assert preparer.main(["--output", str(output)]) == 1
    assert capsys.readouterr() == ("", "command_signed_asset_invalid\n")
    assert not output.parent.exists()


def test_encoded_input_bound_precedes_decoding(synthetic, tmp_path, monkeypatch):
    maximum = 4 * ((synthetic.value["archive"]["bytes"] + 2) // 3)
    monkeypatch.setenv(preparer.ENVIRONMENT, "A" * (maximum + 1))
    monkeypatch.setattr(preparer.base64, "b64decode", lambda *args, **kwargs: pytest.fail("oversized input decoded"))
    with pytest.raises(preparer.CommandAssetError, match="^command_signed_asset_invalid$"):
        preparer.prepare(tmp_path / "archive.tar.gz")


@pytest.mark.parametrize("fault", ["bytes", "sha256"])
def test_archive_identity_mismatch_fails_before_staging(synthetic, tmp_path, monkeypatch, fault):
    if fault == "bytes":
        synthetic.value["archive"][fault] += 1
    else:
        synthetic.value["archive"][fault] = "0" * 64
    monkeypatch.setattr(preparer.signed_command_helpers, "admit_archive", lambda *args, **kwargs: pytest.fail("mismatched input admitted"))
    output = tmp_path / "assets/archive.tar.gz"
    with pytest.raises(preparer.CommandAssetError, match="^command_signed_asset_invalid$"):
        preparer.prepare(output)
    assert not output.parent.exists()


@pytest.mark.parametrize("fault", ["admission", "signature"])
def test_failed_admission_or_signature_never_publishes(synthetic, tmp_path, monkeypatch, capsys, fault):
    def fail(*args, **kwargs):
        raise ValueError("Private admission diagnostics")
    target = "admit_archive" if fault == "admission" else "_signature"
    monkeypatch.setattr(preparer.signed_command_helpers, target, fail)
    output = tmp_path / "assets/archive.tar.gz"
    assert preparer.main(["--output", str(output)]) == 1
    assert capsys.readouterr() == ("", "command_signed_asset_invalid\n")
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["regular", "directory", "symlink", "dangling"])
def test_existing_output_is_preserved(synthetic, tmp_path, kind):
    output = tmp_path / "archive.tar.gz"
    held = tmp_path / "held"
    held.write_bytes(b"Existing producer input")
    if kind == "regular":
        output.write_bytes(held.read_bytes())
    elif kind == "directory":
        output.mkdir()
    else:
        output.symlink_to(held if kind == "symlink" else tmp_path / "absent")
    before = output.lstat()
    with pytest.raises(preparer.CommandAssetError, match="^command_signed_asset_invalid$"):
        preparer.prepare(output)
    after = output.lstat()
    assert (before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) == (
        after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    assert held.read_bytes() == b"Existing producer input"
    assert not synthetic.signatures


def test_output_created_during_admission_is_never_overwritten(synthetic, tmp_path, monkeypatch):
    output = tmp_path / "archive.tar.gz"
    original = preparer.signed_command_helpers.admit_archive
    def admit(*args, **kwargs):
        result = original(*args, **kwargs)
        output.write_bytes(b"Concurrent producer output")
        return result
    monkeypatch.setattr(preparer.signed_command_helpers, "admit_archive", admit)
    with pytest.raises(preparer.CommandAssetError, match="^command_signed_asset_invalid$"):
        preparer.prepare(output)
    assert output.read_bytes() == b"Concurrent producer output"
    assert sorted(path.name for path in tmp_path.iterdir()) == [output.name]


def test_valid_archive_is_admitted_in_private_stage_then_published(synthetic, tmp_path, monkeypatch, capsys):
    output = tmp_path / "assets/archive.tar.gz"
    original = preparer.signed_command_helpers.admit_archive
    observed = []
    def admit(archive, *, destination):
        assert not output.exists() and not destination.exists()
        assert archive.parent == destination.parent
        assert archive.parent.parent == output.parent.resolve()
        assert archive.parent.stat().st_uid == os.getuid()
        assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert archive.read_bytes() == synthetic.data
        observed.append((archive, destination))
        return original(archive, destination=destination)
    monkeypatch.setattr(preparer.signed_command_helpers, "admit_archive", admit)
    assert preparer.main(["--output", str(output)]) == 0
    assert capsys.readouterr() == ("command_signed_asset_ready\n", "")
    assert len(observed) == 1 and len(synthetic.signatures) == 2
    assert output.read_bytes() == synthetic.data
    assert stat.S_IMODE(output.stat().st_mode) == 0o600 and output.stat().st_nlink == 1
    assert sorted(output.parent.iterdir()) == [output]


def test_github_output_uses_canonical_path(synthetic, tmp_path, monkeypatch, capsys):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    output = alias / "archive.tar.gz"
    github_output = tmp_path / "github-output"
    github_output.write_text("existing=value\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    assert preparer.main(["--output", str(output)]) == 0
    assert capsys.readouterr() == ("command_signed_asset_ready\n", "")
    assert github_output.read_text() == "existing=value\npath=" + str(output.resolve()) + "\n"
    assert output.read_bytes() == synthetic.data


def test_release_workflow_admits_fixed_archive_before_tests_and_credentials():
    workflow = (ROOT / ".github/workflows/release-dmg.yml").read_text()
    assert workflow.index("Scan history before") < workflow.index("Prepare reviewed signed command helpers")
    assert workflow.index("Prepare reviewed signed command helpers") < workflow.index("Run tests before")
    assert workflow.index("Run tests before") < workflow.index("Import temporary Apple credentials")
    preparation = workflow.split("Prepare reviewed signed command helpers", 1)[1].split("Run tests before", 1)[0]
    assert "id: command_asset" in preparation
    assert "MCPME_COMMAND_SIGNED_ASSET_BASE64: ${{ secrets.MCPME_COMMAND_SIGNED_ASSET_BASE64 }}" in preparation
    assert '.github/scripts/prepare_command_asset.py --output "$RUNNER_TEMP/mcpme-release-assets/command-native-v1.tar.gz"' in preparation
    build = workflow.split("Build, sign, notarize, and verify DMG", 1)[1].split("Remove temporary Apple credentials", 1)[0]
    assert "COMMAND_SIGNED_ASSET: ${{ steps.command_asset.outputs.path }}" in build
    assert '--command-signed-asset "$COMMAND_SIGNED_ASSET"' in build
