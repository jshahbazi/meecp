"""Launch product entrypoints from hostile working directories."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _package(root: Path, name: str, text: str) -> None:
    package = root / "backend" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (root / "backend/launcher_dependency.py").write_text("VALUE = 'trusted-dependency'\n")
    (package / "__main__.py").write_text(text)


def _hostile(tmp_path: Path, module: str) -> Path:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / (module + ".py")).write_text("raise RuntimeError('cwd shadow')\n")
    (hostile / "launcher_dependency.py").write_text("raise RuntimeError('cwd dependency shadow')\n")
    return hostile


PROGRAM = "import sys, launcher_dependency; print(launcher_dependency.VALUE); print(sys.argv[1:])"


def test_source_launcher_ignores_hostile_cwd(tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/meecp", root / "scripts/meecp")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python3").symlink_to(sys.executable)
    _package(root, "meecp", PROGRAM)
    result = subprocess.run([str(root / "scripts/meecp"), "--help"], cwd=_hostile(tmp_path, "meecp"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "trusted-dependency" in result.stdout and "--help" in result.stdout


def test_generated_launcher_relocation_ignores_hostile_cwd(tmp_path):
    spec = importlib.util.spec_from_file_location("host_builder", ROOT / "scripts/build_meecp_host.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    root = tmp_path / "original host"
    (root / "bin").mkdir(parents=True)
    (root / "python/bin").mkdir(parents=True)
    (root / "python/bin/python3.12").symlink_to(sys.executable)
    _package(root, "meecp", PROGRAM)
    builder.write_launcher(root / "bin/meecp")
    relocated = tmp_path / "relocated host"
    root.rename(relocated)
    result = subprocess.run([str(relocated / "bin/meecp"), "--help"], cwd=_hostile(tmp_path, "meecp"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "trusted-dependency" in result.stdout and "--help" in result.stdout


def test_bundled_legacy_launcher_ignores_hostile_cwd(tmp_path):
    app = tmp_path / "JevK5.app"
    resources = app / "Contents/Resources"
    (resources / "python/bin").mkdir(parents=True)
    (resources / "backend").mkdir()
    (resources / "vendor").mkdir()
    (resources / "python/site-packages").mkdir(parents=True)
    (resources / "python/bin/python3.12").symlink_to(sys.executable)
    launcher = app / "Contents/MacOS/jevk5-mcp"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/jevk5-mcp", launcher); launcher.chmod(0o755)
    _package(resources, "jevk5_app", PROGRAM)
    result = subprocess.run([str(launcher)], cwd=_hostile(tmp_path, "jevk5_app"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "trusted-dependency" in result.stdout and "bridge" in result.stdout


@pytest.mark.parametrize("source,module,executable", [
    ("scripts/jevk5-package-launcher.c", "meecp_jevk5", "jevk5-engine"),
    ("scripts/textgen-package-launcher.c", "meecp_textgen", "textgen-engine"),
    ("templates/text-generate-v1/launcher.c", "meecp_textgen", "textgen-engine"),
])
def test_native_package_launcher_safe_imports_and_argument_forwarding(tmp_path, source, module, executable):
    root = tmp_path / "package"
    (root / "bin").mkdir(parents=True)
    (root / "python/bin").mkdir(parents=True)
    (root / "python/bin/python3.12").symlink_to(sys.executable)
    _package(root, module, "import sys; assert sys.flags.dont_write_bytecode\n" + PROGRAM)
    launcher = root / "bin" / executable
    subprocess.run(["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / source), "-o", str(launcher)], check=True, capture_output=True)
    result = subprocess.run([str(launcher), "--fixture", "argument with spaces"],
                            cwd=_hostile(tmp_path, module), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "trusted-dependency" in result.stdout
    assert "['engine', '--fixture', 'argument with spaces']" in result.stdout
    assert not list(root.rglob("*.pyc"))


def test_payload_removes_only_source_backed_bytecode(tmp_path):
    import py_compile
    spec = importlib.util.spec_from_file_location("bytecode_cleanup", ROOT / "scripts/python_bytecode.py")
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    cached = Path(py_compile.compile(str(source), doraise=True))
    legacy = source.with_suffix(".pyc")
    legacy.write_bytes(cached.read_bytes())
    sourceless = tmp_path / "sourceless.pyc"
    sourceless.write_bytes(legacy.read_bytes())
    external = tmp_path.parent / (tmp_path.name + "-external.pyc")
    external.write_bytes(b"retained")
    link = tmp_path / "external.pyc"
    link.symlink_to(external)
    cleanup.remove_source_bytecode(tmp_path)
    assert source.exists() and sourceless.exists() and link.is_symlink()
    assert external.read_bytes() == b"retained"
    assert not cached.exists() and not legacy.exists()
    assert not cached.parent.exists()


def test_stark_launcher_disables_bytecode_and_preserves_config_descriptor(tmp_path):
    root = tmp_path / "package"
    (root / "bin").mkdir(parents=True)
    (root / "python/bin").mkdir(parents=True)
    (root / "python/bin/python3.12").symlink_to(sys.executable)
    _package(root, "meecp_stark", "import sys; assert sys.flags.dont_write_bytecode\n" + PROGRAM)
    launcher = root / "bin/jevk5-stark-engine"
    subprocess.run(["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "templates/jevk5-stark-v1/launcher.c"), "-o", str(launcher)],
                   check=True, capture_output=True)
    result = subprocess.run([str(launcher), "--meecp-config-fd", "3"],
                            cwd=_hostile(tmp_path, "meecp_stark"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "['engine', '--meecp-config-fd', '3']" in result.stdout
    assert not list(root.rglob("*.pyc"))
