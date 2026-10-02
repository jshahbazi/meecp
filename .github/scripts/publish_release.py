"""Publish verified installer assets only after their draft upload is complete."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "jshahbazi/mcpme"
GH_REPOSITORY = "github.com/" + REPOSITORY
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
SUBMISSION_PATTERN = r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}"
DMG_CHECKS = (
    "image_checksum", "read_only_mount", "applications_shortcut",
    "mounted_app_signature", "copied_app_signature", "ejected_image",
    "private_runtime_minimal_environment", "copied_app_notarization_ticket",
    "copied_app_gatekeeper_assessment", "disk_image_notarization_ticket",
    "disk_image_gatekeeper_assessment",
)
ZIP_CHECKS = (
    "archive_contains_app", "extracted_app_signature",
    "extracted_app_notarization_ticket", "extracted_app_gatekeeper_assessment",
    "private_runtime_minimal_environment",
)


class PublicationError(RuntimeError):
    """A fixed diagnostic with no captured command output or credential values."""


def context() -> tuple[str, str, str]:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise PublicationError("release_repository_invalid")
    commit = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublicationError("release_source_commit_invalid")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise PublicationError("release_project_version_invalid")
    return version, "v" + version, commit


def command(*arguments: str) -> subprocess.CompletedProcess:
    # Capture everything in memory. gh errors can include authentication details;
    # they must never be printed, attached to exceptions, or saved as artifacts.
    environment = dict(os.environ, GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    try:
        return subprocess.run(
            ["gh", *arguments], env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PublicationError("release_github_command_failed") from None


def api(endpoint: str, *, missing_allowed: bool = False) -> dict | None:
    result = command("api", "--hostname", "github.com", "--include", "--method", "GET", endpoint)
    # Use the actual HTTP status rather than matching gh's human-readable errors.
    # A missing object is safe to create; authorization/network errors are not.
    header, separator, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    first_line = header.split("\n", 1)[0]
    match = re.fullmatch(r"HTTP/[0-9.]+ ([0-9]{3})(?: .*)?", first_line)
    if not separator or match is None:
        raise PublicationError("release_github_api_failed")
    status = int(match.group(1))
    if status == 404 and missing_allowed:
        return None
    if status != 200 or result.returncode != 0:
        raise PublicationError("release_github_api_failed")
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise PublicationError("release_github_api_failed") from None
    if not isinstance(value, dict):
        raise PublicationError("release_github_api_failed")
    return value


def release_endpoint(tag: str) -> str:
    return f"repos/{REPOSITORY}/releases/tags/{tag}"


def preflight(tag: str) -> None:
    if api(release_endpoint(tag), missing_allowed=True) is not None:
        raise PublicationError("release_version_already_exists")
    if api(f"repos/{REPOSITORY}/git/ref/tags/{tag}", missing_allowed=True) is not None:
        raise PublicationError("release_tag_already_exists")


def verify_tag(tag: str, commit: str) -> None:
    value = api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")
    target = value.get("object")
    if (value.get("ref") != "refs/tags/" + tag or not isinstance(target, dict)
            or target.get("type") != "commit" or target.get("sha") != commit):
        raise PublicationError("release_tag_commit_mismatch")


def prepare() -> None:
    version, tag, _ = context()
    preflight(tag)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as destination:
            destination.write(f"version={version}\ntag={tag}\n")
    print("release_preflight_ready")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def submission_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(SUBMISSION_PATTERN, value) is not None


def validate_receipt(path: Path, receipt: dict, version: str, commit: str, kind: str) -> None:
    app = receipt.get("app_build")
    checks = receipt.get("checks")
    expected_distribution = "notarized_drag_install_dmg" if kind == "dmg" else "notarized_app_zip"
    expected_checks = DMG_CHECKS if kind == "dmg" else ZIP_CHECKS
    if (
        type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1
        or receipt.get("distribution") != expected_distribution
        or receipt.get("version") != version
        or receipt.get("architecture") != "arm64"
        or receipt.get("minimum_macos") != "26.0"
        or receipt.get("notarization") != "accepted"
        or not submission_id(receipt.get("notary_submission_id"))
        or not isinstance(app, dict)
        or app.get("signing") != "developer_id"
        or app.get("hardened_runtime") is not True
        or app.get("secure_timestamp") is not True
        or app.get("notarization") != "accepted"
        or app.get("notarization_ticket_stapled") is not True
        or not submission_id(app.get("notary_submission_id"))
        or app.get("source_commit") != commit
        or app.get("source_tree_dirty") is not False
        or not isinstance(checks, dict)
        or any(checks.get(name) is not True for name in expected_checks)
    ):
        raise PublicationError("release_asset_receipt_invalid")
    if kind == "dmg" and (
        receipt.get("disk_image_signed") is not True
        or receipt.get("notarization_ticket_stapled") is not True
    ):
        raise PublicationError("release_asset_receipt_invalid")
    if kind == "zip" and receipt.get("contained_app_notarization_ticket_stapled") is not True:
        raise PublicationError("release_asset_receipt_invalid")
    if (type(receipt.get("bytes")) is not int or receipt["bytes"] <= 0
            or receipt["bytes"] != path.stat().st_size or receipt.get("sha256") != digest(path)):
        raise PublicationError("release_asset_receipt_mismatch")


def validated_assets(directory: Path, version: str, commit: str) -> list[Path]:
    binaries = [directory / f"mcpme-{version}-arm64.{kind}" for kind in ("dmg", "zip")]
    paths = [path for binary in binaries for path in (binary, binary.with_name(binary.name + ".build.json"))]
    allowed_names = {path.name for path in paths} | {"SHA256SUMS.txt"}
    if not directory.is_dir() or {path.name for path in directory.iterdir()} - allowed_names:
        raise PublicationError("release_asset_set_invalid")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise PublicationError("release_asset_set_invalid")
    receipts = []
    for binary in binaries:
        try:
            receipt = json.loads(binary.with_name(binary.name + ".build.json").read_text())
        except (ValueError, UnicodeError):
            raise PublicationError("release_asset_receipt_invalid") from None
        if not isinstance(receipt, dict):
            raise PublicationError("release_asset_receipt_invalid")
        validate_receipt(binary, receipt, version, commit, binary.suffix[1:])
        receipts.append(receipt)
    if any(receipts[0].get(name) != receipts[1].get(name) for name in ("build", "app_build")):
        raise PublicationError("release_asset_app_mismatch")
    sums = directory / "SHA256SUMS.txt"
    content = "".join(f"{digest(path)}  {path.name}\n" for path in sorted(paths))
    if sums.exists() or sums.is_symlink():
        if sums.is_symlink() or not sums.is_file() or sums.read_text() != content:
            raise PublicationError("release_checksum_manifest_invalid")
    else:
        with sums.open("x", encoding="utf-8") as destination:
            destination.write(content)
    return [*paths, sums]


def verified_release(tag: str, commit: str, assets: list[Path], *, draft: bool) -> str:
    value = api(release_endpoint(tag))
    expected_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    if (value.get("tag_name") != tag or value.get("target_commitish") != commit
            or value.get("draft") is not draft or value.get("prerelease") is not False
            or value.get("html_url") != expected_url):
        raise PublicationError("release_uploaded_metadata_invalid")
    uploaded = value.get("assets")
    if not isinstance(uploaded, list) or len(uploaded) != len(assets):
        raise PublicationError("release_uploaded_asset_set_invalid")
    expected = {path.name: path for path in assets}
    seen = set()
    for asset in uploaded:
        if not isinstance(asset, dict) or asset.get("name") not in expected or asset["name"] in seen:
            raise PublicationError("release_uploaded_asset_set_invalid")
        path = expected[asset["name"]]
        seen.add(path.name)
        if (asset.get("state") != "uploaded" or type(asset.get("size")) is not int
                or asset["size"] != path.stat().st_size
                or asset.get("digest") != "sha256:" + digest(path)):
            raise PublicationError("release_uploaded_asset_digest_mismatch")
    return expected_url


def release_notes(version: str, commit: str) -> str:
    notes = (
        f"mcpme {version} for Apple silicon Macs running macOS 26 or later.\n\n"
        "Both downloads contain the same Developer ID signed, Apple notarized app.\n\n"
        "- DMG: open the disk image and drag mcpme.app to Applications.\n"
        "- ZIP: extract the archive and move mcpme.app to Applications.\n\n"
        "Quit any running copy before replacing it, then open mcpme from Applications. "
        "The app includes its runtime; projects and settings are preserved.\n\n"
        "SHA256SUMS.txt contains SHA-256 hashes for both downloads and their build receipts.\n\n"
        f"Built from commit `{commit}`.\n"
    )
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[0-9]+", run_id):
        notes += f"\n[Build workflow](https://github.com/{REPOSITORY}/actions/runs/{run_id})\n"
    return notes


def checked_command(*arguments: str) -> None:
    if command(*arguments).returncode != 0:
        raise PublicationError("release_github_command_failed")


def publish(directory: Path) -> str:
    version, tag, commit = context()
    assets = validated_assets(directory, version, commit)
    preflight(tag)
    # GitHub ignores target_commitish when a tag already exists. Create a fresh
    # lightweight ref atomically so a racing tag cannot silently select other code.
    checked_command("api", "--hostname", "github.com", "--method", "POST",
                    f"repos/{REPOSITORY}/git/refs", "--raw-field", "ref=refs/tags/" + tag,
                    "--raw-field", "sha=" + commit)
    verify_tag(tag, commit)
    with tempfile.TemporaryDirectory(prefix="mcpme-release-notes-") as temporary:
        notes = Path(temporary) / "notes.md"
        notes.write_text(release_notes(version, commit), encoding="utf-8")
        checked_command("release", "create", tag, "--repo", GH_REPOSITORY, "--draft",
                        "--verify-tag", "--target", commit, "--title", "mcpme " + version,
                        "--notes-file", str(notes))
    checked_command("release", "upload", tag, "--repo", GH_REPOSITORY,
                    *(str(path) for path in assets))
    # No overwrite/delete operations: a failed upload or verification leaves the
    # draft for inspection. Only a complete upload with matching digests is public.
    verified_release(tag, commit, assets, draft=True)
    verify_tag(tag, commit)
    checked_command("release", "edit", tag, "--repo", GH_REPOSITORY, "--draft=false")
    verify_tag(tag, commit)
    return verified_release(tag, commit, assets, draft=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    subcommands.add_parser("prepare")
    publication = subcommands.add_parser("publish")
    publication.add_argument("--assets-dir", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    try:
        if arguments.operation == "prepare":
            prepare()
        else:
            print(publish(arguments.assets_dir))
    except PublicationError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("release_publication_interrupted") from None
    except Exception:
        raise SystemExit("release_publication_failed") from None


if __name__ == "__main__":
    main()
