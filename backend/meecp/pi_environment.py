"""Generic private runtime staging and archive intake for Pi build work.

Archives and runtime copies become regular-file closures, so packages remain
relocatable and the final package verifier can bind every byte. This module
does not execute archive contents, discover user credentials or install globals.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import re
import ssl
import stat
import tarfile
import tempfile
import threading
import zipfile

from .preparation_contracts import PreparationError
from .processes import OwnershipLost, _await_ownership

MAX_FILES = 60000
MAX_BYTES = 2 * 1024**3
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_METADATA_BYTES = 16 * 1024**2
ENVIRONMENT_MARKER = ".meecp-environment.json"


def stage_trust_bundle(destination: Path) -> Path | None:
    """Stage public certificates from an explicitly configured host trust file.

    TLS verification stays enabled. Other PEM material and all ordinary user
    environment variables are excluded from the child environment.
    """
    configured = os.environ.get("NODE_EXTRA_CA_CERTS")
    if not configured:
        return None
    source = Path(configured).resolve(strict=True)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid not in {0, os.getuid()}
                or details.st_mode & 0o022 or not 0 < details.st_size <= MAX_METADATA_BYTES):
            raise PreparationError("pi_environment_trust_invalid")
        raw = bytearray()
        while len(raw) <= MAX_METADATA_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_METADATA_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_METADATA_BYTES:
            raise PreparationError("pi_environment_trust_invalid")
        certificates = re.findall(rb"-----BEGIN CERTIFICATE-----[\r\nA-Za-z0-9+/=]+-----END CERTIFICATE-----", bytes(raw))
        if not certificates:
            raise PreparationError("pi_environment_trust_invalid")
        for certificate in certificates:
            ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
        with destination.open("xb") as output:
            output.write(b"\n".join(certificates) + b"\n")
        destination.chmod(0o600)
        return destination
    finally:
        os.close(descriptor)


def _safe_name(value: str) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or any(ord(char) < 32 for char in value) or value.startswith("/")):
        raise PreparationError("pi_archive_path_unsafe")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise PreparationError("pi_archive_path_unsafe")
    return parts


class _Budget:
    def __init__(self, check=lambda: None):
        self.check, self.files, self.bytes = check, 0, 0

    def add(self, size):
        self.check()
        self.files += 1
        self.bytes += size
        if self.files > MAX_FILES or self.bytes > MAX_BYTES:
            raise PreparationError("pi_environment_size_limit")


def copy_regular_tree(source: Path, target: Path, check=lambda: None, *, allowed_root: Path | None = None,
                      exclude_paths=(), exclude_names=(), exclude_private=False, budget: _Budget | None = None) -> None:
    """Copy a closed tree; materialize internal links and reject external links."""
    source = source.resolve(strict=True)
    allowed_root = source if allowed_root is None else allowed_root.resolve(strict=True)
    budget = budget or _Budget(check)
    exclusions = {Path(value).absolute() for value in exclude_paths}
    def visit(path, destination, ancestors=()):
        check()
        if path != source and (path.absolute() in exclusions or path.name in exclude_names or (exclude_private and (
                path.name == ".env" or path.name.startswith(".env.") or path.name.endswith((".key", ".pem"))
                or path.name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
                or path.name.startswith(".meecp-")))):
            return
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(allowed_root):
            raise PreparationError("pi_environment_external_link")
        details = resolved.stat()
        if stat.S_ISDIR(details.st_mode):
            if resolved in ancestors:
                raise PreparationError("pi_environment_link_cycle")
            budget.add(0)
            destination.mkdir(mode=0o700)
            for child in sorted(resolved.iterdir()):
                visit(child, destination / child.name, (*ancestors, resolved))
        elif stat.S_ISREG(details.st_mode):
            budget.add(details.st_size)
            shutil.copyfile(resolved, destination)
            destination.chmod(0o755 if details.st_mode & 0o111 else 0o600)
        else:
            raise PreparationError("pi_environment_file_unsafe")
    visit(source, target)


def _link_destination(member, target, *, hard=False):
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target or "\0" in target:
        raise PreparationError("pi_archive_link_unsafe")
    parts = [] if hard else list(PurePosixPath(member).parent.parts)
    for component in PurePosixPath(target).parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not parts:
                raise PreparationError("pi_archive_link_unsafe")
            parts.pop()
        else:
            parts.append(component)
    return tuple(parts)


def extract_regular_archive(archive: Path, target: Path, *, strip_components=0, check=lambda: None) -> dict:
    """Extract tar/zip without invoking an archiver or retaining filesystem links."""
    if type(strip_components) is not int or not 0 <= strip_components <= 32:
        raise PreparationError("pi_archive_arguments_invalid")
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or not 0 < details.st_size <= MAX_ARCHIVE_BYTES:
            raise PreparationError("pi_archive_file_unsafe")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            is_zip = zipfile.is_zipfile(source)
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            container = zipfile.ZipFile(source) if is_zip else tarfile.open(fileobj=source, mode="r:*")
            with container:
                if is_zip:
                    members = container.infolist()
                else:
                    members = []
                    for item in container:
                        check()
                        members.append(item)
                        if len(members) > MAX_FILES:
                            raise PreparationError("pi_environment_size_limit")
                if len(members) > MAX_FILES:
                    raise PreparationError("pi_environment_size_limit")
                records, links, seen = [], [], set()
                budget = _Budget(check)
                for item in members:
                    check()
                    name = item.filename.rstrip("/") if is_zip else item.name.rstrip("/")
                    original = _safe_name(name)
                    if len(original) <= strip_components:
                        if (item.is_dir() if is_zip else item.isdir()):
                            continue
                        raise PreparationError("pi_archive_path_unsafe")
                    parts = original[strip_components:]
                    if parts in seen:
                        raise PreparationError("pi_archive_duplicate_path")
                    seen.add(parts)
                    if is_zip:
                        mode = (item.external_attr >> 16) & 0xffff
                        kind = "directory" if item.is_dir() else "link" if stat.S_ISLNK(mode) else "file"
                        if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                            raise PreparationError("pi_archive_file_unsafe")
                        size = item.file_size
                    else:
                        kind = "directory" if item.isdir() else "link" if item.issym() or item.islnk() else "file" if item.isfile() else "unsafe"
                        mode, size = item.mode, item.size
                    if kind == "unsafe":
                        raise PreparationError("pi_archive_file_unsafe")
                    budget.add(size if kind == "file" else 0)
                    if kind == "link":
                        if is_zip:
                            if size > 4096:
                                raise PreparationError("pi_archive_link_unsafe")
                            link = container.read(item).decode("utf-8")
                        else:
                            link = item.linkname
                        linked = _link_destination(name, link, hard=not is_zip and item.islnk())
                        if len(linked) <= strip_components:
                            raise PreparationError("pi_archive_link_unsafe")
                        links.append((parts, linked[strip_components:]))
                    else:
                        records.append((parts, kind, mode, size, item))
                target.mkdir(mode=0o700)
                for parts, kind, mode, size, item in sorted(records, key=lambda value: (len(value[0]), value[0])):
                    destination = target.joinpath(*parts)
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if kind == "directory":
                        destination.mkdir(mode=0o700, exist_ok=True)
                        continue
                    if destination.exists():
                        raise PreparationError("pi_archive_duplicate_path")
                    stream = container.open(item) if is_zip else container.extractfile(item)
                    if stream is None:
                        raise PreparationError("pi_archive_file_unsafe")
                    written = 0
                    with stream, destination.open("xb") as output:
                        for chunk in iter(lambda: stream.read(65536), b""):
                            check()
                            written += len(chunk)
                            if written > size:
                                raise PreparationError("pi_environment_size_limit")
                            output.write(chunk)
                    if written != size:
                        raise PreparationError("pi_archive_file_unsafe")
                    destination.chmod(0o755 if mode & 0o111 else 0o600)
                pending = links
                while pending:
                    remaining = []
                    progress = False
                    for parts, linked in pending:
                        check()
                        link_source = target.joinpath(*linked)
                        if not link_source.exists():
                            remaining.append((parts, linked))
                            continue
                        destination = target.joinpath(*parts)
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        if destination.exists() or destination == link_source or destination in link_source.parents:
                            raise PreparationError("pi_archive_link_unsafe")
                        if link_source.is_dir():
                            copy_regular_tree(link_source, destination, check, budget=budget)
                        else:
                            budget.add(link_source.stat().st_size)
                            shutil.copyfile(link_source, destination)
                            destination.chmod(stat.S_IMODE(link_source.stat().st_mode))
                        progress = True
                    if not progress:
                        raise PreparationError("pi_archive_link_missing_or_cycle")
                    pending = remaining
                return {"archive_sha256": digest, "files": budget.files, "bytes": budget.bytes}
    except (tarfile.TarError, zipfile.BadZipFile, UnicodeError):
        raise PreparationError("pi_archive_format_invalid") from None
    finally:
        os.close(descriptor)


async def _thread(function, *args, broker):
    canceled = threading.Event()
    def check():
        if canceled.is_set() or broker._closed:
            raise PreparationError("pi_environment_canceled")
    task = asyncio.create_task(asyncio.to_thread(function, *args, check=check))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        canceled.set()
        await _await_ownership(asyncio.gather(task, return_exceptions=True))
        raise


async def extract_archive(broker, args: dict) -> dict:
    work = parent = None
    try:
        if not isinstance(args, dict) or set(args) - {"archive_path", "target_path", "strip_components"}:
            raise PreparationError("pi_archive_arguments_invalid")
        archive = broker._path(args.get("archive_path"))
        parent, leaf, destination = broker._parent(args.get("target_path"), write=True, create=True)
        if os.path.lexists(destination):
            raise PreparationError("pi_environment_target_exists")
        work = Path(tempfile.mkdtemp(prefix=".meecp-extract-", dir=broker.workspace))
        def extract(*, check):
            return extract_regular_archive(archive, work / "tree", strip_components=args.get("strip_components", 0), check=check)
        result = await _thread(extract, broker=broker)
        broker._check_roots()
        os.rename(work / "tree", leaf, dst_dir_fd=parent)
        os.fsync(parent)
        return {"success": True, "output": json.dumps({"target_path": str(destination), **result}, sort_keys=True)}
    except (asyncio.CancelledError, OwnershipLost):
        raise
    except PreparationError as error:
        return {"success": False, "output": error.code}
    except (OSError, ValueError, TypeError):
        return {"success": False, "output": "pi_environment_setup_failed"}
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        if parent is not None:
            os.close(parent)


async def prepare_environment(broker, args: dict, host_root: Path | None) -> dict:
    work = parent = None
    try:
        if (not isinstance(args, dict) or set(args) - {"runtime", "target_path"}
                or not isinstance(args.get("runtime"), str)):
            raise PreparationError("pi_environment_arguments_invalid")
        runtime = args["runtime"]
        # Discovery is descriptive. Other runtimes can be fetched, extracted
        # and built with ordinary agent-authored commands in the same workspace.
        system = {name: str(path) for name, path in ((name, Path("/usr/bin") / name)
                  for name in ("clang", "clang++", "make", "git", "swift", "xcrun", "tar", "unzip")) if path.is_file()}
        if runtime == "system":
            return {"success": True, "output": json.dumps({"system_toolchains": system,
                    "other_runtimes": "fetch_url + extract_archive + run_command"}, sort_keys=True)}
        if host_root is None or runtime not in {"node", "python"}:
            raise PreparationError("pi_environment_runtime_not_bundled")
        host_root = Path(host_root)
        source = host_root / ("pi-runtime" if runtime == "node" else "python")
        parent, leaf, destination = broker._parent(args.get("target_path", ".meecp-environments/" + runtime), write=True, create=True)
        if os.path.lexists(destination):
            raise PreparationError("pi_environment_target_exists")
        work = Path(tempfile.mkdtemp(prefix=".meecp-environment-", dir=broker.workspace))
        def stage(*, check):
            tree = work / "tree"
            if runtime == "node":
                tree.mkdir(mode=0o700)
                (tree / "bin").mkdir(mode=0o700)
                shutil.copyfile(source / "bin/node", tree / "bin/node")
                (tree / "bin/node").chmod(0o755)
                copy_regular_tree(source / "npm", tree / "npm", check)
                (tree / "licenses").mkdir(mode=0o700)
                shutil.copyfile(source / "licenses/NODE-LICENSE.txt", tree / "licenses/NODE-LICENSE.txt")
                shutil.copyfile(source / "provenance.json", tree / "provenance.json")
            else:
                copy_regular_tree(source, tree, check)
            check()
        await _thread(stage, broker=broker)
        result = {"schema_version": 1, "runtime": runtime, "runtime_path": str(destination), "system_toolchains": system}
        if runtime == "node":
            result.update(node_path=str(destination / "bin/node"), npm_cli_path=str(destination / "npm/bin/npm-cli.js"))
        else:
            result.update(python_path=str(destination / "bin/python3.12"), site_packages_path=str(destination / "site-packages"))
        (work / "tree" / ENVIRONMENT_MARKER).write_text(json.dumps(result, sort_keys=True) + "\n")
        broker._check_roots()
        os.rename(work / "tree", leaf, dst_dir_fd=parent)
        os.fsync(parent)
        return {"success": True, "output": json.dumps(result, sort_keys=True)}
    except (asyncio.CancelledError, OwnershipLost):
        raise
    except PreparationError as error:
        return {"success": False, "output": error.code}
    except (OSError, ValueError, TypeError):
        return {"success": False, "output": "pi_environment_setup_failed"}
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        if parent is not None:
            os.close(parent)
