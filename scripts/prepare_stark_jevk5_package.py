"""Compatibility entrypoint for the versioned experimental Stark JevK5 package."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "templates/jevk5-stark-v1/prepare.py"), run_name="__main__")
