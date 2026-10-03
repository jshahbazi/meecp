"""Host-controlled, hash-pinned wheel acquisition for private preparation.

The builder supplies immutable wheel selections. A separately bound policy
selects allowed hosts and disk/deadline limits. No uv networking, credentials,
redirects, proxy environment or dependency installer executes during acquisition.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import time
from urllib.parse import urlsplit

from .downloads import DownloadError, PinnedHTTPSNetwork, normalize_spec
from .preparation_builder import WheelRequest
from .preparation_contracts import PreparationError

HEADROOM = 16 * 1024 * 1024


class WheelAcquirer:
    def __init__(self, network=None):
        self.network = network if network is not None else PinnedHTTPSNetwork()

    async def acquire(self, requests: tuple[WheelRequest, ...], destination: Path, *,
                      allowed_hosts: frozenset[str], disk_bytes: int, wall_seconds: float,
                      check_canceled=lambda: None) -> None:
        """Publish exact wheels only after bytes/hash checks and durable writes.

        Caller retains the job's disk reservation until this coroutine settles.
        This sink checks its own byte ceiling and available space; aggregate
        reservation/admission belongs to the preparation coordinator.
        """
        if (type(requests) is not tuple or not 1 <= len(requests) <= 512
                or type(allowed_hosts) is not frozenset or not allowed_hosts
                or any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]{1,253}", host) for host in allowed_hosts)
                or type(disk_bytes) is not int or not 1 <= disk_bytes <= 8 * 1024**3
                or isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
                or not 0 < wall_seconds <= 3600):
            raise PreparationError("invalid_dependency_acquisition_policy")
        selections = []
        deadline = time.monotonic() + wall_seconds
        def check():
            check_canceled()
            if time.monotonic() >= deadline:
                raise PreparationError("dependency_acquisition_timeout")
        filenames = set()
        total = 0
        for item in requests:
            if (not isinstance(item, WheelRequest) or not isinstance(item.filename, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,256}\.whl", item.filename)
                    or item.filename in filenames):
                raise PreparationError("invalid_dependency_acquisition_identity")
            try:
                selected = normalize_spec({"url": item.url, "sha256": item.sha256, "size": item.size})
            except DownloadError:
                raise PreparationError("invalid_dependency_acquisition_identity") from None
            parsed = urlsplit(selected["url"])
            if parsed.hostname not in allowed_hosts or Path(parsed.path).name != item.filename:
                raise PreparationError("dependency_origin_not_authorized")
            filenames.add(item.filename)
            total += item.size
            selections.append((item, selected["url"]))
        if total > disk_bytes:
            raise PreparationError("dependency_disk_limit")
        directory_fd = None
        created = []
        completed = False
        try:
            if (not destination.is_absolute() or destination.resolve() != destination or destination.is_symlink()
                    or not destination.is_dir() or any(destination.iterdir())):
                raise PreparationError("unsafe_dependency_acquisition_workspace")
            directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PreparationError("unsafe_dependency_acquisition_workspace")
            if shutil.disk_usage(destination).free < total + HEADROOM:
                raise PreparationError("dependency_disk_unavailable")
            async with asyncio.timeout(wall_seconds):
                for item, url in selections:
                    check()
                    filename = item.filename
                    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                 dir_fd=directory_fd)
                    created.append(filename)
                    with os.fdopen(fd, "wb") as sink:
                        received = 0
                        digest = hashlib.sha256()
                        async with self.network.stream(url) as chunks:
                            async for chunk in chunks:
                                check()
                                if (not isinstance(chunk, bytes) or not chunk or len(chunk) > 65536
                                        or received + len(chunk) > item.size):
                                    raise PreparationError("dependency_size_mismatch")
                                sink.write(chunk)
                                received += len(chunk)
                                digest.update(chunk)
                        check()
                        if received != item.size or digest.hexdigest() != item.sha256:
                            raise PreparationError("dependency_artifact_mismatch")
                        sink.flush()
                        os.fsync(sink.fileno())
                        check()
                        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                        opened = os.fstat(sink.fileno())
                        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                            raise PreparationError("dependency_workspace_changed")
                visible = destination.stat()
                if (destination.resolve() != destination or (visible.st_dev, visible.st_ino) != (info.st_dev, info.st_ino)):
                    raise PreparationError("dependency_workspace_changed")
                check()
                os.fsync(directory_fd)
                check()
                completed = True
        except TimeoutError:
            raise PreparationError("dependency_acquisition_timeout") from None
        except DownloadError as failure:
            codes = {"download_redirect_blocked": "dependency_redirect_blocked",
                     "download_network_blocked": "dependency_network_blocked"}
            raise PreparationError(codes.get(failure.code, "dependency_network_unavailable")) from None
        except OSError:
            raise PreparationError("dependency_acquisition_failed") from None
        except BaseException:
            # Canceled/failed attempts leave no partial or superficially usable
            # wheel set. Cleanup remains bound to the originally opened directory.
            raise
        finally:
            if directory_fd is not None:
                # A successful complete set is the only retained result.
                if not completed:
                    for name in created:
                        try:
                            os.unlink(name, dir_fd=directory_fd)
                        except OSError:
                            pass
                os.close(directory_fd)
