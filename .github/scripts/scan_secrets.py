"""Run a pinned, silent credential scan over fetched history and tracked source."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


VERSION = "8.30.1"
ARCHIVE_URL = (
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
    "gitleaks_8.30.1_darwin_arm64.tar.gz"
)
# Official release checksums, also verified against GitHub's asset digest:
# https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
ARCHIVE_SHA256 = "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
FINDINGS_EXIT_CODE = 10


class ScanFailure(Exception):
    """A fixed public error code, with no underlying tool output."""


def scanner_binary(workspace: Path) -> Path:
    archive = workspace / "gitleaks.tar.gz"
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(ARCHIVE_URL, timeout=30) as response, archive.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ScanFailure("secret_scan_failed")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise ScanFailure("secret_scan_failed")
    binary = workspace / "gitleaks"
    # Never extract archive paths, links, or auxiliary files to the filesystem.
    with tarfile.open(archive, "r:gz") as source:
        members = [member for member in source.getmembers() if member.name == "gitleaks"]
        if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= MAX_BINARY_BYTES:
            raise ScanFailure("secret_scan_failed")
        contents = source.extractfile(members[0])
        if contents is None:
            raise ScanFailure("secret_scan_failed")
        with contents, binary.open("xb") as output:
            shutil.copyfileobj(contents, output)
    binary.chmod(0o700)
    return binary


def git_output(repository: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments], stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=60,
    )


def tracked_source(repository: Path, destination: Path):
    """Copy only tracked files, including local edits, without ignored artifacts."""
    destination.mkdir()
    for entry in git_output(repository, "ls-files", "--cached", "-z").split(b"\0"):
        if not entry:
            continue
        relative = Path(os.fsdecode(entry))
        if relative.is_absolute() or ".." in relative.parts:
            raise ScanFailure("secret_scan_failed")
        source = repository / relative
        if not source.exists() and not source.is_symlink():
            # A locally deleted tracked file is already covered by history.
            continue
        if not source.resolve().is_relative_to(repository.resolve()) or not source.is_file():
            raise ScanFailure("secret_scan_failed")
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copied)


def run_scanner(binary: Path, mode: str, target: Path, config: Path, ignore_directory: Path):
    arguments = [str(binary), mode, str(target), "--config", str(config),
                 "--gitleaks-ignore-path", str(ignore_directory),
                 "--ignore-gitleaks-allow", "--redact=100", "--no-banner",
                 "--exit-code", str(FINDINGS_EXIT_CODE), "--timeout", "600"]
    if mode == "git":
        arguments.append("--log-opts=--all")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GITLEAKS_")}
    result = subprocess.run(
        arguments, cwd=target, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=660, check=False,
    )
    if result.returncode == FINDINGS_EXIT_CODE:
        raise ScanFailure("secret_scan_detected_credentials")
    if result.returncode != 0:
        raise ScanFailure("secret_scan_failed")


def scan(repository: Path):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ScanFailure("secret_scan_failed")
    repository = repository.resolve(strict=True)
    if git_output(repository, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ScanFailure("secret_scan_failed")
    with tempfile.TemporaryDirectory(prefix="mcpme-secret-scan-") as temporary:
        workspace = Path(temporary)
        binary = scanner_binary(workspace)
        config = workspace / "default.toml"
        # Explicitly select built-in rules, including generic API keys and private
        # keys, instead of accepting a repository or environment allowlist.
        config.write_text("[extend]\nuseDefault = true\n")
        ignore_directory = workspace / "ignore"
        ignore_directory.mkdir()
        run_scanner(binary, "git", repository, config, ignore_directory)
        source = workspace / "tracked"
        tracked_source(repository, source)
        run_scanner(binary, "dir", source, config, ignore_directory)


def main() -> int:
    try:
        scan(Path.cwd())
    except ScanFailure as error:
        print(str(error))
        return 1 if str(error) == "secret_scan_detected_credentials" else 2
    except (Exception, KeyboardInterrupt):
        print("secret_scan_failed")
        return 2
    print("secret_scan_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
