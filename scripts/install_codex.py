"""Register this bundle's stdio adapter in the existing local Codex config."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path, default=root / "dist/JevK5.app")
args = parser.parse_args()
helper = args.app.resolve() / "Contents/MacOS/jevk5-mcp"
if not helper.is_file():
    raise SystemExit("Build the app before registering it with Codex")
probe = subprocess.run(["codex", "mcp", "get", "jevk5", "--json"], capture_output=True, text=True)
if probe.returncode == 0:
    existing = json.loads(probe.stdout)
    if existing["transport"].get("command") != str(helper):
        raise SystemExit("Another jevk5 server is registered; choose its configuration explicitly")
else:
    subprocess.run(["codex", "mcp", "add", "jevk5", "--", str(helper)], check=True)

# Preserve all other TOML text and existing user-selected timeout values.
configuration = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
original = configuration.read_text()
parsed = tomllib.loads(original)
server = parsed["mcp_servers"]["jevk5"]
additions = []
for key, value in (("startup_timeout_sec", 30), ("tool_timeout_sec", 180)):
    if key not in server:
        additions.append(f"{key} = {value}\n")
if additions:
    match = re.search(r"^\[mcp_servers\.jevk5\]\s*\n", original, re.MULTILINE)
    if not match:
        raise SystemExit("Codex wrote an unexpected configuration layout")
    updated = original[:match.end()] + "".join(additions) + original[match.end():]
    tomllib.loads(updated)
    descriptor, temporary = tempfile.mkstemp(prefix=".jevk5-config-", dir=configuration.parent)
    try:
        os.fchmod(descriptor, configuration.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "w") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        if configuration.read_text() != original:
            raise SystemExit("Codex configuration changed concurrently; registration was preserved, timeout update skipped")
        os.replace(temporary, configuration)
    finally:
        Path(temporary).unlink(missing_ok=True)
verified = subprocess.run(["codex", "mcp", "get", "jevk5", "--json"], check=True, capture_output=True, text=True)
result = json.loads(verified.stdout)
if result["transport"].get("command") != str(helper) or not result["enabled"]:
    raise SystemExit("Codex registration verification failed")
print("JevK5 registered and verified in local Codex configuration")
