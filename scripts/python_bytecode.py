"""Remove disposable source-backed bytecode before inventorying Python payloads."""
from pathlib import Path
from importlib.util import source_from_cache
import os


def remove_source_bytecode(root: Path) -> None:
    # Do not dereference runtime symlinks or remove sourceless distributions.
    for directory, dirs, names in os.walk(root, topdown=False, followlinks=False):
        parent = Path(directory)
        for name in names:
            path = parent / name
            if path.suffix != ".pyc" or path.is_symlink():
                continue
            try:
                source = (Path(source_from_cache(str(path)))
                          if parent.name == "__pycache__" else path.with_suffix(".py"))
            except ValueError:
                continue
            if source.is_file() and not source.is_symlink():
                path.unlink()
        for name in dirs:
            path = parent / name
            if name == "__pycache__" and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
