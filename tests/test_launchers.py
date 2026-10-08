"""Launch product entrypoints from hostile working directories."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


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


def test_native_package_launcher_safe_imports_and_argument_forwarding(tmp_path):
    root = tmp_path / "package"
    (root / "bin").mkdir(parents=True)
    (root / "python/bin").mkdir(parents=True)
    (root / "python/bin/python3.12").symlink_to(sys.executable)
    _package(root, "meecp_jevk5", PROGRAM)
    launcher = root / "bin/jevk5-engine"
    subprocess.run(["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "scripts/jevk5-package-launcher.c"), "-o", str(launcher)], check=True, capture_output=True)
    result = subprocess.run([str(launcher), "--fixture", "argument with spaces"],
                            cwd=_hostile(tmp_path, "meecp_jevk5"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "trusted-dependency" in result.stdout
    assert "['engine', '--fixture', 'argument with spaces']" in result.stdout
