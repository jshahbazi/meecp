from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

from mcpme.pi_environment import copy_regular_tree, extract_archive, extract_regular_archive, prepare_environment, stage_trust_bundle
from mcpme.pi_tools import PiToolBroker
from mcpme.preparation_contracts import PreparationError
from test_preparation_processes import group_reaper


def archive_fixture(path, entries):
    with tarfile.open(path, "w:gz") as output:
        for name, value, kind in entries:
            item = tarfile.TarInfo(name)
            if kind == "file":
                item.size = len(value)
                item.mode = 0o755
                output.addfile(item, io.BytesIO(value))
            elif kind == "directory":
                item.type = tarfile.DIRTYPE
                output.addfile(item)
            else:
                item.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
                item.linkname = value
                output.addfile(item)


def test_tar_materializes_executable_and_internal_symlink_chain(tmp_path):
    archive = tmp_path / "runtime.tgz"
    archive_fixture(archive, [("runtime/bin/tool", b"runtime", "file"),
        ("runtime/tool", "bin/tool", "symlink"), ("runtime/alias", "tool", "symlink"),
        ("runtime/hard", "runtime/bin/tool", "hardlink")])
    result = extract_regular_archive(archive, tmp_path / "tree", strip_components=1)
    assert result["archive_sha256"] and result["bytes"] == 4 * len(b"runtime")
    for name in ("bin/tool", "tool", "alias", "hard"):
        path = tmp_path / "tree" / name
        assert path.read_bytes() == b"runtime" and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o755 and path.stat().st_nlink == 1


def test_zip_materializes_internal_link_and_preserves_license(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("runtime/LICENSE", "synthetic license")
        link = zipfile.ZipInfo("runtime/alias")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "LICENSE")
    extract_regular_archive(archive, tmp_path / "tree", strip_components=1)
    assert (tmp_path / "tree/alias").read_text() == "synthetic license"
    assert not (tmp_path / "tree/alias").is_symlink()


@pytest.mark.parametrize("entries", [
    [("../outside", b"unsafe", "file")], [("/absolute", b"unsafe", "file")],
    [("runtime/link", "../../outside", "symlink")],
    [("runtime/link", "/private/outside", "symlink")],
    [("runtime/link", "missing", "symlink")],
    [("runtime/a", "b", "symlink"), ("runtime/b", "a", "symlink")],
    [("runtime/file", b"first", "file"), ("runtime/file", b"second", "file")],
])
def test_tar_rejects_escape_missing_links_cycles_and_duplicate_paths(tmp_path, entries):
    archive = tmp_path / "unsafe.tgz"
    archive_fixture(archive, entries)
    with pytest.raises(PreparationError):
        extract_regular_archive(archive, tmp_path / "tree", strip_components=1)
    assert not (tmp_path.parent / "outside").exists()


def test_copy_regular_tree_closes_internal_directory_links(tmp_path):
    source = tmp_path / "source"
    (source / "real").mkdir(parents=True)
    (source / "real/license").write_text("license")
    (source / "alias").symlink_to("real", target_is_directory=True)
    copy_regular_tree(source, tmp_path / "copy")
    assert (tmp_path / "copy/alias/license").read_text() == "license"
    assert not (tmp_path / "copy/alias").is_symlink()
    (source / "escape").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_text("private")
    with pytest.raises(PreparationError, match="external_link"):
        copy_regular_tree(source, tmp_path / "unsafe-copy")


def test_explicit_trust_staging_copies_certificate_blocks_only(tmp_path, monkeypatch):
    source = tmp_path / "configured-ca.pem"
    certificate = "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----"
    source.write_text(certificate + "\n-----BEGIN PRIVATE KEY-----\nexcluded\n-----END PRIVATE KEY-----\n")
    source.chmod(0o600)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(source))
    result = stage_trust_bundle(tmp_path / "staged.pem")
    assert result.read_text() == certificate + "\n"
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    source.chmod(0o666)
    with pytest.raises(PreparationError, match="trust_invalid"):
        stage_trust_bundle(tmp_path / "unsafe.pem")


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS ownership")
async def test_extract_publishes_only_into_unoccupied_private_target(tmp_path, group_reaper):
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    archive_fixture(broker.workspace / "runtime.tgz", [("runtime/tool", b"tool", "file")])
    try:
        result = await extract_archive(broker, {"archive_path": "runtime.tgz", "target_path": "tools",
            "strip_components": 1})
        assert result["success"] and (broker.workspace / "tools/tool").read_bytes() == b"tool"
        repeat = await extract_archive(broker, {"archive_path": "runtime.tgz", "target_path": "tools"})
        assert repeat == {"success": False, "output": "pi_environment_target_exists"}
        assert not list(broker.workspace.glob(".mcpme-extract-*"))
    finally:
        await broker.close()


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS ownership")
async def test_prepared_node_environment_has_private_runtime_npm_and_notices(tmp_path, group_reaper):
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper)
    host = tmp_path.resolve() / "host/pi-runtime"
    (host / "bin").mkdir(parents=True)
    (host / "bin/node").write_bytes(b"synthetic node")
    (host / "npm/bin").mkdir(parents=True)
    (host / "npm/bin/npm-cli.js").write_text("synthetic npm")
    (host / "npm/LICENSE").write_text("npm license")
    (host / "licenses").mkdir()
    (host / "licenses/NODE-LICENSE.txt").write_text("node license")
    (host / "provenance.json").write_text('{"node":"synthetic"}')
    try:
        result = await prepare_environment(broker, {"runtime": "node", "target_path": "environment"}, host.parent)
        assert result["success"], result
        metadata = json.loads(result["output"])
        assert Path(metadata["node_path"]).read_bytes() == b"synthetic node"
        assert Path(metadata["npm_cli_path"]).is_file()
        assert (Path(metadata["runtime_path"]) / "licenses/NODE-LICENSE.txt").is_file()
        assert "git" in metadata["system_toolchains"]
    finally:
        await broker.close()
