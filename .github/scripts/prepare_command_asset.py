"""Prepare the fixed signed command archive without logging supplied bytes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import signed_command_helpers

ENVIRONMENT = "MCPME_COMMAND_SIGNED_ASSET_BASE64"


class CommandAssetError(ValueError):
    pass


def output_path(requested: Path) -> Path:
    if requested.is_symlink():
        raise ValueError()
    output = requested.resolve()
    if any(character in str(output) for character in "\r\n"):
        raise ValueError()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = output.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o6022 or output.exists() or output.is_symlink()):
        raise ValueError()
    return output


def prepare(requested: Path) -> Path:
    encoded = os.environ.get(ENVIRONMENT)
    if not encoded:
        raise CommandAssetError("command_signed_asset_missing")
    try:
        archive = signed_command_helpers.manifest()["archive"]
        if len(encoded) > 4 * ((archive["bytes"] + 2) // 3):
            raise ValueError()
        contents = base64.b64decode(encoded, validate=True)
        if (base64.b64encode(contents).decode("ascii") != encoded
                or len(contents) != archive["bytes"]
                or hashlib.sha256(contents).hexdigest() != archive["sha256"]):
            raise ValueError()
        output = output_path(requested)
        with tempfile.TemporaryDirectory(prefix=".meecp-command-asset-", dir=output.parent) as directory:
            stage = Path(directory)
            staged_archive = stage / "command-native-v1.tar.gz"
            descriptor = os.open(staged_archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(contents)
                target.flush()
                os.fsync(target.fileno())
            signed_command_helpers.admit_archive(staged_archive, destination=stage / "host")
            # The private stage shares the output filesystem. Linking publishes
            # the complete verified archive atomically and refuses any target
            # created during admission; removing the stage leaves one link.
            os.link(staged_archive, output, follow_symlinks=False)
        return output
    except (Exception, KeyboardInterrupt):
        raise CommandAssetError("command_signed_asset_invalid") from None


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        output = prepare(args.output)
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a", encoding="utf-8") as target:
                target.write("path=" + str(output) + "\n")
    except CommandAssetError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("command_signed_asset_invalid", file=sys.stderr)
        return 1
    print("command_signed_asset_ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
