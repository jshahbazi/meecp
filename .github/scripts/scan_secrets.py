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

# Reviewed public data, not a credential: SHA-256 of canonical sorted compact
# ASCII JSON containing the pinned GGUF's complete tokenizer.* metadata.
# Independently recomputed after checking the model's complete file identity:
# Qwen/Qwen2.5-0.5B-Instruct-GGUF revision
# 9217f5db79a29953eb74d5343926648285ec7e67, qwen2.5-0.5b-instruct-q4_k_m.gguf.
# The exception is one exact extracted value AND a reviewed exact repository path,
# for generic-api-key only. Other rules, values and paths remain unchanged.
PUBLIC_GGUF_METADATA_DIGEST = "c828d4ced3ffa97554baf70310ae1b232a03bb63eff517952a9222567b44b5b8"
PUBLIC_GGUF_METADATA_PATHS = (
    "backend/mcpme_textgen/fixture.py",
    "templates/text-generate-v1/model-provenance.json",
    "templates/text-generate-signed-v1/model-provenance.json",
    # Byte-identical metadata shipped inside the immutable declarative recipe.
    "templates/builtin-recipes/text-generation-v1/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-1/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-2/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-3/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-4/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-5/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-6/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-7/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-8/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-9/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-10/model-provenance.json",
)


def trusted_config() -> str:
    """Extend built-in rules with one reviewed, content-and-path-bound finding.

    Pinned upstream schema and inheritance semantics:
    https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md#configuration
    https://github.com/gitleaks/gitleaks/blob/v8.30.1/config/config.go
    `AND` is essential: a path alone must never suppress another value. There
    are no commit skips, global exceptions, disabled rules or caller inputs.
    """
    import re
    paths = "|".join(re.escape(path) for path in PUBLIC_GGUF_METADATA_PATHS)
    return ("[extend]\nuseDefault = true\n"
            '[[rules]]\nid = "generic-api-key"\n'
            '[[rules.allowlists]]\n'
            'description = "Reviewed public pinned GGUF metadata SHA-256 at exact source paths"\n'
            'condition = "AND"\nregexTarget = "secret"\n'
            "paths = ['''^(?:" + paths + ")$''']\n"
            "regexes = ['''^" + PUBLIC_GGUF_METADATA_DIGEST + "$''']\n")


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
    # Both history and source findings must use exact repository-relative paths.
    # Scanning '.' from the already selected target preserves the full scope;
    # an absolute dir argument would prefix findings with its temporary path.
    arguments = [str(binary), mode, "." if mode == "dir" else str(target), "--config", str(config),
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
        config.write_text(trusted_config())
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
