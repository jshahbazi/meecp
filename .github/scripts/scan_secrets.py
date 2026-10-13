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
    "backend/mcpme_textgen/fixture.py",  # Preserved fetched history before the rename.
    "backend/meecp_textgen/fixture.py",
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
    "templates/builtin-recipes/text-generation-v1-0-11/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-12/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-13/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-14/model-provenance.json",
    # Retired recipe versions remain in fetched history after tuple rollback.
    "templates/builtin-recipes/text-generation-v1-0-15/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-16/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-17/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-18/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-19/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-20/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-21/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-22/model-provenance.json",
    "templates/builtin-recipes/text-generation-v1-0-23/model-provenance.json",
)
# Public SHA-256 file identities, independently recomputed from the accepted
# BF16 model's tokenizer.json and tokenizer_config.json. The native acceptance
# configurations bind these same files. Restrict this exception to those two
# exact values at the two reviewed public acceptance-record paths. The installed
# record repeats the same accepted tokenizer identities; no other digest or
# repository path is exempted.
PUBLIC_STARK_MODEL_FILES_PATH = "engines/STARK_VALIDATION.json"
PUBLIC_STARK_INTEGRATION_MODEL_FILES_PATH = "engines/STARK_JEVK5_VALIDATION.json"
PUBLIC_STARK_MODEL_FILES_SHA256 = (
    "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
    "9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d",
)


# These exact values were independently recomputed from the admitted model
# files, the llama.cpp converter source at 86d86ed4396b4130922f7b9af26e3d9fc11a591b,
# and the hash-bound benchmark harness plus its admitted Qwen tokenizer.
# The groups admit exactly the 22 value/path pairs reported in fetched history.
# They do not admit the same digest at another path or any other digest.
PUBLIC_UZU_METADATA_EXCEPTIONS = (
    (
        "Reviewed public JevK5 file SHA-256 in scoring results",
        (
            "experiments/uzu-rust/jev-scoring/RESULTS.json",
            "experiments/uzu-rust/jev-scoring/RESULTS_BF16.json",
            "experiments/uzu-rust/jev-scoring/RESULTS_INITIAL.json",
        ),
        (
            "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
            "9cf04fffe3d8c3b85e439fb35c7acad0761ab51c422a8c4256d9f887c3a0be7d",
        ),
    ),
    (
        "Reviewed public llama.cpp converter source SHA-256 in BF16 provenance",
        (
            "experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json",
            "experiments/uzu-rust/jev-scoring/RESULTS_BF16.json",
        ),
        (
            "7da3038029f302deac90772ddc23cf948e0dcc3e4246102fe014d68ce0fa9e3d",
            "33727fdd917fe116312a6649d3daea81b8ee0476d19b8875051aa30c5e1146e5",
            "6c225af1d7a2dce88fdc1b885ca4b525de588067508c72b15eef62cf6112c788",
            "34e3b5edcb16a019faa2d7f17f88d82419cf821e964f921db8b53484037d7929",
            "8e88be28468d4282d2b7fd0d62a3adfe7bddcb186e703a380d81d82455401c60",
            "df7be064f695ea6be5f82b15f0d3f5338340b052ada727b4ebf57e95a9b64e7b",
        ),
    ),
    (
        "Reviewed deterministic benchmark sequence SHA-256",
        ("experiments/uzu-rust/BENCHMARK_RESULTS.json",),
        (
            "240f65e053c09140d315105e6665f5fe205c31436fc2edae38f5fd76185fdf84",
            "cd889e2ef5f44f3de1008001869c72c22377be224be1cdad5935865efb851d6e",
            "5b73d78e97cfd7ae3d2fa567f315e58678363cf4f6c4f69c7a1dfe02d1840c7c",
        ),
    ),
    (
        "Reviewed public Qwen file SHA-256 in quality provenance",
        ("experiments/uzu-rust/QUALITY_RESULTS.json",),
        ("87a7830d63fcf43bf241c3c5242e96e62dd3fdc29224ca26fed8ea333db72de4",),
    ),
)


def trusted_config() -> str:
    """Extend built-in rules with reviewed, content-and-path-bound findings.

    Pinned upstream schema and inheritance semantics:
    https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md#configuration
    https://github.com/gitleaks/gitleaks/blob/v8.30.1/config/config.go
    `AND` is essential: a path alone must never suppress another value. There
    are no commit skips, global exceptions, disabled rules or caller inputs.
    """
    import re
    paths = "|".join(re.escape(path) for path in PUBLIC_GGUF_METADATA_PATHS)
    config = ("[extend]\nuseDefault = true\n"
            '[[rules]]\nid = "generic-api-key"\n'
            '[[rules.allowlists]]\n'
            'description = "Reviewed public pinned GGUF metadata SHA-256 at exact source paths"\n'
            'condition = "AND"\nregexTarget = "secret"\n'
            "paths = ['''^(?:" + paths + ")$''']\n"
            "regexes = ['''^" + PUBLIC_GGUF_METADATA_DIGEST + "$''']\n"
            '[[rules.allowlists]]\n'
            'description = "Reviewed public STARK BF16 tokenizer metadata SHA-256 at exact source path"\n'
            'condition = "AND"\nregexTarget = "secret"\n'
            "paths = ['''^(?:" + re.escape(PUBLIC_STARK_MODEL_FILES_PATH) + "|"
            + re.escape(PUBLIC_STARK_INTEGRATION_MODEL_FILES_PATH) + ")$''']\n"
            "regexes = ['''^(?:" + "|".join(PUBLIC_STARK_MODEL_FILES_SHA256) + ")$''']\n")
    for description, paths, digests in PUBLIC_UZU_METADATA_EXCEPTIONS:
        exact_paths = "|".join(re.escape(path) for path in paths)
        exact_values = "|".join(re.escape(value) for value in digests)
        config += (
            '[[rules.allowlists]]\n'
            f'description = "{description}"\n'
            'condition = "AND"\nregexTarget = "secret"\n'
            "paths = ['''^(?:" + exact_paths + ")$''']\n"
            "regexes = ['''^(?:" + exact_values + ")$''']\n"
        )
    return config


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
    with tempfile.TemporaryDirectory(prefix="meecp-secret-scan-") as temporary:
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
