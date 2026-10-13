"""The bundled implementation must match both hashed source namespaces."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("textgen_backend_bundler", ROOT / "scripts/bundle_textgen_assets.py")
bundler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundler)


def test_bundle_preserves_current_modules_and_distinct_legacy_shims(tmp_path):
    destination = tmp_path / "backend"
    bundler.bundle_backend(destination)
    for name in ("meecp_textgen", "mcpme_textgen"):
        sources = {path.name: path.read_bytes() for path in (ROOT / "backend" / name).glob("*.py")}
        copies = {path.name: path.read_bytes() for path in (destination / name).glob("*.py")}
        assert copies == sources
    # The legacy entry point imports the current implementation. Copying the
    # current entry point over it changes the source closure's admitted digest.
    assert (destination / "mcpme_textgen/__main__.py").read_bytes() != (destination / "meecp_textgen/__main__.py").read_bytes()
    bundler.bundle_backend(destination)


def test_existing_substituted_legacy_entrypoint_is_rejected(tmp_path):
    destination = tmp_path / "backend"
    bundler.bundle_backend(destination)
    (destination / "mcpme_textgen/__main__.py").write_bytes((destination / "meecp_textgen/__main__.py").read_bytes())
    with pytest.raises(ValueError, match="^textgen_backend_identity_mismatch$"):
        bundler.bundle_backend(destination)
