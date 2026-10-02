"""Publish verified installer assets only after their draft upload is complete."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from datetime import datetime
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


class PublicationError(RuntimeError):
    """A fixed diagnostic with no captured command output or credential values."""


def context(version: str | None = None) -> tuple[str, str, str]:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise PublicationError("release_repository_invalid")
    commit = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublicationError("release_source_commit_invalid")
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not isinstance(project_version, str) or not re.fullmatch(VERSION_PATTERN, project_version):
        raise PublicationError("release_project_version_invalid")
    if version is None:
        version = project_version
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise PublicationError("release_version_invalid")
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


def api_response(endpoint: str, *, missing_allowed: bool = False) -> object:
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
    if not isinstance(value, (dict, list)):
        raise PublicationError("release_github_api_failed")
    return value


def api(endpoint: str, *, missing_allowed: bool = False) -> dict | None:
    value = api_response(endpoint, missing_allowed=missing_allowed)
    if value is None and missing_allowed:
        return None
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


def api_pages(endpoint: str, diagnostic: str) -> Iterator[list]:
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        values = api_response(f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(values, list) or len(values) > 100:
            raise PublicationError(diagnostic)
        yield values
        if len(values) < 100:
            return
        page += 1


def stable_releases() -> Iterator[dict]:
    for releases in api_pages(f"repos/{REPOSITORY}/releases", "release_github_release_list_invalid"):
        for release in releases:
            if (not isinstance(release, dict) or type(release.get("draft")) is not bool
                    or type(release.get("prerelease")) is not bool
                    or not isinstance(release.get("tag_name"), str)):
                raise PublicationError("release_github_release_list_invalid")
            tag = release["tag_name"]
            if (release["draft"] or release["prerelease"]
                    or not re.fullmatch("v" + VERSION_PATTERN, tag)):
                continue
            yield release


def bumped_version(version: str, bump: str) -> str:
    if bump not in ("major", "minor", "patch"):
        raise PublicationError("release_bump_invalid")
    # A source baseline ahead of published releases remains the lower bound.
    highest = tuple(map(int, version.split(".")))
    for release in stable_releases():
        highest = max(highest, tuple(map(int, release["tag_name"][1:].split("."))))
    major, minor, patch = highest
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def prepare(bump: str | None = None) -> None:
    version, tag, _ = context()
    if bump is not None:
        version = bumped_version(version, bump)
        tag = "v" + version
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


def validate_receipt(path: Path, receipt: dict, version: str, commit: str) -> None:
    app = receipt.get("app_build")
    checks = receipt.get("checks")
    if (
        type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1
        or receipt.get("distribution") != "notarized_drag_install_dmg"
        or receipt.get("version") != version
        or receipt.get("architecture") != "arm64"
        or receipt.get("minimum_macos") != "26.0"
        or receipt.get("notarization") != "accepted"
        or not submission_id(receipt.get("notary_submission_id"))
        or not isinstance(app, dict)
        or app.get("version", version) != version
        or app.get("signing") != "developer_id"
        or app.get("hardened_runtime") is not True
        or app.get("secure_timestamp") is not True
        or app.get("notarization") != "accepted"
        or app.get("notarization_ticket_stapled") is not True
        or not submission_id(app.get("notary_submission_id"))
        or app.get("source_commit") != commit
        or app.get("source_tree_dirty") is not False
        or not isinstance(checks, dict)
        or any(checks.get(name) is not True for name in DMG_CHECKS)
    ):
        raise PublicationError("release_asset_receipt_invalid")
    if (
        receipt.get("disk_image_signed") is not True
        or receipt.get("notarization_ticket_stapled") is not True
    ):
        raise PublicationError("release_asset_receipt_invalid")
    if (type(receipt.get("bytes")) is not int or receipt["bytes"] <= 0
            or receipt["bytes"] != path.stat().st_size or receipt.get("sha256") != digest(path)):
        raise PublicationError("release_asset_receipt_mismatch")


def validated_assets(directory: Path, version: str, commit: str) -> list[Path]:
    binary = directory / f"mcpme-{version}-arm64.dmg"
    receipt_path = binary.with_name(binary.name + ".build.json")
    paths = [binary, receipt_path]
    allowed_names = {path.name for path in paths} | {"SHA256SUMS.txt"}
    if not directory.is_dir() or {path.name for path in directory.iterdir()} - allowed_names:
        raise PublicationError("release_asset_set_invalid")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise PublicationError("release_asset_set_invalid")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (ValueError, UnicodeError):
        raise PublicationError("release_asset_receipt_invalid") from None
    if not isinstance(receipt, dict):
        raise PublicationError("release_asset_receipt_invalid")
    validate_receipt(binary, receipt, version, commit)
    sums = directory / "SHA256SUMS.txt"
    content = "".join(f"{digest(path)}  {path.name}\n" for path in sorted(paths))
    if sums.exists() or sums.is_symlink():
        if sums.is_symlink() or not sums.is_file() or sums.read_text() != content:
            raise PublicationError("release_checksum_manifest_invalid")
    else:
        with sums.open("x", encoding="utf-8") as destination:
            destination.write(content)
    return [*paths, sums]


def resolve_release_id(tag: str) -> int:
    # gh resolves drafts through GraphQL; REST's tag endpoint only finds
    # published releases. All subsequent reads and writes use this stable ID.
    result = command("release", "view", tag, "--repo", GH_REPOSITORY, "--json", "databaseId")
    if result.returncode != 0:
        raise PublicationError("release_github_command_failed")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise PublicationError("release_github_release_id_invalid") from None
    identifier = value.get("databaseId") if isinstance(value, dict) else None
    if type(identifier) is not int or identifier <= 0:
        raise PublicationError("release_github_release_id_invalid")
    return identifier


def verified_release(tag: str, commit: str, assets: list[Path], *, draft: bool,
                     release_id: int) -> str:
    if type(release_id) is not int or release_id <= 0:
        raise PublicationError("release_github_release_id_invalid")
    value = api(f"repos/{REPOSITORY}/releases/{release_id}")
    expected_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    url = value.get("html_url")
    valid_url = url == expected_url
    if draft and isinstance(url, str):
        valid_url = valid_url or re.fullmatch(
            re.escape(f"https://github.com/{REPOSITORY}/releases/tag/") + r"untagged-[a-f0-9]+", url
        ) is not None
    if (value.get("id") != release_id
            or value.get("tag_name") != tag or value.get("target_commitish") != commit
            or value.get("draft") is not draft or value.get("prerelease") is not False
            or not valid_url):
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


def previous_release_tag(tag: str) -> str | None:
    previous = []
    for release in stable_releases():
        if release["tag_name"] == tag:
            continue
        published = release.get("published_at")
        if not isinstance(published, str):
            raise PublicationError("release_github_release_list_invalid")
        try:
            published = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise PublicationError("release_github_release_list_invalid") from None
        previous.append((published, release["tag_name"]))
    return max(previous)[1] if previous else None


def release_commits(previous_tag: str | None, commit: str) -> set[str]:
    """Use reachable commits, never merge/publication timestamps, as the boundary."""
    if (not re.fullmatch(r"[0-9a-f]{40}", commit)
            or (previous_tag is not None and not re.fullmatch("v" + VERSION_PATTERN, previous_tag))):
        raise PublicationError("release_notes_range_invalid")
    commits = set()

    def collect(values: object) -> int:
        if not isinstance(values, list) or len(values) > 100:
            raise PublicationError("release_notes_commit_list_invalid")
        for value in values:
            sha = value.get("sha") if isinstance(value, dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha in commits:
                raise PublicationError("release_notes_commit_list_invalid")
            commits.add(sha)
        return len(values)

    if previous_tag is None:
        for values in api_pages(f"repos/{REPOSITORY}/commits?sha={commit}",
                                "release_notes_commit_list_invalid"):
            collect(values)
        if commit not in commits:
            raise PublicationError("release_notes_commit_list_invalid")
        return commits

    page = 1
    metadata = None
    while True:
        value = api(f"repos/{REPOSITORY}/compare/{previous_tag}...{commit}?per_page=100&page={page}")
        base = value.get("base_commit")
        merge_base = value.get("merge_base_commit")
        total = value.get("total_commits")
        if (value.get("status") not in ("ahead", "identical")
                or type(total) is not int or total < 0
                or not isinstance(base, dict) or not isinstance(merge_base, dict)
                or not isinstance(base.get("sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", base["sha"])
                or base["sha"] != merge_base.get("sha")
                or (value["status"] == "identical" and (total != 0 or base["sha"] != commit))
                or (value["status"] == "ahead" and total == 0)):
            raise PublicationError("release_notes_range_invalid")
        current = (base["sha"], total, value["status"])
        if metadata is not None and metadata != current:
            raise PublicationError("release_notes_range_invalid")
        metadata = current
        count = collect(value.get("commits"))
        if count < 100:
            if len(commits) != total or (total > 0 and commit not in commits) or base["sha"] in commits:
                raise PublicationError("release_notes_commit_list_invalid")
            return commits
        page += 1


def pull_request_notes(previous_tag: str | None, commit: str) -> str:
    commits = release_commits(previous_tag, commit)
    heading = f"## Changes since {previous_tag}" if previous_tag else "## Changes in this release"
    notes = heading + "\n\n"
    if previous_tag:
        notes += f"[Full changelog](https://github.com/{REPOSITORY}/compare/{previous_tag}...{commit})\n\n"
    pulls = {}
    if commits:
        # Listing all PRs in creation order keeps pagination stable when an old
        # closed PR is reopened or an old open PR is closed during publication.
        for values in api_pages(f"repos/{REPOSITORY}/pulls?state=all&sort=created&direction=asc",
                                "release_notes_pull_request_list_invalid"):
            for pull in values:
                if not isinstance(pull, dict) or "merged_at" not in pull:
                    raise PublicationError("release_notes_pull_request_list_invalid")
                if pull["merged_at"] is None:
                    continue
                merged = pull["merged_at"]
                sha = pull.get("merge_commit_sha")
                if (not isinstance(merged, str) or not isinstance(sha, str)
                        or not re.fullmatch(r"[0-9a-f]{40}", sha)):
                    raise PublicationError("release_notes_pull_request_list_invalid")
                if sha not in commits:
                    continue
                try:
                    merged = datetime.strptime(merged, "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    raise PublicationError("release_notes_pull_request_list_invalid") from None
                number, title, body = pull.get("number"), pull.get("title"), pull.get("body")
                if (type(number) is not int or number <= 0 or number in pulls
                        or not isinstance(title, str) or not title.strip()
                        or (body is not None and not isinstance(body, str))):
                    raise PublicationError("release_notes_pull_request_list_invalid")
                pulls[number] = (merged, title, body)
    for number in sorted(pulls, key=lambda number: (pulls[number][0], number)):
        _, title, body = pulls[number]
        # Preserve the authored opening post, including its Markdown. Quoting it
        # keeps its headings/lists scoped to this PR instead of the release page.
        title = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", " ".join(title.split()))
        description = body.strip() if body and body.strip() else "No description was provided."
        notes += f"### [{title} (#{number})](https://github.com/{REPOSITORY}/pull/{number})\n\n"
        notes += "\n".join("> " + line if line else ">" for line in description.splitlines()) + "\n\n"
    if not pulls:
        notes += "No pull requests were merged in this commit range.\n\n"
    return notes


def release_notes(version: str, commit: str) -> str:
    changes = pull_request_notes(previous_release_tag("v" + version), commit)
    notes = (
        f"mcpme {version} for Apple silicon Macs running macOS 26 or later.\n\n"
        + changes
        + "## Installation\n\n"
        "The DMG contains the Developer ID signed, Apple notarized app.\n\n"
        "- DMG: open mcpme.app on the read-only image and choose Install and Restart.\n"
        "  It stages the signed app, gracefully quits the current copy, replaces the\n"
        "  installation, and launches it. Manual dragging remains available after quitting.\n\n"
        "The DMG installer waits up to 75 seconds for graceful shutdown and restores the\n"
        "prior installation on a confirmed launch failure. The app includes its runtime;\n"
        "projects and settings are preserved.\n\n"
        "SHA256SUMS.txt contains SHA-256 hashes for the DMG and its build receipt.\n\n"
        f"Built from commit `{commit}`.\n"
    )
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[0-9]+", run_id):
        notes += f"\n[Build workflow](https://github.com/{REPOSITORY}/actions/runs/{run_id})\n"
    return notes


def checked_command(*arguments: str) -> None:
    if command(*arguments).returncode != 0:
        raise PublicationError("release_github_command_failed")


def finish_release(tag: str, commit: str, assets: list[Path], release_id: int) -> str:
    verify_tag(tag, commit)
    verified_release(tag, commit, assets, draft=True, release_id=release_id)
    # Address the verified draft by ID, including the write. No asset is replaced.
    checked_command("api", "--hostname", "github.com", "--method", "PATCH",
                    f"repos/{REPOSITORY}/releases/{release_id}", "--field", "draft=false")
    verify_tag(tag, commit)
    return verified_release(tag, commit, assets, draft=False, release_id=release_id)


def resume(directory: Path, release_id: int, version: str | None = None) -> str:
    """Publish an existing complete draft only when all original assets match."""
    version, tag, commit = context(version)
    assets = validated_assets(directory, version, commit)
    return finish_release(tag, commit, assets, release_id)


def publish(directory: Path, version: str | None = None) -> str:
    version, tag, commit = context(version)
    assets = validated_assets(directory, version, commit)
    preflight(tag)
    # Finish all changelog reads before creating a tag or draft. API failures
    # must not silently produce an incomplete public release summary.
    content = release_notes(version, commit)
    # GitHub ignores target_commitish when a tag already exists. Create a fresh
    # lightweight ref atomically so a racing tag cannot silently select other code.
    checked_command("api", "--hostname", "github.com", "--method", "POST",
                    f"repos/{REPOSITORY}/git/refs", "--raw-field", "ref=refs/tags/" + tag,
                    "--raw-field", "sha=" + commit)
    verify_tag(tag, commit)
    with tempfile.TemporaryDirectory(prefix="mcpme-release-notes-") as temporary:
        notes = Path(temporary) / "notes.md"
        notes.write_text(content, encoding="utf-8")
        checked_command("release", "create", tag, "--repo", GH_REPOSITORY, "--draft",
                        "--verify-tag", "--target", commit, "--title", "mcpme " + version,
                        "--notes-file", str(notes))
    checked_command("release", "upload", tag, "--repo", GH_REPOSITORY,
                    *(str(path) for path in assets))
    # No overwrite/delete operations: a failed upload or verification leaves the
    # draft for inspection. Only a complete upload with matching digests is public.
    return finish_release(tag, commit, assets, resolve_release_id(tag))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    preparation = subcommands.add_parser("prepare")
    preparation.add_argument("--bump", choices=("major", "minor", "patch"))
    publication = subcommands.add_parser("publish")
    publication.add_argument("--assets-dir", type=Path, default=ROOT / "dist")
    publication.add_argument("--version", help="Exact version selected by the signing job")
    recovery = subcommands.add_parser("resume")
    recovery.add_argument("--assets-dir", type=Path, default=ROOT / "dist")
    recovery.add_argument("--release-id", type=int, required=True)
    recovery.add_argument("--version", help="Original release version from the build receipts")
    arguments = parser.parse_args()
    try:
        if arguments.operation == "prepare":
            prepare(arguments.bump)
        elif arguments.operation == "resume":
            print(resume(arguments.assets_dir, arguments.release_id, arguments.version))
        else:
            print(publish(arguments.assets_dir, arguments.version))
    except PublicationError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("release_publication_interrupted") from None
    except Exception:
        raise SystemExit("release_publication_failed") from None


if __name__ == "__main__":
    main()
