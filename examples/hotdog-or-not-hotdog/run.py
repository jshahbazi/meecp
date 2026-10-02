"""Start from this folder, without relying on the shell's current directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from app import main

    status = main()
except Exception:
    print("App unavailable. Run Setup.command in this folder and try again.")
    status = 1
raise SystemExit(status)
