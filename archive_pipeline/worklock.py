"""Per-work-directory advisory lock shared by collection and reclamation.

The scanner writes a raw response to the content-addressed archive *before*
it commits the corresponding database facts (scan item / version).  A storage
reclaimer that looked only at the database during that window would see an
unreferenced blob and could move it while the scanner is still ingesting it.
The grace period is not a fix for that window: the race exists regardless of
how old the bytes are.

Both sides therefore take the same lock:

* collection (``ScanEngine.scan``) holds it **shared** for the whole scan, so
  concurrent scans are still possible;
* isolation / restore / purge hold it **exclusive** for the whole operation.

The lock is a ``flock`` on ``<workdir>/pipeline.lock``.  It is keyed by the
work directory, so two different work directories never block each other.
It is deliberately acquired inside the production entry points rather than
left to callers: a collection path cannot opt out.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path


class LockTimeout(TimeoutError):
    """The requested lock could not be acquired before the timeout."""


class WorkdirLock:
    """Advisory lock on one work directory.

    ``shared=True`` is the collection side; ``shared=False`` is the storage
    reclaimer side.  ``timeout=None`` blocks indefinitely.  The lock is
    released when the context exits and also if the process dies (the kernel
    drops ``flock`` locks when the file descriptor is closed).
    """

    def __init__(self, workdir: str | Path, *, shared: bool,
                 timeout: float | None = None, poll_seconds: float = 0.05):
        self.workdir = Path(workdir)
        self.shared = shared
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.path = self.workdir / "pipeline.lock"
        self._fd: int | None = None

    def _open(self) -> int:
        self.workdir.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)

    def acquire(self) -> "WorkdirLock":
        if self._fd is not None:
            raise RuntimeError("lock already acquired")
        fd = self._open()
        mode = (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, mode)
                self._fd = fd
                return self
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    kind = "collection" if self.shared else "exclusive"
                    raise LockTimeout(
                        f"timed out after {self.timeout:.1f}s waiting for the "
                        f"{kind} lock on {self.workdir}")
                time.sleep(self.poll_seconds)
            except BaseException:
                os.close(fd)
                raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "WorkdirLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def collection_lock(workdir: str | Path, *, timeout: float | None = None) -> WorkdirLock:
    """The shared lock held by the official collection path."""
    return WorkdirLock(workdir, shared=True, timeout=timeout)


def exclusive_lock(workdir: str | Path, *, timeout: float | None = None) -> WorkdirLock:
    """The exclusive lock held by isolate / restore / purge."""
    return WorkdirLock(workdir, shared=False, timeout=timeout)
