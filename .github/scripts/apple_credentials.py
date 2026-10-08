"""Prepare an ephemeral signing keychain without exposing credentials in logs."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shlex
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
SEARCH_LIST_FILENAME = "original-keychain-search-list.json"


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def capture(*arguments: str) -> str:
    result = subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=120)
    return result.stdout


def workspace() -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp or not Path(runner_temp).is_absolute():
        raise ValueError("runner_temp_required")
    return Path(runner_temp) / "meecp-signing"


def write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


def save_search_list(root: Path) -> list[str]:
    original = shlex.split(capture("/usr/bin/security", "list-keychains", "-d", "user"))
    write_private(root / SEARCH_LIST_FILENAME, json.dumps(original).encode("utf-8"))
    return original


def read_search_list(root: Path) -> list[str]:
    original = json.loads((root / SEARCH_LIST_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(original, list) or any(not isinstance(path, str) for path in original):
        raise ValueError("keychain_search_list_invalid")
    return original


def register_keychain(root: Path, keychain: Path) -> None:
    state = root / SEARCH_LIST_FILENAME
    original = read_search_list(root) if state.exists() else save_search_list(root)
    # codesign needs the imported keychain in the user search list even when
    # --keychain points at it explicitly. Preserve every original entry/order.
    run("/usr/bin/security", "list-keychains", "-d", "user", "-s", str(keychain), *original)


def preflight_signing(keychain: Path) -> None:
    root = keychain.parent
    source = root / "signing-probe.c"
    binary = root / "signing-probe"
    try:
        write_private(source, b"int main(void) { return 0; }\n")
        run("/usr/bin/xcrun", "clang", str(source), "-o", str(binary))
        # This checks private-key access locally before the lengthy app build.
        # Release signatures receive Apple's timestamp in the installer builder.
        run("/usr/bin/codesign", "--force", "--sign", os.environ["MACOS_SIGNING_IDENTITY"],
            "--keychain", str(keychain), "--options", "runtime", "--timestamp=none", str(binary))
        run("/usr/bin/codesign", "--verify", "--strict", str(binary))
    finally:
        source.unlink(missing_ok=True)
        binary.unlink(missing_ok=True)


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
    # Save this before create-keychain so cleanup also handles partial setup.
    save_search_list(root)
    run("/usr/bin/security", "create-keychain", "-p", password, str(keychain))
    run("/usr/bin/security", "set-keychain-settings", "-lut", "3600", str(keychain))
    run("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
    run("/usr/bin/security", "import", str(certificate), "-k", str(keychain),
        "-P", os.environ["MACOS_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign")
    run("/usr/bin/security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
        "-s", "-k", password, str(keychain))
    register_keychain(root, keychain)
    preflight_signing(keychain)
    run("/usr/bin/xcrun", "notarytool", "store-credentials", "meecp-notary",
        "--key", str(api_key), "--key-id", os.environ["APPLE_API_KEY_ID"],
        "--issuer", os.environ["APPLE_API_ISSUER_ID"], "--keychain", str(keychain))
    certificate.unlink()
    api_key.unlink()


def cleanup() -> None:
    root = workspace()
    if not root.exists():
        return
    keychain = root / "signing.keychain-db"
    error = None
    try:
        if (root / SEARCH_LIST_FILENAME).exists():
            try:
                original = read_search_list(root)
                run("/usr/bin/security", "list-keychains", "-d", "user", "-s", *original)
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = failure
        if keychain.exists():
            try:
                run("/usr/bin/security", "delete-keychain", str(keychain))
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = error or failure
    finally:
        shutil.rmtree(root)
    if error is not None:
        raise error


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
