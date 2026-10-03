"""Compatibility entrypoint for the versioned external JevK5 generator."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "templates/jevk5-llamacpp-v1/prepare.py"), run_name="__main__")
