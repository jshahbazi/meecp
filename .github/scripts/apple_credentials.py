"""Prepare an ephemeral signing keychain without exposing credentials in logs."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


REQUIRED = (
    "MACOS_CERTIFICATE_P12_BASE64",
    "MACOS_CERTIFICATE_PASSWORD",
    "MACOS_SIGNING_IDENTITY",
    "APPLE_API_KEY_P8_BASE64",
    "APPLE_API_KEY_ID",
    "APPLE_API_ISSUER_ID",
)


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def workspace() -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp or not Path(runner_temp).is_absolute():
        raise ValueError("runner_temp_required")
    return Path(runner_temp) / "mcpme-signing"


def write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


def setup() -> None:
    if any(not os.environ.get(name) for name in REQUIRED):
        raise ValueError("signing_credentials_missing")
    if not os.environ["MACOS_SIGNING_IDENTITY"].startswith("Developer ID Application:"):
        raise ValueError("developer_id_application_required")
    root = workspace()
    root.mkdir(mode=0o700)
    keychain = root / "signing.keychain-db"
    certificate = root / "certificate.p12"
    api_key = root / "AuthKey.p8"
    password = secrets.token_urlsafe(48)
    write_private(certificate, base64.b64decode(os.environ[REQUIRED[0]], validate=True))
    write_private(api_key, base64.b64decode(os.environ[REQUIRED[3]], validate=True))
    run("/usr/bin/security", "create-keychain", "-p", password, str(keychain))
    run("/usr/bin/security", "set-keychain-settings", "-lut", "3600", str(keychain))
    run("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
    run("/usr/bin/security", "import", str(certificate), "-k", str(keychain),
        "-P", os.environ["MACOS_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign")
    run("/usr/bin/security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
        "-s", "-k", password, str(keychain))
    run("/usr/bin/xcrun", "notarytool", "store-credentials", "mcpme-notary",
        "--key", str(api_key), "--key-id", os.environ["APPLE_API_KEY_ID"],
        "--issuer", os.environ["APPLE_API_ISSUER_ID"], "--keychain", str(keychain))
    certificate.unlink()
    api_key.unlink()


def cleanup() -> None:
    root = workspace()
    if not root.exists():
        return
    keychain = root / "signing.keychain-db"
    try:
        if keychain.exists():
            run("/usr/bin/security", "delete-keychain", str(keychain))
    finally:
        shutil.rmtree(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "cleanup"))
    arguments = parser.parse_args()
    try:
        setup() if arguments.action == "setup" else cleanup()
    except (OSError, ValueError, subprocess.SubprocessError):
        print("apple_credentials_" + arguments.action + "_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
