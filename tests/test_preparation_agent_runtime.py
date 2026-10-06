"""SDK overlay uses only exact bundled identities and retained real notices."""
import hashlib
import json
from pathlib import Path

import pytest

from mcpme.preparation_agent_runtime import complete_agent_runtime
from mcpme.preparation_builder import _dependency_notices
from mcpme.preparation_contracts import PreparationError


def distribution(site, name, version, *, requirements=(), notice=True):
    root = site / (name + "-" + version + ".dist-info")
    root.mkdir(parents=True)
    package = site / name; package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('metadata inspection must never import this package')\n")
    metadata = "Name: " + name + "\nVersion: " + version + "\n"
    metadata += "".join("Requires-Dist: " + value + "\n" for value in requirements)
    (root / "METADATA").write_text(metadata)
    if notice:
        (root / "LICENSE").write_text("Synthetic original test permission.\n")
    files = [*root.rglob("*"), *package.rglob("*")]
    (root / "RECORD").write_text("".join(path.relative_to(site).as_posix() + ",,\n" for path in files if path.is_file()))
    with (root / "RECORD").open("a") as stream:
        stream.write((root / "RECORD").relative_to(site).as_posix() + ",,\n")
    return root


def fixture(tmp_path):
    host, output, source = (tmp_path / name for name in ("host", "output", "source"))
    host_site, site = host / "python/site-packages", output / "python/site-packages"
    host_site.mkdir(parents=True); site.mkdir(parents=True); source.mkdir()
    distribution(host_site, "mcp", "1.30.0", requirements=("utility==1.0",))
    distribution(host_site, "utility", "1.0")
    distribution(site, "application", "2.0", notice=False)
    (source / "LICENSE-APPLICATION").write_text("Original application dependency license.\n")
    artifacts = {path.relative_to(host).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in host_site.rglob("*") if path.is_file()}
    (host / "build.json").write_text(json.dumps({"artifact_sha256": artifacts}))
    return host, output, source


def test_exact_sdk_overlay_preserves_app_and_retains_notice_provenance(tmp_path):
    host, output, source = fixture(tmp_path)
    application = output / "python/site-packages/application/__init__.py"
    before = application.read_bytes()
    origin = complete_agent_runtime(host, output, source, lambda: None)
    assert application.read_bytes() == before
    assert {item["name"] for item in origin["distributions"]} == {"mcp", "utility"}
    assert origin["supplemented_notices"][0]["source"] == "LICENSE-APPLICATION"
    assert origin["supplemented_notices"][0]["origin"] == "original-source"
    _dependency_notices(output / "python/site-packages", output, lambda: None)
    notices = json.loads((output / "dependency-notices.json").read_text())
    assert {item["name"] for item in notices["distributions"]} == {"application", "mcp", "utility"}


def test_changed_bundled_sdk_and_version_conflicts_are_rejected(tmp_path):
    host, output, source = fixture(tmp_path)
    path = host / "python/site-packages/mcp/__init__.py"
    path.write_text("changed bytes")
    with pytest.raises(PreparationError, match="agent_sdk_closure_invalid"):
        complete_agent_runtime(host, output, source, lambda: None)
    other = tmp_path / "other"; other.mkdir()
    host, output, source = fixture(other)
    distribution(output / "python/site-packages", "utility", "2.0")
    with pytest.raises(PreparationError, match="agent_sdk_dependency_conflict"):
        complete_agent_runtime(host, output, source, lambda: None)


def test_missing_notice_still_fails_and_sdk_symlink_is_not_followed(tmp_path):
    host, output, source = fixture(tmp_path)
    (source / "LICENSE-APPLICATION").unlink()
    complete_agent_runtime(host, output, source, lambda: None)
    with pytest.raises(PreparationError, match="dependency_notice_required"):
        _dependency_notices(output / "python/site-packages", output, lambda: None)
    other = tmp_path / "other"; other.mkdir()
    host, output, source = fixture(other)
    target = host / "python/site-packages/mcp/__init__.py"
    outside = tmp_path / "outside.py"; outside.write_bytes(target.read_bytes())
    target.unlink(); target.symlink_to(outside)
    with pytest.raises(PreparationError):
        complete_agent_runtime(host, output, source, lambda: None)


def test_existing_supported_sdk_keeps_the_original_locked_dependencies(tmp_path):
    host, output, source = fixture(tmp_path)
    site = output / "python/site-packages"
    distribution(site, "mcp", "1.30.0", requirements=("utility>=1.0",))
    distribution(site, "utility", "2.0")
    before = {path.relative_to(site).as_posix(): path.read_bytes() for path in site.rglob("*") if path.is_file()}
    origin = complete_agent_runtime(host, output, source, lambda: None)
    assert origin["sdk_source"] == "original-lock" and origin["files"] == []
    assert all((site / name).read_bytes() == content for name, content in before.items())
    _dependency_notices(site, output, lambda: None)
