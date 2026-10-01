"""Build the host process reaper, with compiler diagnostics confined to this build."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "scripts/mcpme-reap.c"), "-o", str(destination)], check=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/mcpme-reap")
    args = parser.parse_args()
    print(build(args.output.absolute()))
